import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from trading_bot.storage.repository import EventRepository, MarketEventInput

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class MarketStream(Protocol):
    def on(self, topic: str, handler: MessageHandler) -> None: ...

    async def connect(self) -> None: ...

    async def subscribe(self, symbol: str, topics: Sequence[str]) -> None: ...

    async def wait_closed(self) -> None: ...

    async def disconnect(self) -> None: ...


class EventSink(Protocol):
    async def append_market_event(self, event: MarketEventInput) -> None: ...


def _message_value(payload: dict[str, Any], names: tuple[str, ...]) -> Any:
    containers = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        containers.append(data)
    for container in containers:
        for name in names:
            if name in container:
                return container[name]
    return None


def extract_sequence(payload: dict[str, Any]) -> int | None:
    raw = _message_value(payload, ("sequence", "seq", "sequenceNumber"))
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def extract_exchange_time(payload: dict[str, Any]) -> datetime | None:
    raw = _message_value(
        payload,
        ("timestamp", "timestampMs", "ts", "time", "createdAt"),
    )
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        try:
            if raw.isdigit():
                raw = int(raw)
            else:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None
    if isinstance(raw, int | float):
        value = float(raw)
        magnitude = abs(value)
        if magnitude >= 1e18:
            value /= 1e9
        elif magnitude >= 1e15:
            value /= 1e6
        elif magnitude >= 1e12:
            value /= 1e3
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None


class HibachiMarketStream:
    """Lifecycle wrapper for the pinned official Hibachi market WebSocket client."""

    def __init__(self, data_api_url: str) -> None:
        from hibachi_xyz import HibachiWSMarketClient  # type: ignore[import-untyped]
        from hibachi_xyz.executors.aiohttp import (  # type: ignore[import-untyped]
            AiohttpWsExecutor,
        )

        self._executor = AiohttpWsExecutor()
        self._client = HibachiWSMarketClient(
            api_endpoint=data_api_url,
            executor=self._executor,
        )

    def on(self, topic: str, handler: MessageHandler) -> None:
        self._client.on(topic, handler)

    async def connect(self) -> None:
        await self._client.connect()

    async def subscribe(self, symbol: str, topics: Sequence[str]) -> None:
        from hibachi_xyz import (
            WebSocketSubscription,
            WebSocketSubscriptionTopic,
        )

        subscriptions = [
            WebSocketSubscription(symbol=symbol, topic=WebSocketSubscriptionTopic(topic))
            for topic in topics
        ]
        await self._client.subscribe(subscriptions)

    async def wait_closed(self) -> None:
        receive_task = self._client._receive_task  # noqa: SLF001
        if receive_task is None:
            raise RuntimeError("Hibachi WebSocket receive loop was not started")
        await receive_task

    async def disconnect(self) -> None:
        try:
            await self._client.disconnect()
        finally:
            await self._executor.close()


class MarketCollector:
    """Persists raw messages and fails closed if the stream terminates."""

    def __init__(
        self,
        *,
        symbol: str,
        topics: Sequence[str],
        stream: MarketStream,
        sink: EventSink,
    ) -> None:
        self._symbol = symbol
        self._topics = tuple(topics)
        self._stream = stream
        self._sink = sink

    async def _handle_message(self, payload: dict[str, Any]) -> None:
        received_at = datetime.now(UTC)
        exchange_at = extract_exchange_time(payload)
        latency_ms = None
        if exchange_at is not None:
            latency_ms = max(0.0, (received_at - exchange_at).total_seconds() * 1000)
        await self._sink.append_market_event(
            MarketEventInput(
                received_at=received_at,
                exchange_at=exchange_at,
                source="hibachi_ws",
                event_type=str(payload.get("topic", "unknown")),
                symbol=str(payload.get("symbol", self._symbol)),
                sequence=extract_sequence(payload),
                latency_ms=latency_ms,
                payload=payload,
            )
        )

    async def run(self) -> None:
        for topic in self._topics:
            self._stream.on(topic, self._handle_message)
        await self._stream.connect()
        try:
            await self._stream.subscribe(self._symbol, self._topics)
            await self._stream.wait_closed()
            raise ConnectionError("Hibachi market WebSocket stopped unexpectedly")
        except asyncio.CancelledError:
            raise
        finally:
            await self._stream.disconnect()


def build_collector(
    *,
    symbol: str,
    topics: Sequence[str],
    data_api_url: str,
    repository: EventRepository,
) -> MarketCollector:
    return MarketCollector(
        symbol=symbol,
        topics=topics,
        stream=HibachiMarketStream(data_api_url),
        sink=repository,
    )
