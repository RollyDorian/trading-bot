import asyncio
import os
from argparse import Namespace
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from tests.integration.database import require_test_database_url
from trading_bot.storage.database import create_engine

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

        async def writer() -> None:
            nonlocal writes
            while not stop.is_set():
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            """
                            INSERT INTO market_events (
                                received_at, source, event_type, symbol, payload
                            ) VALUES (
                                now(), 'migration_probe', 'probe', 'ETH/USDT-P',
                                jsonb_build_object('marker', :marker)
                            )
                            """
                        ),
                        {"marker": marker},
                    )
                writes += 1
                await asyncio.sleep(0.01)

        try:
            await asyncio.to_thread(command.downgrade, config, "20260715_0001")
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO market_events (
                            received_at, source, event_type, symbol, payload
                        )
                        SELECT
                            now(), 'migration_probe', 'probe', 'ETH/USDT-P',
                            jsonb_build_object('marker', :marker)
                        FROM generate_series(1, :copy_rows)
                        """
                    ),
                    {"marker": marker, "copy_rows": copy_rows},
                )
            writer_task = asyncio.create_task(writer())
            await asyncio.sleep(0.05)
            await asyncio.to_thread(command.upgrade, config, "head")
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
