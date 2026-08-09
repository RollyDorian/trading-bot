"""PostgreSQL integration proof for RAW market_events partition lifecycle."""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from tests.integration.database import require_test_database_url
from trading_bot.storage.database import create_engine, create_session_factory
from trading_bot.storage.partition_gate import build_generation_archive_evidence
from trading_bot.storage.partitions import (
    DEFAULT_GENERATION_ROW_SPAN,
    DROP_GENERATION_CONFIRMATION_TOKEN,
    GenerationState,
    PartitionLifecycleError,
    assert_not_droppable_unless_eligible,
    drop_eligible_generation,
    ensure_writable_cover,
    is_market_events_partitioned,
    list_generations,
    mark_drop_eligible,
    mark_generation_state,
    measure_relation_size,
    provision_generation,
    read_sequence_cursor,
)
from trading_bot.storage.repository import EventRepository, MarketEventInput


def _alembic_config() -> Config:
    root = Path(__file__).parents[2]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.cmd_opts = Namespace(x=["database-role=test"])
    return config


def _role_url(base_url: str, username: str, password: str) -> str:
    return make_url(base_url).set(username=username, password=password).render_as_string(
        hide_password=False
    )


async def _reset_to_head(database_url: str) -> None:
    config = _alembic_config()
    engine = create_engine(database_url)
    try:
        async with engine.begin() as conn:
            # Downgrade of 20260809_0004 is fail-closed while rows exist.
            await conn.execute(text("DROP SCHEMA IF EXISTS pipeline CASCADE"))
            await conn.execute(text("DROP SCHEMA IF EXISTS normalized CASCADE"))
            await conn.execute(text("DROP TABLE IF EXISTS market_events CASCADE"))
            await conn.execute(
                text("DROP TABLE IF EXISTS market_event_generations CASCADE")
            )
            await conn.execute(text("DROP TABLE IF EXISTS system_events CASCADE"))
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version CASCADE"))
            await conn.execute(
                text(
                    "DROP FUNCTION IF EXISTS "
                    "public.drop_verified_market_event_generation(text, text, boolean)"
                )
            )
    finally:
        await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "20260809_0004")
    # Continuity probe: simulate production last_value without rewriting RAW.
    engine = create_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT setval('market_events_id_seq', 7471912, true)")
            )
            next_id = 7_471_913
            gens = await list_generations(conn)
            for gen in gens:
                if gen.state != GenerationState.DROPPED:
                    await conn.execute(text(f"DROP TABLE IF EXISTS {gen.partition_name}"))
            await conn.execute(text("DELETE FROM market_event_generations"))
            await provision_generation(
                conn,
                id_start=next_id,
                row_span=1_000,
                activate=True,
            )
    finally:
        await engine.dispose()


def test_alembic_upgrade_downgrade_partition_revision() -> None:
    database_url = require_test_database_url()

    async def run() -> None:
        config = _alembic_config()
        engine = create_engine(database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DROP SCHEMA IF EXISTS pipeline CASCADE"))
                await conn.execute(text("DROP SCHEMA IF EXISTS normalized CASCADE"))
                await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
                await conn.execute(text("CREATE SCHEMA public"))
                await conn.execute(text("GRANT ALL ON SCHEMA public TO cryptobot"))
                await conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
        finally:
            await engine.dispose()

        await asyncio.to_thread(command.upgrade, config, "20260809_0004")
        engine = create_engine(database_url)
        try:
            async with engine.connect() as conn:
                assert await is_market_events_partitioned(conn)
                gens = await list_generations(conn)
                assert len(gens) == 1
                assert gens[0].state == GenerationState.ACTIVE
                assert gens[0].row_span == DEFAULT_GENERATION_ROW_SPAN
                indexes = (
                    await conn.execute(
                        text(
                            """
                            SELECT indexname
                            FROM pg_indexes
                            WHERE schemaname = 'public'
                              AND tablename LIKE 'market_events%'
                            ORDER BY indexname
                            """
                        )
                    )
                ).scalars().all()
                assert "ix_market_events_source_sequence" in indexes
                assert "ix_market_events_symbol_exchange_at" in indexes
                assert "ix_market_events_type_received_at" in indexes
                # Parent PK / partition indexes present.
                assert any("pkey" in name for name in indexes)
            # Empty before downgrade (fail-closed otherwise).
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM market_events"))
            await asyncio.to_thread(command.downgrade, config, "20260729_0002")
            async with engine.connect() as conn:
                assert not await is_market_events_partitioned(conn)
            await asyncio.to_thread(command.upgrade, config, "20260809_0004")
            async with engine.connect() as conn:
                assert await is_market_events_partitioned(conn)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_sequence_continuity_and_id_routing() -> None:
    database_url = require_test_database_url()

    async def run() -> None:
        await _reset_to_head(database_url)
        engine = create_engine(database_url)
        factory = create_session_factory(engine)
        repo = EventRepository(factory)
        try:
            async with engine.connect() as conn:
                cursor = await read_sequence_cursor(conn)
                assert cursor.next_id == 7_471_913
                await ensure_writable_cover(conn, next_id=cursor.next_id)

            await repo.append_market_event(
                MarketEventInput(
                    received_at=datetime.now(UTC),
                    exchange_at=datetime.now(UTC),
                    source="hibachi",
                    event_type="ask_bid_price",
                    symbol="ETH/USDT-P",
                    sequence=1,
                    latency_ms=1.0,
                    payload={"v": 2},
                    connection_id=str(uuid4()),
                    local_sequence=1,
                    exchange_sequence=1,
                    schema_version=2,
                )
            )
            # Legacy v1-shaped row still accepted.
            await repo.append_market_event(
                MarketEventInput(
                    received_at=datetime.now(UTC),
                    exchange_at=None,
                    source="hibachi",
                    event_type="mark_price",
                    symbol="ETH/USDT-P",
                    sequence=None,
                    latency_ms=None,
                    payload={"mark": "1"},
                    schema_version=1,
                )
            )
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            """
                            SELECT id, schema_version, tableoid::regclass::text AS part
                            FROM market_events
                            ORDER BY id
                            """
                        )
                    )
                ).all()
                assert [int(r.id) for r in rows] == [7_471_913, 7_471_914]
                assert {int(r.schema_version) for r in rows} == {1, 2}
                assert all(str(r.part).startswith("market_events_g_") for r in rows)
            async with engine.begin() as conn:
                # Global uniqueness still enforced at parent PK.
                with pytest.raises(DBAPIError):
                    await conn.execute(
                        text(
                            """
                            INSERT INTO market_events (
                                id, source, event_type, symbol, payload
                            ) VALUES (
                                7471913, 'x', 'y', 'ETH/USDT-P', '{}'::jsonb
                            )
                            """
                        )
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_missing_partition_and_boundary_fail_closed() -> None:
    database_url = require_test_database_url()

    async def run() -> None:
        await _reset_to_head(database_url)
        engine = create_engine(database_url)
        try:
            async with engine.begin() as conn:
                # Fill almost to boundary (span=1000 starting 7471913).
                await conn.execute(
                    text(
                        """
                        INSERT INTO market_events (
                            source, event_type, symbol, payload
                        )
                        SELECT 't', 'probe', 'ETH/USDT-P', '{}'::jsonb
                        FROM generate_series(1, 999)
                        """
                    )
                )
                cursor = await read_sequence_cursor(conn)
                assert cursor.next_id == 7_471_913 + 999
                # Last slot of current generation still writable.
                await ensure_writable_cover(conn, headroom_rows=1)
                await conn.execute(
                    text(
                        """
                        INSERT INTO market_events (
                            source, event_type, symbol, payload
                        ) VALUES ('t', 'probe', 'ETH/USDT-P', '{}'::jsonb)
                        """
                    )
                )
                # Next id has no partition.
                with pytest.raises(PartitionLifecycleError, match="no generation covers"):
                    await ensure_writable_cover(conn)
                with pytest.raises(DBAPIError):
                    await conn.execute(
                        text(
                            """
                            INSERT INTO market_events (
                                source, event_type, symbol, payload
                            ) VALUES ('t', 'probe', 'ETH/USDT-P', '{}'::jsonb)
                            """
                        )
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_archive_gate_and_drop_reclaims_relation_bytes() -> None:
    database_url = require_test_database_url()

    async def run() -> None:
        await _reset_to_head(database_url)
        engine = create_engine(database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        """
                        INSERT INTO market_events (
                            source, event_type, symbol, payload
                        )
                        SELECT
                            't', 'ask_bid_price', 'ETH/USDT-P',
                            jsonb_build_object('pad', repeat('x', 200))
                        FROM generate_series(1, 500)
                        """
                    )
                )
                gens = await list_generations(conn)
                active = gens[0]
                part_before = await measure_relation_size(conn, active.partition_name)
                parent_before = await measure_relation_size(conn, "market_events")
                assert part_before.total_bytes > 0

                # Unverified cannot drop.
                with pytest.raises(PartitionLifecycleError, match="DROP_ELIGIBLE"):
                    await assert_not_droppable_unless_eligible(conn, active.generation_key)

                await mark_generation_state(
                    conn, active.generation_key, GenerationState.CLOSED_UNARCHIVED
                )
                await mark_generation_state(
                    conn, active.generation_key, GenerationState.VERIFIED
                )
                closed = (await list_generations(conn))[0]
                evidence = build_generation_archive_evidence(
                    generation=closed,
                    min_raw_event_id=7_471_913,
                    max_raw_event_id=7_471_913 + 499,
                    observed_row_count=500,
                    checksums_pass=True,
                    manifest_pass=True,
                    remote_completed=True,
                    download_verification_pass=True,
                    storage_reconciliation_pass=True,
                    id_coverage_contiguous=True,
                )
                await mark_drop_eligible(conn, closed.generation_key, evidence)

                # DELETE comparison sample on a second tiny partition.
                await provision_generation(
                    conn,
                    id_start=7_472_913,
                    row_span=200,
                    activate=True,
                )
                await conn.execute(
                    text("SELECT setval('market_events_id_seq', 7472912, true)")
                )
                await conn.execute(
                    text(
                        """
                        INSERT INTO market_events (
                            source, event_type, symbol, payload
                        )
                        SELECT 't', 'probe', 'ETH/USDT-P', jsonb_build_object('a', 1)
                        FROM generate_series(1, 100)
                        """
                    )
                )
                delete_part = [
                    g for g in await list_generations(conn) if g.id_start == 7_472_913
                ][0]
                delete_before = await measure_relation_size(conn, delete_part.partition_name)
                await conn.execute(
                    text("DELETE FROM market_events WHERE id >= 7472913")
                )
                delete_after = await measure_relation_size(conn, delete_part.partition_name)
                # Ordinary DELETE must not be treated as reliable reclaim.
                assert delete_after.total_bytes >= delete_before.total_bytes * 0.5

                dropped_size = await drop_eligible_generation(
                    conn,
                    closed.generation_key,
                    confirmation_token=DROP_GENERATION_CONFIRMATION_TOKEN,
                    operator_approved=True,
                )
                assert dropped_size.total_bytes == part_before.total_bytes
                parent_after = await measure_relation_size(conn, "market_events")
                assert parent_after.total_bytes < parent_before.total_bytes
                gone = await conn.scalar(
                    text("SELECT to_regclass(:rel)"),
                    {"rel": f"public.{active.partition_name}"},
                )
                assert gone is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_runtime_role_cannot_drop_partition() -> None:
    database_url = require_test_database_url()

    async def run() -> None:
        await _reset_to_head(database_url)
        engine = create_engine(database_url)
        suffix = uuid4().hex[:8]
        password = f"pw_{suffix}"
        research_role = f"part_it_research_{suffix}"
        generation_key = ""
        partition_name = ""
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        f"CREATE ROLE {research_role} LOGIN PASSWORD '{password}'"
                    )
                )
                await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {research_role}"))
                await conn.execute(
                    text(f"GRANT SELECT, INSERT ON market_events TO {research_role}")
                )
                await conn.execute(
                    text(
                        f"GRANT USAGE, SELECT ON SEQUENCE market_events_id_seq TO {research_role}"
                    )
                )
                await conn.execute(
                    text(f"GRANT SELECT ON market_event_generations TO {research_role}")
                )
                await conn.execute(
                    text(
                        f"""
                        REVOKE ALL ON FUNCTION
                        public.drop_verified_market_event_generation(text, text, boolean)
                        FROM {research_role}
                        """
                    )
                )
                gens = await list_generations(conn)
                generation_key = gens[0].generation_key
                partition_name = gens[0].partition_name

            research_engine = create_engine(_role_url(database_url, research_role, password))
            try:
                async with research_engine.begin() as conn:
                    await conn.execute(
                        text(
                            """
                            INSERT INTO market_events (
                                source, event_type, symbol, payload
                            ) VALUES ('t', 'probe', 'ETH/USDT-P', '{}'::jsonb)
                            """
                        )
                    )
                    with pytest.raises(DBAPIError):
                        await conn.execute(text(f"DROP TABLE {partition_name}"))
                    with pytest.raises(DBAPIError):
                        await conn.execute(
                            text(
                                """
                                SELECT public.drop_verified_market_event_generation(
                                    :key, :token, true
                                )
                                """
                            ),
                            {
                                "key": generation_key,
                                "token": DROP_GENERATION_CONFIRMATION_TOKEN,
                            },
                        )
            finally:
                await research_engine.dispose()
        finally:
            async with engine.begin() as conn:
                await conn.execute(text(f"REASSIGN OWNED BY {research_role} TO CURRENT_USER"))
                await conn.execute(text(f"DROP OWNED BY {research_role}"))
                await conn.execute(text(f"DROP ROLE IF EXISTS {research_role}"))
            await engine.dispose()

    asyncio.run(run())


def test_b2_failure_preserves_local_generation_and_interrupted_archive() -> None:
    database_url = require_test_database_url()

    async def run() -> None:
        await _reset_to_head(database_url)
        engine = create_engine(database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        """
                        INSERT INTO market_events (
                            source, event_type, symbol, payload
                        )
                        SELECT 't', 'probe', 'ETH/USDT-P', '{}'::jsonb
                        FROM generate_series(1, 10)
                        """
                    )
                )
                active = (await list_generations(conn))[0]
                await mark_generation_state(
                    conn, active.generation_key, GenerationState.CLOSED_UNARCHIVED
                )
                await mark_generation_state(
                    conn, active.generation_key, GenerationState.ARCHIVING
                )
                # Simulate B2 unavailable / verify failure — remain local.
                await mark_generation_state(
                    conn, active.generation_key, GenerationState.ARCHIVE_FAILED
                )
                # Successor may still be provisioned while capacity allows.
                await provision_generation(
                    conn,
                    id_start=active.id_end,
                    row_span=500,
                    activate=True,
                )
                gens = {g.generation_key: g for g in await list_generations(conn)}
                assert gens[active.generation_key].state == GenerationState.ARCHIVE_FAILED
                # Physical partition still present.
                present = await conn.scalar(
                    text("SELECT to_regclass(:rel)"),
                    {"rel": f"public.{active.partition_name}"},
                )
                assert present is not None
                with pytest.raises(PartitionLifecycleError):
                    await assert_not_droppable_unless_eligible(conn, active.generation_key)
                # Interrupted archive recovery: return to CLOSED_UNARCHIVED then VERIFIED.
                await mark_generation_state(
                    conn, active.generation_key, GenerationState.CLOSED_UNARCHIVED
                )
                await mark_generation_state(
                    conn, active.generation_key, GenerationState.ARCHIVING
                )
                await mark_generation_state(
                    conn, active.generation_key, GenerationState.VERIFIED
                )
                recovered = (await list_generations(conn))
                assert any(
                    g.generation_key == active.generation_key
                    and g.state == GenerationState.VERIFIED
                    for g in recovered
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_export_query_across_one_generation() -> None:
    """Archive-style SELECT by id range remains valid on the parent table."""

    database_url = require_test_database_url()

    async def run() -> None:
        await _reset_to_head(database_url)
        engine = create_engine(database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        """
                        INSERT INTO market_events (
                            source, event_type, symbol, schema_version, payload
                        )
                        SELECT
                            'hibachi', 'ask_bid_price', 'ETH/USDT-P', 2,
                            jsonb_build_object('i', g)
                        FROM generate_series(1, 25) AS g
                        """
                    )
                )
                rows = (
                    await conn.execute(
                        text(
                            """
                            SELECT id, schema_version, payload
                            FROM market_events
                            WHERE id >= :min_id AND id <= :max_id
                            ORDER BY id
                            """
                        ),
                        {"min_id": 7_471_913, "max_id": 7_471_937},
                    )
                ).mappings().all()
                assert len(rows) == 25
                assert int(rows[0]["id"]) == 7_471_913
                assert int(rows[-1]["id"]) == 7_471_937
                assert all(int(r["schema_version"]) in {1, 2} for r in rows)
                # JSON payload round-trip (RAW envelope compatibility).
                assert json.loads(json.dumps(rows[0]["payload"]))["i"] == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
