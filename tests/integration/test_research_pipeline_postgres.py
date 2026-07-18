import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from trading_bot.research.dataset import DatasetExporter, validate_manifest
from trading_bot.research.quality import validate_dataset
from trading_bot.research.replay import BaselineConfig, CostConfig, replay_dataset
from trading_bot.storage.database import create_engine, create_session_factory
from trading_bot.storage.repository import EventRepository, MarketEventInput


@pytest.mark.asyncio
async def test_postgres_export_then_offline_replay(tmp_path: Path) -> None:
    database_url = os.getenv("DATABASE_URL")
    if database_url is None:
        pytest.skip("DATABASE_URL is required for the PostgreSQL integration check")

    engine = create_engine(database_url)
    factory = create_session_factory(engine)
    repository = EventRepository(factory)
    marker = str(uuid4())
    start = datetime.now(UTC).replace(microsecond=0)
    try:
        for index in range(12):
            timestamp = start + timedelta(seconds=index)
            await repository.append_market_event(
                MarketEventInput(
                    received_at=timestamp,
                    exchange_at=timestamp,
                    source="integration_fixture",
                    event_type="trades",
                    symbol="ETH/USDT-P",
                    sequence=index + 1,
                    latency_ms=0.0,
                    payload={
                        "topic": "trades",
                        "price": 100 + index,
                        "quantity": 1,
                        "research_marker": marker,
                    },
                )
            )
        dataset_dir = await DatasetExporter(factory).export(
            symbol="ETH/USDT-P",
            start=start,
            end=start + timedelta(seconds=12),
            output_root=tmp_path,
        )
        manifest = validate_manifest(dataset_dir)
        quality = validate_dataset(dataset_dir)
        report = replay_dataset(
            dataset_dir,
            signal_config=BaselineConfig(
                lookback_seconds=2,
                return_threshold_bps=1,
                holding_seconds=2,
                cooldown_seconds=1,
            ),
            cost_config=CostConfig(execution_delay_seconds=0),
        )
        assert manifest["row_counts"]["events"] == 12
        assert quality["status"] == "valid"
        assert report["events"] == 12
        assert report["candles"] == 12
        assert report["signals"] > 0
        assert report["net_pnl"] < report["gross_pnl"]
    finally:
        await engine.dispose()
