import os
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from trading_bot.collector import (
    CollectorSupervisor,
    MarketCollector,
    MessageHandler,
)
from trading_bot.storage.database import create_engine, create_session_factory
from trading_bot.storage.models import MarketEvent, SystemEvent
from trading_bot.storage.repository import EventRepository


class DisconnectingStream:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = messages
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
        for message in self._messages:
            await self._handlers["trades"](message)

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.mark.asyncio
async def test_reconnect_resumes_without_market_event_gaps() -> None:
    database_url = os.getenv("DATABASE_URL")
    if database_url is None:
        pytest.skip("DATABASE_URL is required for PostgreSQL soak tests")

    marker = str(uuid4())
    batches = (range(1, 101), range(101, 201))
    streams = [
        DisconnectingStream(
            [
                {
                    "topic": "trades",
                    "symbol": "ETH/USDT-P",
                    "sequence": sequence,
                    "soak_marker": marker,
                }
                for sequence in batch
            ]
        )
        for batch in batches
    ]
    engine = create_engine(database_url)
    factory = create_session_factory(engine)
    repository = EventRepository(factory)
    created = 0

    def collector_factory() -> MarketCollector:
        nonlocal created
        stream = streams[created]
        created += 1
        return MarketCollector(
            symbol="ETH/USDT-P",
            topics=("trades",),
            stream=stream,
            sink=repository,
        )

    async def no_delay(_: float) -> None:
        return None

    supervisor = CollectorSupervisor(
        collector_factory=collector_factory,
        sink=repository,
        max_attempts=2,
        initial_delay=0,
        max_delay=0,
        sleeper=no_delay,
    )
    try:
        with pytest.raises(ConnectionError, match="stopped unexpectedly"):
            await supervisor.run()

        async with factory() as session:
            sequences = list(
                (
                    await session.scalars(
                        select(MarketEvent.sequence)
                        .where(MarketEvent.payload["soak_marker"].as_string() == marker)
                        .order_by(MarketEvent.sequence)
                    )
                ).all()
            )
            lifecycle_events = list(
                (
                    await session.scalars(
                        select(SystemEvent).where(
                            SystemEvent.component == "collector_supervisor"
                        )
                    )
                ).all()
            )

        assert sequences == list(range(1, 201))
        assert created == 2
        assert all(stream.disconnected for stream in streams)
        assert any(event.event_type == "DEGRADED" for event in lifecycle_events)
        assert any(event.event_type == "HALTED" for event in lifecycle_events)
    finally:
        await engine.dispose()
