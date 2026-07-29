import asyncio
import json
import os
import time
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pyarrow.parquet as pq  # type: ignore[import-untyped]
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text

from tests.integration.database import require_test_database_url
from trading_bot.research.exporter import VersionedDatasetExporter
from trading_bot.storage.database import create_engine, create_session_factory
from trading_bot.storage.models import MarketEvent
from trading_bot.storage.repository import EventRepository, MarketEventInput

DEFAULT_COPY_ROWS = 100_000
MAX_COPY_ROWS = 2_500_000


def _alembic_config() -> Config:
    root = Path(__file__).parents[2]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.cmd_opts = Namespace(x=["database-role=test"])
    return config


def test_raw_v2_upgrade_downgrade_allows_concurrent_legacy_writes() -> None:
    database_url = require_test_database_url()
    copy_rows = int(os.getenv("RAW_V2_MIGRATION_COPY_ROWS", str(DEFAULT_COPY_ROWS)))
    assert 1 <= copy_rows <= MAX_COPY_ROWS
    marker = str(uuid4())

    async def run_check() -> None:
        engine = create_engine(database_url)
        config = _alembic_config()
        stop = asyncio.Event()
        writes = 0
        writer_latencies_ms: list[float] = []

        async def writer() -> None:
            nonlocal writes
            while not stop.is_set():
                started = time.perf_counter()
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            """
                            INSERT INTO market_events (
                                received_at, source, event_type, symbol, payload
                            ) VALUES (
                                now(), 'migration_probe', 'probe', 'ETH/USDT-P',
                                    jsonb_build_object('marker', CAST(:marker AS text))
                            )
                            """
                        ),
                        {"marker": marker},
                    )
                writes += 1
                writer_latencies_ms.append((time.perf_counter() - started) * 1000)
                await asyncio.sleep(0.01)

        try:
            downgrade_started = time.perf_counter()
            await asyncio.to_thread(command.downgrade, config, "20260715_0001")
            downgrade_seconds = time.perf_counter() - downgrade_started
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO market_events (
                            received_at, source, event_type, symbol, payload
                        )
                        SELECT
                            now(), 'migration_probe', 'probe', 'ETH/USDT-P',
                                jsonb_build_object('marker', CAST(:marker AS text))
                            FROM generate_series(1, CAST(:copy_rows AS integer))
                        """
                    ),
                    {"marker": marker, "copy_rows": copy_rows},
                )
            writer_task = asyncio.create_task(writer())
            await asyncio.sleep(0.05)
            blocker_connection = await engine.connect()
            blocker_transaction = await blocker_connection.begin()
            await blocker_connection.execute(
                text(
                    """
                    INSERT INTO market_events (
                        received_at, source, event_type, symbol, payload
                    ) VALUES (
                        now(), 'migration_probe', 'probe', 'ETH/USDT-P',
                            jsonb_build_object('marker', CAST(:marker AS text))
                    )
                    """
                ),
                {"marker": marker},
            )
            migration_started = time.perf_counter()
            migration_task = asyncio.create_task(
                asyncio.to_thread(command.upgrade, config, "head")
            )
            waiting_lock_seen = False
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not migration_task.done():
                async with engine.connect() as connection:
                    waiting_lock_seen = bool(
                        await connection.scalar(
                            text(
                                """
                                SELECT EXISTS (
                                    SELECT 1
                                    FROM pg_locks
                                    WHERE relation = 'market_events'::regclass
                                      AND mode = 'AccessExclusiveLock'
                                      AND NOT granted
                                )
                                """
                            )
                        )
                    )
                if waiting_lock_seen:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            await blocker_transaction.commit()
            await blocker_connection.close()
            await migration_task
            migration_seconds = time.perf_counter() - migration_started
            await asyncio.sleep(0.05)
            stop.set()
            await writer_task
            async with engine.begin() as connection:
                versions = (
                    await connection.execute(
                        text(
                            """
                            SELECT DISTINCT schema_version
                            FROM market_events
                            WHERE source = 'migration_probe'
                              AND payload->>'marker' = :marker
                            """
                        ),
                        {"marker": marker},
                    )
                ).scalars().all()
                await connection.execute(
                    text(
                        """
                        DELETE FROM market_events
                        WHERE source = 'migration_probe'
                          AND payload->>'marker' = :marker
                        """
                    ),
                    {"marker": marker},
                )
            assert writes >= 2
            assert versions == [1]
            assert waiting_lock_seen is True
            assert migration_seconds < 5.0
            assert max(writer_latencies_ms) < 5_000
            print(
                "raw_v2_migration_metrics="
                + json.dumps(
                    {
                        "rows": copy_rows,
                        "postgres_major": 16,
                        "upgrade_seconds": round(migration_seconds, 6),
                        "downgrade_seconds": round(downgrade_seconds, 6),
                        "writer_max_pause_ms": round(max(writer_latencies_ms), 3),
                        "waiting_access_exclusive_seen": waiting_lock_seen,
                    },
                    sort_keys=True,
                )
            )
        finally:
            stop.set()
            await asyncio.to_thread(command.upgrade, config, "head")
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        DELETE FROM market_events
                        WHERE source = 'migration_probe'
                          AND payload->>'marker' = :marker
                        """
                    ),
                    {"marker": marker},
                )
            await engine.dispose()

    asyncio.run(run_check())


def test_raw_v1_v2_database_and_export_round_trip(tmp_path: Path) -> None:
    database_url = require_test_database_url()
    symbol = f"TST/{str(uuid4())[:8]}-P"
    connection_id = str(uuid4())
    start = datetime.now(UTC)

    async def run_check() -> None:
        engine = create_engine(database_url)
        factory = create_session_factory(engine)
        repository = EventRepository(factory)
        try:
            await repository.append_market_event(
                MarketEventInput(
                    received_at=start,
                    exchange_at=None,
                    source="hibachi_ws",
                    event_type="trades",
                    symbol=symbol,
                    sequence=None,
                    latency_ms=None,
                    payload={"topic": "trades"},
                )
            )
            await repository.append_market_event(
                MarketEventInput(
                    received_at=start + timedelta(microseconds=1),
                    exchange_at=None,
                    source="hibachi_ws",
                    event_type="trades",
                    symbol=symbol,
                    sequence=7,
                    latency_ms=None,
                    payload={"topic": "trades"},
                    connection_id=connection_id,
                    local_sequence=1,
                    exchange_sequence=7,
                    schema_version=2,
                )
            )
            async with factory() as session:
                events = list(
                    (
                        await session.scalars(
                            select(MarketEvent)
                            .where(MarketEvent.symbol == symbol)
                            .order_by(MarketEvent.id)
                        )
                    ).all()
                )
            assert [event.schema_version for event in events] == [1, 2]
            assert events[0].connection_id is None
            assert events[1].connection_id == connection_id

            dataset_dir = await VersionedDatasetExporter(factory).export(
                output_root=tmp_path,
                symbol=symbol,
                version="raw-v1-v2",
                start=start - timedelta(seconds=1),
                end=start + timedelta(seconds=1),
            )
            table = pq.read_table(dataset_dir / f"{symbol.replace('/', '-')}.parquet")
            assert table.column("raw_event_id").to_pylist() == [
                events[0].id,
                events[1].id,
            ]
            assert table.column("raw_schema_version").to_pylist() == [1, 2]
            assert table.column("connection_id").to_pylist() == [None, connection_id]
            assert table.column("local_sequence").to_pylist() == [None, 1]
            assert table.column("exchange_sequence").to_pylist() == [None, 7]
        finally:
            async with factory.begin() as session:
                await session.execute(delete(MarketEvent).where(MarketEvent.symbol == symbol))
            await engine.dispose()

    asyncio.run(run_check())
