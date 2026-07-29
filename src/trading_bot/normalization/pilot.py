import asyncio
import math
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.normalization.orderbook import OrderBookReconstructor
from trading_bot.normalization.parsers import OrderBookRecord, parse_market_event
from trading_bot.normalization.resources import ResourceProbe, SystemResourceProbe
from trading_bot.normalization.runner import RawEventNormalizer
from trading_bot.storage.models import (
    BestQuote,
    FundingEstimate,
    MarketEvent,
    NormalizationError,
    OrderBookEvent,
    ReferencePrice,
)

MAX_PILOT_ROWS = 100_000


@dataclass(frozen=True, slots=True)
class PilotEstimate:
    daily_bytes: int | None
    seven_day_bytes: int | None
    headroom_days: float | None
    uncertainty: str


def capacity_estimate(
    *,
    normalized_bytes: int,
    source_rows: int,
    source_span_seconds: float,
    production_free_bytes: int | None,
    production_hard_floor_bytes: int,
) -> PilotEstimate:
    if source_rows < 1000 or source_span_seconds < 300 or normalized_bytes <= 0:
        return PilotEstimate(None, None, None, "insufficient_sample")
    source_rate = source_rows / source_span_seconds
    normalized_bytes_per_source = normalized_bytes / source_rows
    daily = math.ceil(source_rate * 86_400 * normalized_bytes_per_source)
    seven_day = daily * 7
    headroom: float | None = None
    if production_free_bytes is not None:
        usable = max(0, production_free_bytes - production_hard_floor_bytes)
        headroom = round(usable / daily, 2) if daily > 0 else None
    return PilotEstimate(daily, seven_day, headroom, "linear_extrapolation")


async def _relation_sizes(session: AsyncSession) -> tuple[int, int]:
    row = (
        await session.execute(
            text(
                """
                SELECT
                    coalesce(sum(pg_relation_size(c.oid)), 0)::bigint AS heap_bytes,
                    coalesce(sum(pg_indexes_size(c.oid)), 0)::bigint AS index_bytes
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname IN ('normalized', 'pipeline')
                  AND c.relkind = 'r'
                """
            )
        )
    ).one()
    return int(row.heap_bytes), int(row.index_bytes)


async def _wal_lsn(session: AsyncSession) -> str:
    return str(await session.scalar(text("SELECT pg_current_wal_lsn()::text")))


async def _wal_delta(session: AsyncSession, start_lsn: str) -> int:
    value = await session.scalar(
        text(
            "SELECT pg_wal_lsn_diff("
            "pg_current_wal_lsn(), CAST(CAST(:start_lsn AS text) AS pg_lsn)"
            ")"
        ),
        {"start_lsn": start_lsn},
    )
    return int(value or 0)


async def _peak_rss(
    probe: ResourceProbe,
    stop: asyncio.Event,
    samples: list[int],
) -> None:
    while not stop.is_set():
        samples.append(probe.rss_bytes())
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=0.05)


async def reconstruction_summary(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    reconstructor = OrderBookReconstructor()
    states: dict[str, int] = {}
    final_state = "no_orderbook_events"
    event_count = 0
    async with factory() as session:
        stream = await session.stream_scalars(
            select(MarketEvent)
            .where(MarketEvent.event_type == "orderbook")
            .order_by(MarketEvent.id)
            .execution_options(yield_per=100)
        )
        async for event in stream:
            parsed = parse_market_event(event)
            if not isinstance(parsed, OrderBookRecord):
                raise RuntimeError("orderbook parser returned an incompatible record")
            result = reconstructor.apply(parsed)
            final_state = result.state
            states[result.state] = states.get(result.state, 0) + 1
            event_count += 1
    return {
        "events": event_count,
        "final_state": final_state,
        "state_counts": dict(sorted(states.items())),
    }


async def run_capacity_pilot(
    factory: async_sessionmaker[AsyncSession],
    *,
    capacity_path: Path,
    max_raw_rows: int,
    production_free_bytes: int | None = None,
    production_hard_floor_bytes: int = 3 * 1024**3,
    resource_probe: ResourceProbe | None = None,
) -> dict[str, Any]:
    if not 1 <= max_raw_rows <= MAX_PILOT_ROWS:
        raise ValueError(f"max_raw_rows must be between 1 and {MAX_PILOT_ROWS}")
    probe = resource_probe or SystemResourceProbe()
    async with factory() as session:
        source_rows = int(
            await session.scalar(select(func.count()).select_from(MarketEvent)) or 0
        )
        if source_rows == 0 or source_rows > max_raw_rows:
            raise ValueError("pilot RAW sample is empty or exceeds the explicit bound")
        span = (
            await session.execute(
                select(
                    func.min(MarketEvent.received_at),
                    func.max(MarketEvent.received_at),
                )
            )
        ).one()
        if span[0] is None or span[1] is None:
            raise ValueError("pilot RAW timestamps are unavailable")
        source_span_seconds = max(0.0, (span[1] - span[0]).total_seconds())
        event_counts = {
            str(event_type): int(count)
            for event_type, count in (
                await session.execute(
                    select(MarketEvent.event_type, func.count())
                    .group_by(MarketEvent.event_type)
                    .order_by(MarketEvent.event_type)
                )
            ).all()
        }
        before_heap, before_indexes = await _relation_sizes(session)
        start_lsn = await _wal_lsn(session)
    normalizer = RawEventNormalizer(
        factory,
        consumer=f"capacity-pilot-{uuid4()}",
        batch_size=100,
        capacity_path=capacity_path,
        resource_probe=probe,
    )
    stop = asyncio.Event()
    rss_samples: list[int] = []
    monitor = asyncio.create_task(_peak_rss(probe, stop, rss_samples))
    started = time.monotonic()
    read_rows = normalized_rows = error_rows = 0
    try:
        for _ in range(math.ceil(source_rows / 100) + 1):
            result = await normalizer.normalize_batch()
            if result.resource_state != "run":
                raise RuntimeError(f"pilot resource gate: {result.resource_reason}")
            read_rows += result.raw_rows_read
            normalized_rows += result.normalized_rows
            error_rows += result.error_rows
            if result.raw_rows_read == 0:
                break
    finally:
        stop.set()
        await monitor
    duration = time.monotonic() - started
    async with factory() as session:
        after_heap, after_indexes = await _relation_sizes(session)
        wal_bytes = await _wal_delta(session, start_lsn)
        normalized_counts = {
            "best_quotes": int(
                await session.scalar(select(func.count()).select_from(BestQuote)) or 0
            ),
            "funding_estimates": int(
                await session.scalar(select(func.count()).select_from(FundingEstimate)) or 0
            ),
            "orderbook_events": int(
                await session.scalar(select(func.count()).select_from(OrderBookEvent)) or 0
            ),
            "reference_prices": int(
                await session.scalar(select(func.count()).select_from(ReferencePrice)) or 0
            ),
        }
        errors_by_code = {
            str(code): int(count)
            for code, count in (
                await session.execute(
                    select(NormalizationError.error_code, func.count())
                    .group_by(NormalizationError.error_code)
                    .order_by(NormalizationError.error_code)
                )
            ).all()
        }
    heap_growth = max(0, after_heap - before_heap)
    index_growth = max(0, after_indexes - before_indexes)
    estimate = capacity_estimate(
        normalized_bytes=heap_growth + index_growth,
        source_rows=source_rows,
        source_span_seconds=source_span_seconds,
        production_free_bytes=production_free_bytes,
        production_hard_floor_bytes=production_hard_floor_bytes,
    )
    reconstruction = await reconstruction_summary(factory)
    return {
        "schema_version": 1,
        "sample": {
            "raw_rows": source_rows,
            "span_seconds": round(source_span_seconds, 3),
            "event_counts": event_counts,
        },
        "output": {
            "raw_rows_read": read_rows,
            "normalized_rows": normalized_rows,
            "normalization_errors": error_rows,
            "rows_by_table": normalized_counts,
            "errors_by_code": errors_by_code,
        },
        "runtime": {
            "duration_seconds": round(duration, 3),
            "throughput_rows_per_second": round(read_rows / duration, 2),
            "peak_rss_bytes": max(rss_samples, default=probe.rss_bytes()),
        },
        "storage": {
            "heap_growth_bytes": heap_growth,
            "index_growth_bytes": index_growth,
            "wal_bytes": wal_bytes,
        },
        "estimate": asdict(estimate),
        "reconstruction": reconstruction,
    }


def bounded_summary(report: Mapping[str, Any]) -> str:
    sample = report["sample"]
    output = report["output"]
    runtime = report["runtime"]
    storage = report["storage"]
    estimate = report["estimate"]
    return (
        f"raw_rows={sample['raw_rows']} normalized_rows={output['normalized_rows']} "
        f"errors={output['normalization_errors']} duration_seconds={runtime['duration_seconds']} "
        f"peak_rss_bytes={runtime['peak_rss_bytes']} "
        f"normalized_bytes={storage['heap_growth_bytes'] + storage['index_growth_bytes']} "
        f"wal_bytes={storage['wal_bytes']} daily_estimate_bytes={estimate['daily_bytes']} "
        f"headroom_days={estimate['headroom_days']}"
    )
