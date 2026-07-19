from collections.abc import Sequence
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.integration.database import require_test_database_url
from trading_bot.collector import MarketCollector, MessageHandler, SequenceDesyncError
from trading_bot.storage.database import create_engine, create_session_factory
from trading_bot.storage.models import MarketEvent, SystemEvent
from trading_bot.storage.repository import EventRepository


class SequencedOrderbookStream:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = messages
        self._handler: MessageHandler | None = None
        self.disconnected = False

    def on(self, topic: str, handler: MessageHandler) -> None:
        assert topic == "orderbook"
        self._handler = handler

    async def connect(self) -> None:
        return None

    async def subscribe(self, symbol: str, topics: Sequence[str]) -> None:
        assert symbol == "ETH/USDT-P"
        assert topics == ("orderbook",)

    async def wait_closed(self) -> None:
        assert self._handler is not None
        for message in self._messages:
            await self._handler(message)

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.mark.asyncio
async def test_sequence_gap_records_desync_and_halts_collection() -> None:
    database_url = require_test_database_url()

    marker = str(uuid4())
    first_sequence = uuid4().int % 1_000_000_000 + 10_000
    messages = [
        {
            "topic": "orderbook",
            "type": "snapshot",
            "sequence": first_sequence,
            "soak_marker": marker,
        },
        {
            "topic": "orderbook",
            "sequence": first_sequence + 2,
            "soak_marker": marker,
        },
        {
            "topic": "orderbook",
            "sequence": first_sequence + 3,
            "soak_marker": marker,
        },
    ]
    engine = create_engine(database_url)
    factory = create_session_factory(engine)
    stream = SequencedOrderbookStream(messages)
    collector = MarketCollector(
        symbol="ETH/USDT-P",
        topics=("orderbook",),
        stream=stream,
        sink=EventRepository(factory),
    )
    try:
        with pytest.raises(
            SequenceDesyncError,
            match=rf"previous={first_sequence}, received={first_sequence + 2}",
        ):
            await collector.run()

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
            desync = (
                await session.scalars(
                    select(SystemEvent).where(
                        SystemEvent.event_type == "DESYNC",
                        SystemEvent.details["received_sequence"].as_integer()
                        == first_sequence + 2,
                    )
                )
            ).one()

        assert sequences == [first_sequence, first_sequence + 2]
        assert desync.severity == "ERROR"
        assert desync.details["reason"] == "sequence_gap"
        assert stream.disconnected is True
    finally:
        await engine.dispose()
