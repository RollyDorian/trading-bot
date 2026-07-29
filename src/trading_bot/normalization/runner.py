import asyncio
import hashlib
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.normalization.parsers import (
    PIPELINE_VERSION,
    BestQuoteRecord,
    FundingEstimateRecord,
    NormalizationFailure,
    ParsedRecord,
    ReferencePriceRecord,
    parse_market_event,
)
from trading_bot.normalization.resources import (
    ResourceDecision,
    ResourceLimits,
    ResourceProbe,
    SystemResourceProbe,
    evaluate_resources,
)
from trading_bot.storage.models import (
    BestQuote,
    FundingEstimate,
    MarketEvent,
    NormalizationError,
    NormalizerCheckpoint,
    OrderBookEvent,
    ReferencePrice,
)

DEFAULT_BATCH_SIZE = 100
MAX_BATCH_SIZE = 1000
MAX_BACKFILL_BATCHES = 100
DEFAULT_RESOURCE_LIMITS = ResourceLimits()


class ConcurrentNormalizerError(RuntimeError):
    pass


class ResourceStopError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BatchResult:
    raw_rows_read: int
    normalized_rows: int
    error_rows: int
    checkpoint: int
    raw_high_water: int
    lag_rows: int
    duration_ms: int
    resource_state: str
    resource_reason: str


def _lock_key(consumer: str) -> int:
    digest = hashlib.sha256(consumer.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], signed=True)


def _provenance_values(record: ParsedRecord) -> dict[str, Any]:
    return asdict(record.provenance)


def _model_insert(record: ParsedRecord) -> tuple[type[Any], dict[str, Any]]:
    values = _provenance_values(record)
    if isinstance(record, BestQuoteRecord):
        values.update(
            bid_price=record.bid_price,
            bid_size=record.bid_size,
            ask_price=record.ask_price,
            ask_size=record.ask_size,
        )
        return BestQuote, values
    if isinstance(record, ReferencePriceRecord):
        values.update(price_kind=record.price_kind, price=record.price)
        return ReferencePrice, values
    if isinstance(record, FundingEstimateRecord):
        values.update(
            estimated_rate=record.estimated_rate,
            next_funding_at=record.next_funding_at,
        )
        return FundingEstimate, values
    values.update(
        message_type=record.message_type,
        depth=record.depth,
        granularity=record.granularity,
        bids=[level.compact() for level in record.bids],
        asks=[level.compact() for level in record.asks],
        changed_level_count=len(record.bids) + len(record.asks),
    )
    return OrderBookEvent, values


async def _try_consumer_lock(session: AsyncSession, consumer: str) -> None:
    locked = await session.scalar(
        select(func.pg_try_advisory_xact_lock(_lock_key(consumer)))
    )
    if locked is not True:
        raise ConcurrentNormalizerError("normalizer consumer is already active")


async def _ensure_checkpoint(
    session: AsyncSession,
    *,
    consumer: str,
    initial_raw_event_id: int,
) -> NormalizerCheckpoint:
    await session.execute(
        insert(NormalizerCheckpoint)
        .values(
            consumer=consumer,
            last_raw_event_id=initial_raw_event_id,
            pipeline_version=PIPELINE_VERSION,
        )
        .on_conflict_do_nothing(index_elements=["consumer"])
    )
    checkpoint = await session.scalar(
        select(NormalizerCheckpoint)
        .where(NormalizerCheckpoint.consumer == consumer)
        .with_for_update()
    )
    if checkpoint is None:
        raise RuntimeError("normalizer checkpoint is unavailable")
    if checkpoint.pipeline_version != PIPELINE_VERSION:
        raise RuntimeError("normalizer checkpoint version is incompatible")
    return checkpoint


class RawEventNormalizer:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        consumer: str,
        batch_size: int = DEFAULT_BATCH_SIZE,
        capacity_path: Path = Path("/"),
        resource_limits: ResourceLimits | None = None,
        resource_probe: ResourceProbe | None = None,
        transient_retries: int = 2,
    ) -> None:
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise ValueError(f"batch size must be between 1 and {MAX_BATCH_SIZE}")
        if not 0 <= transient_retries <= 5:
            raise ValueError("transient retries must be between 0 and 5")
        self._factory = session_factory
        self._consumer = consumer
        self._batch_size = batch_size
        self._capacity_path = capacity_path
        self._resource_limits = resource_limits or DEFAULT_RESOURCE_LIMITS
        self._resource_probe = resource_probe or SystemResourceProbe()
        self._transient_retries = transient_retries

    def _resources(self) -> ResourceDecision:
        return evaluate_resources(
            probe=self._resource_probe,
            path=self._capacity_path,
            limits=self._resource_limits,
            batch_size=self._batch_size,
        )

    async def normalize_batch(
        self,
        *,
        initial_raw_event_id: int = 0,
        stop_raw_event_id: int | None = None,
    ) -> BatchResult:
        decision = self._resources()
        if decision.state == "stop":
            raise ResourceStopError(decision.reason)
        if decision.state == "pause":
            return BatchResult(0, 0, 0, initial_raw_event_id, 0, 0, 0, "pause", decision.reason)
        for attempt in range(self._transient_retries + 1):
            try:
                return await self._normalize_transaction(
                    decision,
                    initial_raw_event_id=initial_raw_event_id,
                    stop_raw_event_id=stop_raw_event_id,
                )
            except (OperationalError, DBAPIError) as error:
                if attempt >= self._transient_retries or not _is_transient(error):
                    raise
                await asyncio.sleep(0.25 * (2**attempt))
        raise AssertionError("bounded retry loop exhausted without result")

    async def _normalize_transaction(
        self,
        decision: ResourceDecision,
        *,
        initial_raw_event_id: int,
        stop_raw_event_id: int | None,
    ) -> BatchResult:
        started = time.monotonic()
        async with self._factory.begin() as session:
            await session.execute(text("SET LOCAL statement_timeout = '5s'"))
            await _try_consumer_lock(session, self._consumer)
            checkpoint = await _ensure_checkpoint(
                session,
                consumer=self._consumer,
                initial_raw_event_id=initial_raw_event_id,
            )
            high_water = int(
                await session.scalar(select(func.coalesce(func.max(MarketEvent.id), 0))) or 0
            )
            upper_bound = min(high_water, stop_raw_event_id or high_water)
            events = list(
                await session.scalars(
                    select(MarketEvent)
                    .where(
                        MarketEvent.id > checkpoint.last_raw_event_id,
                        MarketEvent.id <= upper_bound,
                    )
                    .order_by(MarketEvent.id)
                    .limit(self._batch_size)
                )
            )
            normalized = 0
            errors = 0
            for event in events:
                try:
                    record = parse_market_event(event)
                except NormalizationFailure as error:
                    await session.execute(
                        insert(NormalizationError)
                        .values(
                            raw_event_id=event.id,
                            pipeline_version=PIPELINE_VERSION,
                            event_type=event.event_type[:64],
                            error_code=error.code[:64],
                            error_detail=error.detail[:160],
                        )
                        .on_conflict_do_nothing(
                            index_elements=["raw_event_id", "pipeline_version"]
                        )
                    )
                    errors += 1
                else:
                    model, values = _model_insert(record)
                    result = await session.execute(
                        insert(model)
                        .values(**values)
                        .on_conflict_do_nothing(
                            index_elements=["raw_event_id", "pipeline_version"]
                        )
                    )
                    normalized += int(getattr(result, "rowcount", 0) or 0)
            if events:
                checkpoint.last_raw_event_id = events[-1].id
                checkpoint.updated_at = func.now()
            lag = max(0, high_water - checkpoint.last_raw_event_id)
            return BatchResult(
                raw_rows_read=len(events),
                normalized_rows=normalized,
                error_rows=errors,
                checkpoint=checkpoint.last_raw_event_id,
                raw_high_water=high_water,
                lag_rows=lag,
                duration_ms=int((time.monotonic() - started) * 1000),
                resource_state=decision.state,
                resource_reason=decision.reason,
            )

    async def backfill(
        self,
        *,
        max_batches: int = 1,
    ) -> list[BatchResult]:
        if not 1 <= max_batches <= MAX_BACKFILL_BATCHES:
            raise ValueError(f"max_batches must be between 1 and {MAX_BACKFILL_BATCHES}")
        async with self._factory() as session:
            stop_id = int(
                await session.scalar(select(func.coalesce(func.max(MarketEvent.id), 0))) or 0
            )
        results: list[BatchResult] = []
        for _ in range(max_batches):
            result = await self.normalize_batch(stop_raw_event_id=stop_id)
            results.append(result)
            if result.resource_state != "run" or result.raw_rows_read == 0:
                break
        return results

    async def follow(
        self,
        *,
        poll_seconds: float = 1.0,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        if not 0.1 <= poll_seconds <= 60:
            raise ValueError("poll_seconds must be between 0.1 and 60")
        stopper = stop_event or asyncio.Event()
        initial_id: int | None = None
        async with self._factory() as session:
            initial_id = int(
                await session.scalar(select(func.coalesce(func.max(MarketEvent.id), 0))) or 0
            )
        while not stopper.is_set():
            result = await self.normalize_batch(initial_raw_event_id=initial_id)
            initial_id = 0
            if result.resource_state == "pause" or result.raw_rows_read == 0:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stopper.wait(), timeout=poll_seconds)


def _is_transient(error: DBAPIError) -> bool:
    if error.connection_invalidated:
        return True
    sqlstate = getattr(error.orig, "sqlstate", None)
    return sqlstate in {"40001", "40P01", "55P03", "57P01", "08000", "08003", "08006"}
