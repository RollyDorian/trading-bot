import asyncio
import random
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

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

    async def append_system_event(
        self,
        *,
        severity: str,
        event_type: str,
        component: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None: ...


class SequenceDesyncError(ConnectionError):
    pass


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
        ("timestamp", "timestampMs", "timestamp_ms", "ts", "time", "createdAt"),
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
            api_endpoint=data_api_url.rstrip("/"),
            executor=self._executor,
        )

    def on(self, topic: str, handler: MessageHandler) -> None:
        self._client.on(topic, handler)

    async def connect(self) -> None:
        await asyncio.wait_for(self._client.connect(), timeout=15.0)

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
        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling():
            raise asyncio.CancelledError

    async def disconnect(self) -> None:
        try:
            await asyncio.wait_for(self._client.disconnect(), timeout=10.0)
        finally:
            await asyncio.wait_for(self._executor.close(), timeout=10.0)


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
        self._last_sequences: dict[str, int] = {}
        self._orderbook_snapshot_seen = False

    @staticmethod
    def _is_snapshot(payload: dict[str, Any]) -> bool:
        marker = _message_value(
            payload,
            ("messageType", "type", "event", "action", "isSnapshot"),
        )
        if marker is True:
            return True
        return isinstance(marker, str) and marker.lower() in {"snapshot", "initial", "full"}

    async def _validate_sequence(self, payload: dict[str, Any]) -> None:
        topic = str(payload.get("topic", "unknown"))
        if topic != "orderbook":
            return
        sequence = extract_sequence(payload)
        if self._is_snapshot(payload):
            self._orderbook_snapshot_seen = True
            if sequence is not None:
                self._last_sequences[topic] = sequence
            return
        if not self._orderbook_snapshot_seen:
            await self._sink.append_system_event(
                severity="ERROR",
                event_type="DESYNC",
                component="market_collector",
                message="Order book update received before snapshot",
                details={
                    "symbol": self._symbol,
                    "topic": topic,
                    "reason": "missing_snapshot",
                },
            )
            raise SequenceDesyncError("Order book desynchronized: snapshot missing")
        if sequence is None:
            return
        previous = self._last_sequences.get(topic)
        self._last_sequences[topic] = sequence
        if previous is None or sequence == previous + 1:
            return
        reason = "sequence_gap" if sequence > previous + 1 else "sequence_regression"
        await self._sink.append_system_event(
            severity="ERROR",
            event_type="DESYNC",
            component="market_collector",
            message=f"Order book {reason.replace('_', ' ')} detected",
            details={
                "symbol": self._symbol,
                "topic": topic,
                "previous_sequence": previous,
                "received_sequence": sequence,
                "reason": reason,
            },
        )
        raise SequenceDesyncError(
            f"Order book desynchronized: previous={previous}, received={sequence}"
        )

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
        await self._validate_sequence(payload)

    async def run(self) -> None:
        for topic in self._topics:
            self._stream.on(topic, self._handle_message)
        try:
            await self._stream.connect()
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


CollectorFactory = Callable[[], MarketCollector]
Sleeper = Callable[[float], Awaitable[None]]
Jitter = Callable[[float, float], float]
Clock = Callable[[], float]

_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|authorization|token|secret|password)\s*[:=]\s*\S+"),
    re.compile(r"(?i)bearer\s+\S+"),
)
_URI_PATTERN = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://\S+")
_SAFE_HOST_PATTERN = re.compile(r"^[a-zA-Z0-9.:-]+$")
_SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?i)(?:api[_-]?key|authorization|access[_-]?token|token|secret|password)"
)


def _sanitize_uri(match: re.Match[str]) -> str:
    try:
        parsed = urlsplit(match.group(0))
        if not parsed.scheme or not parsed.hostname:
            return "[REDACTED_URI]"
        hostname = parsed.hostname
        if not _SAFE_HOST_PATTERN.fullmatch(hostname):
            return "[REDACTED_URI]"
        port = parsed.port
    except ValueError:
        return "[REDACTED_URI]"
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    authority = f"{display_host}:{port}" if port is not None else display_host
    safe_path = "/[PATH]" if parsed.path and parsed.path != "/" else parsed.path
    return f"{parsed.scheme.lower()}://{authority}{safe_path}"


def sanitize_error_text(value: str, *, limit: int = 500) -> str:
    message = value.replace("\r", " ").replace("\n", " ").strip()
    message = _URI_PATTERN.sub(_sanitize_uri, message)
    for pattern in _SECRET_PATTERNS:
        message = pattern.sub(
            lambda match: (
                f"{match.group(1)}[REDACTED]" if match.lastindex else "[REDACTED]"
            ),
            message,
        )
    return (message or "Error")[:limit]


def sanitize_error_data(value: Any, *, field: str | None = None) -> Any:
    if field is not None and _SENSITIVE_FIELD_PATTERN.fullmatch(field):
        return "[REDACTED]"
    if isinstance(value, str):
        return sanitize_error_text(value)
    if isinstance(value, dict):
        return {
            key: sanitize_error_data(item, field=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_error_data(item) for item in value]
    return value


def sanitize_error_message(error: BaseException, *, limit: int = 500) -> str:
    message = sanitize_error_text(str(error), limit=limit)
    return type(error).__name__ if message == "Error" else message


class CollectorSupervisor:
    """Restarts failed collectors with bounded exponential backoff."""

    def __init__(
        self,
        *,
        collector_factory: CollectorFactory,
        sink: EventSink,
        max_attempts: int,
        initial_delay: float,
        max_delay: float,
        sleeper: Sleeper = asyncio.sleep,
        jitter: Jitter = random.uniform,
        clock: Clock = time.monotonic,
        jitter_ratio: float = 0.2,
    ) -> None:
        self._collector_factory = collector_factory
        self._sink = sink
        self._max_attempts = max_attempts
        self._initial_delay = initial_delay
        self._max_delay = max_delay
        self._sleeper = sleeper
        self._jitter = jitter
        self._clock = clock
        self._jitter_ratio = jitter_ratio

    def _retry_delay(self, attempt: int) -> float:
        base = min(self._initial_delay * (2 ** (attempt - 1)), self._max_delay)
        spread = base * self._jitter_ratio
        return min(
            self._max_delay,
            max(self._initial_delay, self._jitter(base - spread, base + spread)),
        )

    def _failure_details(
        self,
        error: Exception,
        *,
        attempt: int,
        started_at: float,
        delay: float | None,
    ) -> dict[str, Any]:
        received_at = datetime.now(UTC)
        details: dict[str, Any] = {
            "event_kind": "collection_failure",
            "category": "collector_reconnect" if delay is not None else "collector_halt",
            "exception_class": type(error).__name__,
            "error_message": sanitize_error_message(error),
            "reconnect_attempt": attempt,
            "session_uptime_seconds": max(0.0, self._clock() - started_at),
            "receipt_timestamp": received_at.isoformat(),
        }
        if delay is not None:
            details["retry_delay_seconds"] = delay
            details["next_retry_timestamp"] = (received_at + timedelta(seconds=delay)).isoformat()
        return details

    async def run(self) -> None:
        attempt = 0
        while True:
            started_at = self._clock()
            try:
                await self._collector_factory().run()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                attempt += 1
                if attempt >= self._max_attempts:
                    await self._sink.append_system_event(
                        severity="CRITICAL",
                        event_type="HALTED",
                        component="collector_supervisor",
                        message="Market collection halted after repeated failures",
                        details=self._failure_details(
                            error, attempt=attempt, started_at=started_at, delay=None
                        ),
                    )
                    raise
                delay = self._retry_delay(attempt)
                await self._sink.append_system_event(
                    severity="WARNING",
                    event_type="DEGRADED",
                    component="collector_supervisor",
                    message="Market collection failed; reconnect scheduled",
                    details=self._failure_details(
                        error, attempt=attempt, started_at=started_at, delay=delay
                    ),
                )
                await self._sleeper(delay)


def build_supervisor(
    *,
    symbol: str,
    topics: Sequence[str],
    data_api_url: str,
    repository: EventRepository,
    max_attempts: int,
    initial_delay: float,
    max_delay: float,
) -> CollectorSupervisor:
    return CollectorSupervisor(
        collector_factory=lambda: build_collector(
            symbol=symbol,
            topics=topics,
            data_api_url=data_api_url,
            repository=repository,
        ),
        sink=repository,
        max_attempts=max_attempts,
        initial_delay=initial_delay,
        max_delay=max_delay,
    )
