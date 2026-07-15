import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from trading_bot.collector import (
    CollectorSupervisor,
    MarketCollector,
    MessageHandler,
    SequenceDesyncError,
    extract_exchange_time,
    extract_sequence,
)
from trading_bot.storage.repository import MarketEventInput


class MemorySink:
    def __init__(self) -> None:
        self.events: list[MarketEventInput] = []
        self.system_events: list[dict[str, Any]] = []

    async def append_market_event(self, event: MarketEventInput) -> None:
        self.events.append(event)

    async def append_system_event(self, **event: Any) -> None:
        self.system_events.append(event)


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


def test_hibachi_timestamp_ms_is_extracted() -> None:
    payload = {"timestamp_ms": 1_720_000_000_000}
    assert extract_exchange_time(payload) == datetime.fromtimestamp(1_720_000_000, tz=UTC)


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


def test_orderbook_sequence_gap_is_persisted_and_fails_closed() -> None:
    messages = [
        {"topic": "orderbook", "data": {"type": "snapshot", "sequence": 100}},
        {"topic": "orderbook", "data": {"sequence": 102}},
    ]

    class OrderbookStream(FakeStream):
        async def subscribe(self, symbol: str, topics: Sequence[str]) -> None:
            assert symbol == "ETH/USDT-P"
            assert topics == ("orderbook",)

        async def wait_closed(self) -> None:
            for message in messages:
                await self.handlers["orderbook"](message)

    sink = MemorySink()
    collector = MarketCollector(
        symbol="ETH/USDT-P",
        topics=("orderbook",),
        stream=OrderbookStream(messages[0]),
        sink=sink,
    )

    with pytest.raises(SequenceDesyncError, match="previous=100, received=102"):
        asyncio.run(collector.run())

    assert [event.sequence for event in sink.events] == [100, 102]
    assert sink.system_events[0]["event_type"] == "DESYNC"
    assert sink.system_events[0]["details"]["reason"] == "sequence_gap"


def test_orderbook_update_before_snapshot_fails_closed() -> None:
    payload = {
        "topic": "orderbook",
        "messageType": "Update",
        "timestamp_ms": 1_720_000_000_000,
        "data": {"bid": {"levels": []}, "ask": {"levels": []}},
    }

    class OrderbookStream(FakeStream):
        async def subscribe(self, symbol: str, topics: Sequence[str]) -> None:
            assert symbol == "ETH/USDT-P"
            assert topics == ("orderbook",)

        async def wait_closed(self) -> None:
            await self.handlers["orderbook"](self.message)

    sink = MemorySink()
    collector = MarketCollector(
        symbol="ETH/USDT-P",
        topics=("orderbook",),
        stream=OrderbookStream(payload),
        sink=sink,
    )

    with pytest.raises(SequenceDesyncError, match="snapshot missing"):
        asyncio.run(collector.run())

    assert sink.system_events[0]["details"]["reason"] == "missing_snapshot"


def test_supervisor_retries_with_backoff_then_halts() -> None:
    sink = MemorySink()
    delays: list[float] = []
    collectors_created = 0

    class FailedCollector:
        async def run(self) -> None:
            raise ConnectionError("stream failed")

    def collector_factory() -> Any:
        nonlocal collectors_created
        collectors_created += 1
        return FailedCollector()

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    supervisor = CollectorSupervisor(
        collector_factory=collector_factory,
        sink=sink,
        max_attempts=3,
        initial_delay=0.5,
        max_delay=1.0,
        sleeper=sleeper,
    )

    with pytest.raises(ConnectionError, match="stream failed"):
        asyncio.run(supervisor.run())

    assert collectors_created == 3
    assert delays == [0.5, 1.0]
    assert [event["event_type"] for event in sink.system_events] == [
        "DEGRADED",
        "DEGRADED",
        "HALTED",
    ]
