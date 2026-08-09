import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.archive.retention import (
    DELETE_CONFIRMATION_TOKEN,
    MAX_DELETE_CHUNK,
    PROGRESS_STATUS_COMPLETED,
    PROGRESS_STATUS_RUNNING,
    ArchivedRawRangeTarget,
    BoundedRetentionRunner,
    RetentionCandidate,
    RetentionExecutor,
    RetentionRuntimeGuards,
    assert_retention_guards,
    load_coverage_plan,
    verify_archive_coverage_for_retention,
)
from trading_bot.archive.store import LocalArchiveStore
from trading_bot.archive.window import (
    COMPLETED_MARKER_NAME,
    INCOMPLETE_MARKER_NAME,
    _attempt_key,
    _completed_key,
    _incomplete_key,
)


def _write_coverage_plan(
    path: Path,
    *,
    native: bool = True,
    windows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    default_windows = [
        {
            "dataset_id": "dataset-a",
            "expected_event_count": 2,
            "min_raw_event_id": 10,
            "max_raw_event_id": 11,
            "start_utc": "2026-07-01T00:00:00+00:00",
            "end_utc": "2026-07-01T01:00:00+00:00",
        },
        {
            "dataset_id": "dataset-b",
            "expected_event_count": 3,
            "min_raw_event_id": 12,
            "max_raw_event_id": 14,
            "start_utc": "2026-07-01T01:00:00+00:00",
            "end_utc": "2026-07-01T02:00:00+00:00",
        },
    ]
    windows = windows if windows is not None else default_windows
    if native:
        payload: dict[str, Any] = {
            "min_raw_event_id": 10,
            "max_raw_event_id": 14,
            "expected_row_count": 5,
            "windows": windows,
        }
    else:
        payload = {
            "source_bounds": {
                "min_id": 10,
                "max_id": 14,
                "expected_rows": 5,
            },
            "windows": [
                {
                    "dataset_id": window["dataset_id"],
                    "min_id": window["min_raw_event_id"],
                    "max_id": window["max_raw_event_id"],
                    "expected_event_count": window["expected_event_count"],
                }
                for window in windows
            ],
        }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def _seed_completed_window(
    store: LocalArchiveStore,
    dataset_id: str,
    *,
    attempt_id: str,
    events: int,
) -> None:
    store.publish_bytes(
        _completed_key(dataset_id),
        (
            json.dumps(
                {
                    "status": COMPLETED_MARKER_NAME,
                    "dataset_id": dataset_id,
                    "attempt_id": attempt_id,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    )
    store.publish_bytes(
        _attempt_key(dataset_id, attempt_id, "archive_metadata.json"),
        (
            json.dumps(
                {"row_counts": {"events": events}},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    )


def _target_from_plan(path: Path) -> ArchivedRawRangeTarget:
    return load_coverage_plan(path)


def _guards() -> RetentionRuntimeGuards:
    return RetentionRuntimeGuards(
        collector_stopped=True,
        write_quiescent=True,
        postgresql_healthy=True,
        free_disk_bytes=4 * 1024**3,
        min_free_disk_bytes=3 * 1024**3,
    )


def test_load_coverage_plan_native_and_hot_buffer(tmp_path: Path) -> None:
    native_path = tmp_path / "native.json"
    _write_coverage_plan(native_path, native=True)
    native = load_coverage_plan(native_path)
    assert native.min_raw_event_id == 10
    assert native.max_raw_event_id == 14
    assert native.expected_row_count == 5
    assert len(native.windows) == 2

    hot_path = tmp_path / "hot.json"
    _write_coverage_plan(hot_path, native=False)
    hot = load_coverage_plan(hot_path)
    assert hot.expected_row_count == native.expected_row_count
    assert hot.windows[0].dataset_id == "dataset-a"


def test_load_coverage_plan_rejects_windows_without_id_bounds(tmp_path: Path) -> None:
    path = tmp_path / "missing-ids.json"
    payload = {
        "min_raw_event_id": 10,
        "max_raw_event_id": 14,
        "expected_row_count": 5,
        "windows": [
            {
                "dataset_id": "dataset-a",
                "expected_event_count": 2,
            },
            {
                "dataset_id": "dataset-b",
                "expected_event_count": 3,
                "min_raw_event_id": 12,
                "max_raw_event_id": 14,
            },
        ],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="id bounds"):
        load_coverage_plan(path)


def test_load_coverage_plan_rejects_gaps(tmp_path: Path) -> None:
    path = tmp_path / "gap.json"
    payload = {
        "min_raw_event_id": 10,
        "max_raw_event_id": 14,
        "expected_row_count": 5,
        "windows": [
            {
                "dataset_id": "dataset-a",
                "expected_event_count": 2,
                "min_raw_event_id": 10,
                "max_raw_event_id": 11,
            },
            {
                "dataset_id": "dataset-b",
                "expected_event_count": 3,
                "min_raw_event_id": 13,
                "max_raw_event_id": 14,
            },
        ],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="contiguous"):
        load_coverage_plan(path)


def test_verify_archive_coverage_passes_with_storage_only_evidence(
    tmp_path: Path,
) -> None:
    store = LocalArchiveStore(tmp_path / "archive", destination_label="b2_s3")
    _seed_completed_window(store, "dataset-a", attempt_id="a1", events=2)
    _seed_completed_window(store, "dataset-b", attempt_id="b1", events=3)
    plan_path = tmp_path / "plan.json"
    _write_coverage_plan(plan_path)
    target = _target_from_plan(plan_path)
    result = verify_archive_coverage_for_retention(store, target)
    assert result["status"] == "pass"
    assert result["storage_coverage_continuous"] is True


def test_verify_archive_coverage_fails_on_incomplete_marker(tmp_path: Path) -> None:
    store = LocalArchiveStore(tmp_path / "archive", destination_label="b2_s3")
    _seed_completed_window(store, "dataset-a", attempt_id="a1", events=2)
    _seed_completed_window(store, "dataset-b", attempt_id="b1", events=3)
    plan_path = tmp_path / "plan.json"
    _write_coverage_plan(plan_path)
    store.publish_bytes(
        _incomplete_key("dataset-b", "stale"),
        json.dumps({"status": INCOMPLETE_MARKER_NAME}).encode("utf-8"),
    )
    target = _target_from_plan(plan_path)
    result = verify_archive_coverage_for_retention(store, target)
    assert result["status"] == "fail"
    assert any("INCOMPLETE" in reason for reason in result["reasons"])


def test_verify_archive_coverage_fails_on_event_count_mismatch(tmp_path: Path) -> None:
    store = LocalArchiveStore(tmp_path / "archive", destination_label="b2_s3")
    _seed_completed_window(store, "dataset-a", attempt_id="a1", events=99)
    _seed_completed_window(store, "dataset-b", attempt_id="b1", events=3)
    plan_path = tmp_path / "plan.json"
    _write_coverage_plan(plan_path)
    target = _target_from_plan(plan_path)
    result = verify_archive_coverage_for_retention(store, target)
    assert result["status"] == "fail"


def test_assert_retention_guards_fail_closed() -> None:
    with pytest.raises(RuntimeError, match="collector"):
        assert_retention_guards(
            RetentionRuntimeGuards(
                collector_stopped=False,
                write_quiescent=True,
                postgresql_healthy=True,
                free_disk_bytes=4 * 1024**3,
                min_free_disk_bytes=3 * 1024**3,
            )
        )


def test_batch_size_above_cap_rejected(tmp_path: Path) -> None:
    async def check() -> None:
        runner = BoundedRetentionRunner(
            cast(async_sessionmaker[AsyncSession], object()),
            tmp_path / "audit",
            test_mode=True,
        )
        target = ArchivedRawRangeTarget(
            min_raw_event_id=1,
            max_raw_event_id=5,
            expected_row_count=5,
            coverage_plan_sha256="abc",
            windows=(),
        )
        with pytest.raises(ValueError, match="batch_size"):
            await runner.dry_run(target, _guards(), batch_size=MAX_DELETE_CHUNK + 1)

    asyncio.run(check())


def test_confirm_delete_false_never_mutates(tmp_path: Path) -> None:
    class FakeSession:
        async def execute(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def scalar(self, *_args: object, **_kwargs: object) -> int:
            return 5

        async def scalars(self, *_args: object, **_kwargs: object) -> list[int]:
            return []

    class FakeContext:
        async def __aenter__(self) -> FakeSession:
            return FakeSession()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeBegin:
        async def __aenter__(self) -> FakeSession:
            return FakeSession()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeFactory:
        def __call__(self) -> FakeContext:
            return FakeContext()

        def begin(self) -> FakeBegin:
            return FakeBegin()

    async def check() -> None:
        runner = BoundedRetentionRunner(
            cast(async_sessionmaker[AsyncSession], FakeFactory()),
            tmp_path / "audit",
            test_mode=True,
        )
        target = ArchivedRawRangeTarget(
            min_raw_event_id=1,
            max_raw_event_id=5,
            expected_row_count=5,
            coverage_plan_sha256="abc",
            windows=(),
        )
        result = await runner.execute(
            target,
            _guards(),
            confirm_delete=False,
            confirmation=DELETE_CONFIRMATION_TOKEN,
        )
        assert result["dry_run"] is True
        assert result["status"] == "planned"
        assert result["mutation"] is False
        assert result["confirm_delete"] is False

    asyncio.run(check())


def test_confirmation_token_required_for_execute(tmp_path: Path) -> None:
    async def check() -> None:
        runner = BoundedRetentionRunner(
            cast(async_sessionmaker[AsyncSession], object()),
            tmp_path / "audit",
            test_mode=True,
        )
        target = ArchivedRawRangeTarget(
            min_raw_event_id=1,
            max_raw_event_id=5,
            expected_row_count=5,
            coverage_plan_sha256="abc",
            windows=(),
        )
        with pytest.raises(PermissionError, match="confirmation"):
            await runner.execute(
                target,
                _guards(),
                confirm_delete=True,
                confirmation="wrong-token",
            )

    asyncio.run(check())


def test_retention_executor_accepts_b2_s3_destination(tmp_path: Path) -> None:
    store = LocalArchiveStore(tmp_path / "audit", destination_label="b2_s3")

    class BrokenFactory:
        def begin(self) -> object:
            raise AssertionError("delete should not run in this unit test")

    executor = RetentionExecutor(
        cast(async_sessionmaker[AsyncSession], BrokenFactory()),
        store,
        test_mode=False,
    )
    with pytest.raises(AssertionError):
        asyncio.run(
            executor.delete_verified_chunk(
                RetentionCandidate(
                    interval_start_utc="2026-07-01T00:00:00+00:00",
                    interval_end_utc="2026-07-02T00:00:00+00:00",
                    min_raw_event_id=1,
                    max_raw_event_id=3,
                    row_count=3,
                    manifest_sha256="a" * 64,
                ),
                limit=1,
                confirmation=DELETE_CONFIRMATION_TOKEN,
            )
        )


def test_dry_run_does_not_clobber_completed_progress(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    operation_id = "completed-op"
    progress_path = audit_dir / f"{operation_id}.progress.json"
    audit_path = audit_dir / f"{operation_id}.json"
    progress_payload = {
        "operation_id": operation_id,
        "min_raw_event_id": 10,
        "max_raw_event_id": 14,
        "expected_rows": 5,
        "batch_size": 1000,
        "cumulative_deleted": 5,
        "batches_completed": 1,
        "last_deleted_max_id": 14,
        "status": PROGRESS_STATUS_COMPLETED,
        "updated_at_utc": "2026-07-01T00:00:00+00:00",
        "last_error": None,
    }
    audit_payload = {
        **progress_payload,
        "rows_deleted_per_batch": [5],
        "git_sha": None,
        "coverage_plan_sha256": "abc",
        "confirmation_provided": True,
        "started_at_utc": "2026-07-01T00:00:00+00:00",
        "completed_at_utc": "2026-07-01T00:00:00+00:00",
        "final_remaining_target_rows": 0,
    }
    progress_path.write_text(json.dumps(progress_payload, indent=2) + "\n", encoding="utf-8")
    audit_path.write_text(json.dumps(audit_payload, indent=2) + "\n", encoding="utf-8")
    progress_before = progress_path.read_text(encoding="utf-8")
    audit_before = audit_path.read_text(encoding="utf-8")

    class FakeSession:
        async def execute(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def scalar(self, *_args: object, **_kwargs: object) -> int:
            return 0

    class FakeContext:
        async def __aenter__(self) -> FakeSession:
            return FakeSession()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeFactory:
        def __call__(self) -> FakeContext:
            return FakeContext()

    async def check() -> None:
        runner = BoundedRetentionRunner(
            cast(async_sessionmaker[AsyncSession], FakeFactory()),
            audit_dir,
            test_mode=True,
        )
        plan_path = tmp_path / "plan.json"
        _write_coverage_plan(plan_path)
        target = _target_from_plan(plan_path)
        result = await runner.dry_run(
            target,
            _guards(),
            operation_id=operation_id,
        )
        assert result["status"] == PROGRESS_STATUS_COMPLETED
        assert result["dry_run"] is True
        assert progress_path.read_text(encoding="utf-8") == progress_before
        assert audit_path.read_text(encoding="utf-8") == audit_before

    asyncio.run(check())


def test_resume_reconcile_after_cumulative_desync(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    operation_id = "resume-op"
    progress_path = audit_dir / f"{operation_id}.progress.json"
    audit_path = audit_dir / f"{operation_id}.json"
    progress_payload = {
        "operation_id": operation_id,
        "min_raw_event_id": 1,
        "max_raw_event_id": 5,
        "expected_rows": 5,
        "batch_size": 2,
        "cumulative_deleted": 1,
        "batches_completed": 1,
        "last_deleted_max_id": 1,
        "status": PROGRESS_STATUS_RUNNING,
        "updated_at_utc": "2026-07-01T00:00:00+00:00",
        "last_error": None,
    }
    audit_payload = {
        **progress_payload,
        "rows_deleted_per_batch": [1],
        "git_sha": None,
        "coverage_plan_sha256": "abc",
        "confirmation_provided": True,
        "started_at_utc": "2026-07-01T00:00:00+00:00",
        "completed_at_utc": None,
        "final_remaining_target_rows": None,
    }
    progress_path.write_text(json.dumps(progress_payload, indent=2) + "\n", encoding="utf-8")
    audit_path.write_text(json.dumps(audit_payload, indent=2) + "\n", encoding="utf-8")
    remaining_rows = 2

    class FakeSession:
        async def execute(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def scalar(self, *_args: object, **_kwargs: object) -> int:
            return remaining_rows

        async def scalars(self, *_args: object, **_kwargs: object) -> list[int]:
            nonlocal remaining_rows
            batch = list(range(1, min(remaining_rows, 2) + 1))
            remaining_rows -= len(batch)
            return batch

    class FakeContext:
        async def __aenter__(self) -> FakeSession:
            return FakeSession()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeBegin:
        async def __aenter__(self) -> FakeSession:
            return FakeSession()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeFactory:
        def __call__(self) -> FakeContext:
            return FakeContext()

        def begin(self) -> FakeBegin:
            return FakeBegin()

    async def check() -> None:
        runner = BoundedRetentionRunner(
            cast(async_sessionmaker[AsyncSession], FakeFactory()),
            audit_dir,
            test_mode=True,
        )
        target = ArchivedRawRangeTarget(
            min_raw_event_id=1,
            max_raw_event_id=5,
            expected_row_count=5,
            coverage_plan_sha256="abc",
            windows=(),
        )
        result = await runner.execute(
            target,
            _guards(),
            confirm_delete=True,
            confirmation=DELETE_CONFIRMATION_TOKEN,
            operation_id=operation_id,
            batch_size=2,
            pause_seconds=0,
        )
        assert result["status"] == PROGRESS_STATUS_COMPLETED
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        assert progress["cumulative_deleted"] == 5
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        assert audit["rows_deleted_per_batch"] == [1, 2]

    asyncio.run(check())


def test_cli_target_boundary_enforcement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_bot import archive_cli

    plan_path = tmp_path / "plan.json"
    _write_coverage_plan(plan_path)
    args = archive_cli._parser().parse_args(
        [
            "retention-dry-run",
            "--coverage-plan",
            str(plan_path),
            "--audit-dir",
            str(tmp_path / "audit"),
            "--min-id",
            "11",
            "--max-id",
            "14",
            "--expected-rows",
            "5",
            "--free-disk-bytes",
            str(4 * 1024**3),
            "--collector-stopped-confirmed",
            "--write-quiescence-confirmed",
            "--postgresql-healthy-confirmed",
        ]
    )
    with pytest.raises(ValueError, match="--min-id"):
        archive_cli._resolve_retention_target(args)


def test_cli_requires_target_boundary_args(tmp_path: Path) -> None:
    from trading_bot import archive_cli

    plan_path = tmp_path / "plan.json"
    _write_coverage_plan(plan_path)
    with pytest.raises(SystemExit):
        archive_cli._parser().parse_args(
            [
                "retention-dry-run",
                "--coverage-plan",
                str(plan_path),
                "--audit-dir",
                str(tmp_path / "audit"),
                "--free-disk-bytes",
                str(4 * 1024**3),
                "--collector-stopped-confirmed",
                "--write-quiescence-confirmed",
                "--postgresql-healthy-confirmed",
            ]
        )
