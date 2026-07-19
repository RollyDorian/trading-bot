from collections.abc import Sequence
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from tests.integration.database import require_test_database_url
from trading_bot.collector import MarketCollector, MessageHandler
from trading_bot.storage.database import create_engine, create_session_factory
from trading_bot.storage.models import MarketEvent
from trading_bot.storage.repository import EventRepository


class ConstraintFailureStream:
    def __init__(self, message: dict[str, Any]) -> None:
        self._message = message
        self._handler: MessageHandler | None = None
        self.disconnected = False

    def on(self, topic: str, handler: MessageHandler) -> None:
        assert topic == "trades"
        self._handler = handler

    async def connect(self) -> None:
        return None

    async def subscribe(self, symbol: str, topics: Sequence[str]) -> None:
        assert symbol == "ETH/USDT-P"
        assert topics == ("trades",)

    async def wait_closed(self) -> None:
        assert self._handler is not None
        await self._handler(self._message)

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.mark.asyncio
async def test_postgres_constraint_failure_propagates_without_partial_row() -> None:
    database_url = require_test_database_url()

    marker = str(uuid4())
    payload = {
        "topic": "trades",
        "symbol": "X" * 33,
        "sequence": 1,
        "soak_marker": marker,
    }
    engine = create_engine(database_url)
    factory = create_session_factory(engine)
    stream = ConstraintFailureStream(payload)
    collector = MarketCollector(
        symbol="ETH/USDT-P",
        topics=("trades",),
        stream=stream,
        sink=EventRepository(factory),
    )
    try:
        with pytest.raises(DBAPIError):
            await collector.run()

        async with factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(MarketEvent).where(
                            MarketEvent.payload["soak_marker"].as_string() == marker
                        )
                    )
                ).all()
            )

        assert rows == []
        assert stream.disconnected is True
    finally:
        await engine.dispose()
