"""Disposable PostgreSQL proof of continuous generation rotation operating loop."""

from __future__ import annotations

import asyncio
from argparse import Namespace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from tests.integration.database import require_test_database_url
from trading_bot.storage.database import create_engine
from trading_bot.storage.generation_archive import (
    MockGenerationArchiveBackend,
    archive_closed_generation,
    select_next_archive_candidate,
)
from trading_bot.storage.operator_status import OperatorAction, build_operator_status
from trading_bot.storage.partitions import (
    DROP_GENERATION_CONFIRMATION_TOKEN,
    GenerationState,
    PartitionLifecycleError,
    drop_eligible_generation,
    ensure_writable_cover,
    list_generations,
    mark_generation_state,
    provision_generation,
    provision_next_if_needed,
    read_sequence_cursor,
)
from trading_bot.storage.rotation import (
    assert_catalog_metadata_consistent,
    maintain_writable_generations,
    post_drop_verification,
    recover_active_from_sequence,
    rotate_active_if_sequence_crossed,
)


def _alembic_config() -> Config:
    root = Path(__file__).parents[2]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.cmd_opts = Namespace(x=["database-role=test"])
    return config


async def _reset_tiny(database_url: str, *, span: int = 100) -> None:
    config = _alembic_config()
    engine = create_engine(database_url)
    try:
        async with engine.begin() as conn:
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
    # Apply partition revision plus optional 0003 so this shared test DB
    # still has pipeline/normalized after the module finishes.
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_engine(database_url)
    try:
        async with engine.begin() as conn:
            gens = await list_generations(conn)
            for gen in gens:
                if gen.state != GenerationState.DROPPED:
                    await conn.execute(text(f"DROP TABLE IF EXISTS {gen.partition_name}"))
            await conn.execute(text("DELETE FROM market_event_generations"))
            await provision_generation(
                conn,
                id_start=7_471_913,
                row_span=span,
                activate=True,
            )
            await conn.execute(
                text("SELECT setval('market_events_id_seq', 7471912, true)")
            )
    finally:
        await engine.dispose()


async def _insert_n(conn, n: int) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO market_events (
                source, event_type, symbol, schema_version, payload
            )
            SELECT
                't',
                CASE WHEN g % 17 = 0 THEN 'trade' ELSE 'ask_bid_price' END,
                'ETH/USDT-P',
                2,
                jsonb_build_object('i', g)
            FROM generate_series(1, :n) AS g
            """
        ),
        {"n": n},
    )


def test_full_rotation_archive_verify_drop_loop() -> None:
    database_url = require_test_database_url()

    async def run() -> None:
        await _reset_tiny(database_url, span=100)
        engine = create_engine(database_url)
        try:
            async with engine.begin() as conn:
                await assert_catalog_metadata_consistent(conn)
                await _insert_n(conn, 80)
                action = await maintain_writable_generations(
                    conn,
                    row_span=100,
                    remaining_threshold=20,
                )
                assert action.provisioned_successor is True
                assert action.successor is not None
                assert action.successor.id_start == 7_472_013

                # Idempotent provision of the same successor.
                again = await provision_next_if_needed(
                    conn, row_span=100, remaining_threshold=20
                )
                assert again is not None
                assert again.generation_key == action.successor.generation_key
                again2 = await provision_generation(
                    conn,
                    id_start=7_472_013,
                    row_span=100,
                    activate=False,
                )
                assert again2.generation_key == action.successor.generation_key

                await _insert_n(conn, 25)
                rotated = await rotate_active_if_sequence_crossed(conn)
                assert rotated is not None
                assert rotated.state == GenerationState.CLOSED_UNARCHIVED
                gens = {g.generation_key: g for g in await list_generations(conn)}
                active = next(g for g in gens.values() if g.state == GenerationState.ACTIVE)
                assert active.id_start == 7_472_013

                last_a = await conn.scalar(
                    text(
                        """
                        SELECT MAX(id) FROM market_events
                        WHERE id >= 7471913 AND id < 7472013
                        """
                    )
                )
                first_b = await conn.scalar(
                    text(
                        """
                        SELECT MIN(id) FROM market_events
                        WHERE id >= 7472013 AND id < 7472113
                        """
                    )
                )
                assert int(last_a) == 7_472_012
                assert int(first_b) == 7_472_013

                await _insert_n(conn, 5)
                candidate = await select_next_archive_candidate(conn)
                assert candidate is not None
                evidence = await archive_closed_generation(
                    conn,
                    candidate.generation_key,
                    MockGenerationArchiveBackend(),
                )
                assert evidence.observed_row_count == 100
                closed = next(
                    g
                    for g in await list_generations(conn)
                    if g.generation_key == candidate.generation_key
                )
                assert closed.state == GenerationState.DROP_ELIGIBLE

                with pytest.raises(PartitionLifecycleError, match="DROP_ELIGIBLE"):
                    await drop_eligible_generation(
                        conn,
                        active.generation_key,
                        confirmation_token=DROP_GENERATION_CONFIRMATION_TOKEN,
                        operator_approved=True,
                    )

                with pytest.raises(PartitionLifecycleError, match="already covered"):
                    await provision_generation(
                        conn, id_start=7_472_050, row_span=100, activate=False
                    )

                seq_before = (await read_sequence_cursor(conn)).next_id
                active_key = active.generation_key
                await drop_eligible_generation(
                    conn,
                    closed.generation_key,
                    confirmation_token=DROP_GENERATION_CONFIRMATION_TOKEN,
                    operator_approved=True,
                )
                verify = await post_drop_verification(
                    conn,
                    dropped_generation_key=closed.generation_key,
                    expected_active_key=active_key,
                    sequence_next_id_before=seq_before,
                )
                assert verify["partition_absent"] is True
                await ensure_writable_cover(conn)
                await _insert_n(conn, 3)
                assert (await read_sequence_cursor(conn)).next_id == seq_before + 3

                status = await build_operator_status(
                    conn,
                    free_disk_bytes=int(5.06 * 1024**3),
                )
                assert status.read_only is True
                # Capacity at ~5.06 GiB is not continuously feasible.
                assert status.capacity["continuous_operation_feasible"] is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_archive_and_drop_failure_modes() -> None:
    database_url = require_test_database_url()

    async def run() -> None:
        await _reset_tiny(database_url, span=50)
        engine = create_engine(database_url)
        try:
            async with engine.begin() as conn:
                await _insert_n(conn, 50)
                await provision_generation(
                    conn, id_start=7_471_963, row_span=50, activate=False
                )
                await rotate_active_if_sequence_crossed(
                    conn, reason="fill complete for failure probes"
                )
                # Sequence already at successor start after 50 inserts from 7471913.
                await recover_active_from_sequence(conn)
                closed = next(
                    g
                    for g in await list_generations(conn)
                    if g.state == GenerationState.CLOSED_UNARCHIVED
                )

                with pytest.raises(PartitionLifecycleError, match="B2 unavailable"):
                    await archive_closed_generation(
                        conn,
                        closed.generation_key,
                        MockGenerationArchiveBackend(fail_upload=True),
                    )
                assert (
                    next(
                        g
                        for g in await list_generations(conn)
                        if g.generation_key == closed.generation_key
                    ).state
                    == GenerationState.ARCHIVE_FAILED
                )

                with pytest.raises(PartitionLifecycleError, match="checksums"):
                    await archive_closed_generation(
                        conn,
                        closed.generation_key,
                        MockGenerationArchiveBackend(fail_checksum=True),
                    )
                assert (
                    next(
                        g
                        for g in await list_generations(conn)
                        if g.generation_key == closed.generation_key
                    ).state
                    == GenerationState.VERIFY_FAILED
                )

                # Crash during archive: durable ARCHIVING then resume CLOSED.
                await mark_generation_state(
                    conn, closed.generation_key, GenerationState.CLOSED_UNARCHIVED
                )
                await mark_generation_state(
                    conn, closed.generation_key, GenerationState.ARCHIVING
                )
                await mark_generation_state(
                    conn, closed.generation_key, GenerationState.CLOSED_UNARCHIVED
                )

                # DROP before verification rejected.
                with pytest.raises(PartitionLifecycleError, match="DROP_ELIGIBLE"):
                    await drop_eligible_generation(
                        conn,
                        closed.generation_key,
                        confirmation_token=DROP_GENERATION_CONFIRMATION_TOKEN,
                        operator_approved=True,
                    )
                with pytest.raises(PartitionLifecycleError, match="invalid DROP"):
                    await drop_eligible_generation(
                        conn,
                        closed.generation_key,
                        confirmation_token="WRONG",
                        operator_approved=True,
                    )

                # Archive completed but metadata update crashed: VERIFIED without
                # DROP_ELIGIBLE still refuses DROP; re-run mark path via archive.
                evidence = await archive_closed_generation(
                    conn,
                    closed.generation_key,
                    MockGenerationArchiveBackend(),
                    mark_drop_eligible_on_success=False,
                )
                assert evidence.evidence_sha256
                verified = next(
                    g
                    for g in await list_generations(conn)
                    if g.generation_key == closed.generation_key
                )
                assert verified.state == GenerationState.VERIFIED
                with pytest.raises(PartitionLifecycleError, match="DROP_ELIGIBLE"):
                    await drop_eligible_generation(
                        conn,
                        closed.generation_key,
                        confirmation_token=DROP_GENERATION_CONFIRMATION_TOKEN,
                        operator_approved=True,
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_missing_successor_and_unexpected_partition_fail_closed() -> None:
    database_url = require_test_database_url()

    async def run() -> None:
        await _reset_tiny(database_url, span=20)
        engine = create_engine(database_url)
        try:
            async with engine.begin() as conn:
                await _insert_n(conn, 19)
                # One slot left; no successor provisioned.
                await ensure_writable_cover(conn, headroom_rows=1)
                await _insert_n(conn, 1)
                with pytest.raises(
                    PartitionLifecycleError,
                    match="headroom exhausted|no generation covers",
                ):
                    await ensure_writable_cover(conn, headroom_rows=1)
                with pytest.raises(PartitionLifecycleError, match="no provisioned successor"):
                    await rotate_active_if_sequence_crossed(conn)

                active = next(
                    g for g in await list_generations(conn) if g.state == GenerationState.ACTIVE
                )
                await conn.execute(
                    text(
                        f"""
                        CREATE TABLE market_events_g_unexpected
                        PARTITION OF market_events
                        FOR VALUES FROM ({active.id_end + 10_000})
                        TO ({active.id_end + 10_100})
                        """
                    )
                )
                with pytest.raises(PartitionLifecycleError, match="unexpected partitions"):
                    await assert_catalog_metadata_consistent(conn)
                await conn.execute(text("DROP TABLE market_events_g_unexpected"))
                await assert_catalog_metadata_consistent(conn)

                status = await build_operator_status(
                    conn,
                    free_disk_bytes=2 * 1024**3,
                )
                # Disk below floor AND missing successor near boundary: cover
                # urgency outranks capacity STOP so operators see the partition risk.
                assert status.action == OperatorAction.COVER_STOP_REQUIRED
                assert status.capacity["state"] == "STOP_REQUIRED"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_crash_recovery_activates_successor_when_sequence_crossed() -> None:
    database_url = require_test_database_url()

    async def run() -> None:
        await _reset_tiny(database_url, span=30)
        engine = create_engine(database_url)
        try:
            async with engine.begin() as conn:
                await provision_generation(
                    conn, id_start=7_471_943, row_span=30, activate=False
                )
                await _insert_n(conn, 30)
                # Simulate crash after inserts routed to successor but before
                # metadata rotation: ACTIVE still points at generation A.
                active = next(
                    g for g in await list_generations(conn) if g.state == GenerationState.ACTIVE
                )
                assert active.id_start == 7_471_913
                cursor = await read_sequence_cursor(conn)
                assert cursor.next_id == 7_471_943
                recovered = await recover_active_from_sequence(conn)
                assert recovered.id_start == 7_471_943
                closed = next(
                    g
                    for g in await list_generations(conn)
                    if g.generation_key == active.generation_key
                )
                assert closed.state == GenerationState.CLOSED_UNARCHIVED
                await ensure_writable_cover(conn)
                await _insert_n(conn, 2)
        finally:
            await engine.dispose()

    asyncio.run(run())
