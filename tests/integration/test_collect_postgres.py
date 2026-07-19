import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.integration.database import require_test_database_url
from trading_bot.collector import MarketCollector, MessageHandler
from trading_bot.storage.database import create_engine, create_session_factory
from trading_bot.storage.maintenance import DataMaintenance, ReplayFilter
from trading_bot.storage.models import MarketEvent
from trading_bot.storage.repository import EventRepository


class OneMessageStream:
    def __init__(self, message: dict[str, Any]) -> None:
        self._message = message
        self._handlers: dict[str, MessageHandler] = {}
        self.disconnected = False

    def on(self, topic: str, handler: MessageHandler) -> None:
        self._handlers[topic] = handler

    async def connect(self) -> None:
        return None

    async def subscribe(self, symbol: str, topics: Sequence[str]) -> None:
        assert symbol == "ETH/USDT-P"
        assert topics == ("trades",)

    async def wait_closed(self) -> None:
        await self._handlers["trades"](self._message)

    async def disconnect(self) -> None:
        self.disconnected = True


def test_collect_stream_writes_raw_event_to_postgres() -> None:
    database_url = require_test_database_url()

    marker = str(uuid4())
    exchange_timestamp_ms = int(datetime.now(UTC).timestamp() * 1000)
    payload = {
        "topic": "trades",
        "symbol": "ETH/USDT-P",
        "data": {
            "timestamp": exchange_timestamp_ms,
            "sequence": 901,
            "price": "3000",
            "e2e_marker": marker,
        },
    }

    async def run_check() -> None:
        engine = create_engine(database_url)
        factory = create_session_factory(engine)
        stream = OneMessageStream(payload)
        collector = MarketCollector(
            symbol="ETH/USDT-P",
            topics=("trades",),
            stream=stream,
            sink=EventRepository(factory),
        )
        maintenance = DataMaintenance(factory)
        try:
            with pytest.raises(ConnectionError, match="stopped unexpectedly"):
                await collector.run()

            async with factory() as session:
                event = (
                    await session.execute(
                        select(MarketEvent).where(
                            MarketEvent.payload["data"]["e2e_marker"].as_string() == marker
                        )
                    )
                ).scalar_one()

            assert stream.disconnected is True
            assert event.source == "hibachi_ws"
            assert event.event_type == "trades"
            assert event.symbol == "ETH/USDT-P"
            assert event.sequence == 901
            assert event.payload == payload
            assert event.latency_ms is not None
            assert event.latency_ms < 5_000

            replay = await maintenance.replay(
                ReplayFilter(
                    symbol="ETH/USDT-P",
                    event_types=("trades",),
                    limit=100,
                )
            )
            assert [item.id for item in replay] == sorted(item.id for item in replay)
            assert any(item.payload == payload for item in replay)

            metrics = await maintenance.daily_quality(
                datetime.now(UTC).date(),
                symbol="ETH/USDT-P",
            )
            trades = next(metric for metric in metrics if metric.event_type == "trades")
            assert trades.total_events >= 1
            assert trades.missing_exchange_time == 0
        finally:
            await engine.dispose()

    asyncio.run(run_check())
