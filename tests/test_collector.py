import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from trading_bot.collector import (
    MarketCollector,
    MessageHandler,
    extract_exchange_time,
    extract_sequence,
)
from trading_bot.storage.repository import MarketEventInput


class MemorySink:
    def __init__(self) -> None:
        self.events: list[MarketEventInput] = []

    async def append_market_event(self, event: MarketEventInput) -> None:
        self.events.append(event)


class FakeStream:
    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message
        self.handlers: dict[str, MessageHandler] = {}
        self.connected = False
        self.disconnected = False

    def on(self, topic: str, handler: MessageHandler) -> None:
        self.handlers[topic] = handler

    async def connect(self) -> None:
        self.connected = True

    async def subscribe(self, symbol: str, topics: Sequence[str]) -> None:
        assert symbol == "ETH/USDT-P"
        assert topics == ("trades",)

    async def wait_closed(self) -> None:
        await self.handlers["trades"](self.message)

    async def disconnect(self) -> None:
        self.disconnected = True


def test_timestamp_and_sequence_are_extracted_from_nested_data() -> None:
    payload = {"data": {"timestamp": 1_720_000_000_000, "sequence": "42"}}
    assert extract_exchange_time(payload) == datetime.fromtimestamp(1_720_000_000, tz=UTC)
    assert extract_sequence(payload) == 42


def test_invalid_metadata_is_left_unknown() -> None:
    payload = {"timestamp": "not-a-time", "sequence": True}
    assert extract_exchange_time(payload) is None
    assert extract_sequence(payload) is None


def test_collector_persists_raw_message_and_fails_on_closed_stream() -> None:
    payload = {
        "topic": "trades",
        "symbol": "ETH/USDT-P",
        "data": {"timestamp": 1_720_000_000_000, "sequence": 9, "price": "3000"},
    }
    stream = FakeStream(payload)
    sink = MemorySink()
    collector = MarketCollector(
        symbol="ETH/USDT-P",
        topics=("trades",),
        stream=stream,
        sink=sink,
    )

    with pytest.raises(ConnectionError, match="stopped unexpectedly"):
        asyncio.run(collector.run())

    assert stream.connected is True
    assert stream.disconnected is True
    assert len(sink.events) == 1
    assert sink.events[0].payload == payload
    assert sink.events[0].sequence == 9
