import asyncio
import copy
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from tests.integration.database import require_test_database_url
from tests.test_normalization_parsers import raw
from trading_bot.archive.retention import (
    DELETE_CONFIRMATION_TOKEN,
    ArchivedRawRangeTarget,
    BoundedRetentionRunner,
    RetentionRuntimeGuards,
)
from trading_bot.archive.retention_identity import require_retention_mutation_identity
from trading_bot.storage.database import create_engine, create_session_factory


def _retention_guards() -> RetentionRuntimeGuards:
    return RetentionRuntimeGuards(
        collector_stopped=True,
        write_quiescent=True,
        postgresql_healthy=True,
        free_disk_bytes=4 * 1024**3,
        min_free_disk_bytes=3 * 1024**3,
    )


def _role_database_url(base_url: str, username: str, password: str) -> str:
    return make_url(base_url).set(username=username, password=password).render_as_string(
        hide_password=False
    )


def _sql_string_literal(value: str) -> str:
    # CREATE ROLE ... PASSWORD does not accept bind parameters (asyncpg $1).
    return "'" + value.replace("'", "''") + "'"


async def _provision_ephemeral_roles(
    owner_factory,
    *,
    suffix: str,
    password: str,
) -> tuple[str, str, str]:
    research_role = f"retention_it_research_{suffix}"
    retention_like_role = f"retention_it_retention_{suffix}"
    decoy_table = f"retention_decoy_{suffix}"
    async with owner_factory.begin() as session:
        await session.execute(
            text(
                f"CREATE ROLE {research_role} LOGIN PASSWORD {_sql_string_literal(password)}"
            )
        )
        await session.execute(
            text(
                f"CREATE ROLE {retention_like_role} "
                f"LOGIN PASSWORD {_sql_string_literal(password)}"
            )
        )
        await session.execute(text(f"CREATE TABLE {decoy_table} (id bigint PRIMARY KEY)"))
        await session.execute(text(f"GRANT SELECT ON market_events TO {research_role}"))
        await session.execute(
            text(
                f"GRANT SELECT, UPDATE, DELETE ON market_events TO {retention_like_role}"
            )
        )
    return research_role, retention_like_role, decoy_table


async def _ensure_retention_role(owner_factory, password: str) -> bool:
    async with owner_factory.begin() as session:
        exists = await session.scalar(
            text("SELECT 1 FROM pg_roles WHERE rolname = 'retention'")
        )
        created = exists is None
        if created:
            await session.execute(
                text(
                    "CREATE ROLE retention LOGIN PASSWORD "
                    f"{_sql_string_literal(password)}"
                )
            )
        await session.execute(
            text("GRANT SELECT, UPDATE, DELETE ON market_events TO retention")
        )
    return created


async def _cleanup_retention_role(owner_factory, *, retention_created: bool) -> None:
    if not retention_created:
        return
    async with owner_factory.begin() as session:
        await session.execute(text("DROP ROLE IF EXISTS retention"))


async def _cleanup_ephemeral_roles(
    owner_factory,
    *,
    research_role: str,
    retention_like_role: str,
    decoy_table: str,
) -> None:
    async with owner_factory.begin() as session:
        await session.execute(text(f"DROP TABLE IF EXISTS {decoy_table}"))
        await session.execute(text(f"DROP ROLE IF EXISTS {retention_like_role}"))
        await session.execute(text(f"DROP ROLE IF EXISTS {research_role}"))


def test_retention_role_privilege_matrix() -> None:
    async def check() -> None:
        base_url = require_test_database_url()
        owner_engine = create_engine(base_url)
        owner_factory = create_session_factory(owner_engine)
        suffix = uuid4().hex[:12]
        password = f"retention-it-{suffix}"
        research_role, retention_like_role, decoy_table = await _provision_ephemeral_roles(
            owner_factory,
            suffix=suffix,
            password=password,
        )
        research_url = _role_database_url(base_url, research_role, password)
        retention_like_url = _role_database_url(base_url, retention_like_role, password)
        research_engine = create_engine(research_url)
        retention_like_engine = create_engine(retention_like_url)
        try:
            async with research_engine.connect() as connection:
                await connection.execute(text("SELECT 1 FROM market_events LIMIT 1"))
                with pytest.raises(DBAPIError):
                    await connection.execute(
                        text("DELETE FROM market_events WHERE id = -1")
                    )
                with pytest.raises(DBAPIError):
                    await connection.execute(
                        text(
                            "SELECT id FROM market_events "
                            "WHERE id = -1 FOR UPDATE SKIP LOCKED"
                        )
                    )
            async with retention_like_engine.connect() as connection:
                await connection.execute(text("SELECT 1 FROM market_events LIMIT 1"))
                await connection.execute(
                    text(
                        "SELECT id FROM market_events "
                        "WHERE id = -1 FOR UPDATE SKIP LOCKED"
                    )
                )
                await connection.execute(
                    text("DELETE FROM market_events WHERE id = -1")
                )
                with pytest.raises(DBAPIError):
                    await connection.execute(text("INSERT INTO market_events DEFAULT VALUES"))
                with pytest.raises(DBAPIError):
                    await connection.execute(text("TRUNCATE market_events"))
                with pytest.raises(DBAPIError):
                    await connection.execute(text(f"DELETE FROM {decoy_table} WHERE id = -1"))
        finally:
            await research_engine.dispose()
            await retention_like_engine.dispose()
            await _cleanup_ephemeral_roles(
                owner_factory,
                research_role=research_role,
                retention_like_role=retention_like_role,
                decoy_table=decoy_table,
            )
            await owner_engine.dispose()

    asyncio.run(check())


def test_bounded_retention_runner_requires_retention_identity(tmp_path) -> None:
    async def check() -> None:
        base_url = require_test_database_url()
        owner_engine = create_engine(base_url)
        owner_factory = create_session_factory(owner_engine)
        suffix = uuid4().hex[:12]
        password = f"retention-it-{suffix}"
        symbol = f"RET-ID-{suffix}"
        retention_created = await _ensure_retention_role(owner_factory, password)
        retention_url = _role_database_url(base_url, "retention", password)
        retention_engine = create_engine(retention_url)
        retention_factory = create_session_factory(retention_engine)
        try:
            async with owner_factory.begin() as session:
                event = raw("mark_price", raw_id=1)
                event.id = None
                event.symbol = symbol
                event.payload = copy.deepcopy(event.payload)
                event.payload["symbol"] = symbol
                session.add(event)
            async with owner_factory() as session:
                event_id = await session.scalar(
                    text("SELECT id FROM market_events WHERE symbol = :symbol LIMIT 1"),
                    {"symbol": symbol},
                )
            target = ArchivedRawRangeTarget(
                min_raw_event_id=int(event_id),
                max_raw_event_id=int(event_id),
                expected_row_count=1,
                coverage_plan_sha256="integration-identity",
                windows=(),
            )
            audit_dir = tmp_path / "audit"
            async with retention_factory() as session:
                role = await require_retention_mutation_identity(session)
                assert role == "retention"

            owner_runner = BoundedRetentionRunner(
                owner_factory,
                audit_dir / "owner",
                test_mode=False,
            )
            with pytest.raises(PermissionError):
                await owner_runner.execute(
                    target,
                    _retention_guards(),
                    confirm_delete=True,
                    confirmation=DELETE_CONFIRMATION_TOKEN,
                    batch_size=1,
                    pause_seconds=0,
                )

            runner = BoundedRetentionRunner(
                retention_factory,
                audit_dir,
                test_mode=False,
            )
            result = await runner.execute(
                target,
                _retention_guards(),
                confirm_delete=True,
                confirmation=DELETE_CONFIRMATION_TOKEN,
                batch_size=1,
                pause_seconds=0,
            )
            assert result["status"] == "completed"
            assert result["remaining_rows"] == 0
            async with owner_factory() as session:
                remaining = await session.scalar(
                    text("SELECT count(*) FROM market_events WHERE symbol = :symbol"),
                    {"symbol": symbol},
                )
            assert remaining == 0
        finally:
            await retention_engine.dispose()
            await _cleanup_retention_role(
                owner_factory,
                retention_created=retention_created,
            )
            await owner_engine.dispose()

    asyncio.run(check())
