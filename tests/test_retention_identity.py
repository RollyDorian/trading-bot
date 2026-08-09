import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.archive.retention import (
    DELETE_CONFIRMATION_TOKEN,
    PROGRESS_STATUS_FAILED,
    ArchivedRawRangeTarget,
    BoundedRetentionRunner,
    RetentionRuntimeGuards,
)
from trading_bot.archive.retention_identity import (
    EXPECTED_RETENTION_DB_ROLE,
    FORBIDDEN_RETENTION_MUTATION_ROLES,
    require_retention_database_url,
    require_retention_mutation_identity,
)


def _guards() -> RetentionRuntimeGuards:
    return RetentionRuntimeGuards(
        collector_stopped=True,
        write_quiescent=True,
        postgresql_healthy=True,
        free_disk_bytes=4 * 1024**3,
        min_free_disk_bytes=3 * 1024**3,
    )


def _session_result(current_user: str, session_user: str, is_superuser: bool) -> MagicMock:
    result = MagicMock()
    result.one.return_value = (current_user, session_user, is_superuser)
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.mark.parametrize(
    ("current_user", "session_user", "is_superuser"),
    [
        ("research", "research", False),
        ("cryptobot", "cryptobot", False),
        ("cryptobot_runtime", "cryptobot_runtime", False),
        ("postgres", "postgres", False),
        ("test", "test", False),
        ("retention", "retention", True),
        ("retention", "postgres", False),
        ("owner", "owner", False),
    ],
)
def test_require_retention_mutation_identity_rejects_non_retention(
    current_user: str,
    session_user: str,
    is_superuser: bool,
) -> None:
    async def check() -> None:
        with pytest.raises(PermissionError):
            await require_retention_mutation_identity(
                cast(AsyncSession, _session_result(current_user, session_user, is_superuser))
            )

    asyncio.run(check())


def test_require_retention_mutation_identity_accepts_retention() -> None:
    async def check() -> None:
        role = await require_retention_mutation_identity(
            cast(AsyncSession, _session_result("retention", "retention", False))
        )
        assert role == EXPECTED_RETENTION_DB_ROLE

    asyncio.run(check())


def test_forbidden_roles_include_runtime_identities() -> None:
    assert "research" in FORBIDDEN_RETENTION_MUTATION_ROLES
    assert "cryptobot" in FORBIDDEN_RETENTION_MUTATION_ROLES
    assert "cryptobot_runtime" in FORBIDDEN_RETENTION_MUTATION_ROLES
    assert "postgres" in FORBIDDEN_RETENTION_MUTATION_ROLES
    assert "test" in FORBIDDEN_RETENTION_MUTATION_ROLES
    assert EXPECTED_RETENTION_DB_ROLE not in FORBIDDEN_RETENTION_MUTATION_ROLES


def test_require_retention_database_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RETENTION_DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="RETENTION_DATABASE_URL"):
        require_retention_database_url()


def test_require_retention_database_url_rejects_research_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RETENTION_DATABASE_URL",
        "postgresql+asyncpg://research:secret@localhost:5432/research_db",
    )
    with pytest.raises(ValueError, match="retention"):
        require_retention_database_url()


def test_require_retention_database_url_accepts_retention_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RETENTION_DATABASE_URL",
        "postgresql+asyncpg://retention:secret@localhost:5432/research_db",
    )
    assert (
        require_retention_database_url()
        == "postgresql+asyncpg://retention:secret@localhost:5432/research_db"
    )


def test_execute_refuses_failed_zero_delete_operation_resume(tmp_path: Any) -> None:
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    operation_id = "c146073f-faeb-47c1-bc86-d71ae9c73d97"
    progress_payload = {
        "operation_id": operation_id,
        "min_raw_event_id": 10,
        "max_raw_event_id": 14,
        "expected_rows": 5,
        "batch_size": 1000,
        "cumulative_deleted": 0,
        "batches_completed": 0,
        "last_deleted_max_id": None,
        "status": PROGRESS_STATUS_FAILED,
        "updated_at_utc": "2026-08-07T00:00:00+00:00",
        "last_error": "permission denied for table market_events",
    }
    audit_payload = {
        **progress_payload,
        "rows_deleted_per_batch": [],
        "git_sha": None,
        "coverage_plan_sha256": "abc",
        "confirmation_provided": True,
        "started_at_utc": "2026-08-07T00:00:00+00:00",
        "completed_at_utc": None,
        "final_remaining_target_rows": None,
    }
    (audit_dir / f"{operation_id}.progress.json").write_text(
        __import__("json").dumps(progress_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (audit_dir / f"{operation_id}.json").write_text(
        __import__("json").dumps(audit_payload, indent=2) + "\n",
        encoding="utf-8",
    )

    async def check() -> None:
        runner = BoundedRetentionRunner(
            cast(Any, object()),
            audit_dir,
            test_mode=True,
        )
        target = ArchivedRawRangeTarget(
            min_raw_event_id=10,
            max_raw_event_id=14,
            expected_row_count=5,
            coverage_plan_sha256="abc",
            windows=(),
        )
        with pytest.raises(RuntimeError, match="new --operation-id"):
            await runner.execute(
                target,
                _guards(),
                confirm_delete=True,
                confirmation=DELETE_CONFIRMATION_TOKEN,
                operation_id=operation_id,
            )

    asyncio.run(check())


def test_retention_execute_confirm_delete_requires_retention_url(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading_bot import archive_cli

    monkeypatch.delenv("RETENTION_DATABASE_URL", raising=False)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        __import__("json").dumps(
            {
                "min_raw_event_id": 10,
                "max_raw_event_id": 14,
                "expected_row_count": 5,
                "windows": [
                    {
                        "dataset_id": "dataset-a",
                        "expected_event_count": 5,
                        "min_raw_event_id": 10,
                        "max_raw_event_id": 14,
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    args = archive_cli._parser().parse_args(
        [
            "retention-execute",
            "--coverage-plan",
            str(plan_path),
            "--audit-dir",
            str(tmp_path / "audit"),
            "--min-id",
            "10",
            "--max-id",
            "14",
            "--expected-rows",
            "5",
            "--free-disk-bytes",
            str(4 * 1024**3),
            "--collector-stopped-confirmed",
            "--write-quiescence-confirmed",
            "--postgresql-healthy-confirmed",
            "--confirm-delete",
            "--confirmation-token",
            DELETE_CONFIRMATION_TOKEN,
        ]
    )
    with pytest.raises(ValueError, match="RETENTION_DATABASE_URL"):
        asyncio.run(archive_cli._retention_execute(args))
