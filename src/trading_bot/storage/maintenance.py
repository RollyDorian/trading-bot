from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.storage.database import session_scope
from trading_bot.storage.models import MarketEvent, SystemEvent


@dataclass(frozen=True, slots=True)
class ReplayFilter:
    symbol: str
    start: datetime | None = None
    end: datetime | None = None
    event_types: tuple[str, ...] = ()
    limit: int = 10_000

    def __post_init__(self) -> None:
        if self.limit < 1 or self.limit > 1_000_000:
            raise ValueError("Replay limit must be between 1 and 1,000,000.")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("Replay start must be earlier than end.")
        if self.start is not None and self.start.tzinfo is None:
            raise ValueError("Replay start must be timezone-aware.")
        if self.end is not None and self.end.tzinfo is None:
            raise ValueError("Replay end must be timezone-aware.")


@dataclass(frozen=True, slots=True)
class DailyQualityMetric:
    day: date
    symbol: str
    event_type: str
    total_events: int
    missing_exchange_time: int
    missing_sequence: int
    average_latency_ms: float | None
    maximum_latency_ms: float | None


@dataclass(frozen=True, slots=True)
class RetentionResult:
    market_events_deleted: int
    system_events_deleted: int


class DataMaintenance:
    """Explicit read/research maintenance outside the append-only collection path."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def replay(self, replay_filter: ReplayFilter) -> list[MarketEvent]:
        statement = select(MarketEvent).where(MarketEvent.symbol == replay_filter.symbol)
        if replay_filter.start is not None:
            statement = statement.where(MarketEvent.received_at >= replay_filter.start)
        if replay_filter.end is not None:
            statement = statement.where(MarketEvent.received_at < replay_filter.end)
        if replay_filter.event_types:
            statement = statement.where(MarketEvent.event_type.in_(replay_filter.event_types))
        statement = statement.order_by(MarketEvent.received_at, MarketEvent.id).limit(
            replay_filter.limit
        )
        async with self._session_factory() as session:
            return list((await session.scalars(statement)).all())

    async def daily_quality(
        self,
        day: date,
        *,
        symbol: str | None = None,
    ) -> list[DailyQualityMetric]:
        start = datetime.combine(day, time.min, tzinfo=UTC)
        end = start + timedelta(days=1)
        statement = (
            select(
                MarketEvent.symbol,
                MarketEvent.event_type,
                func.count(MarketEvent.id),
                func.count().filter(MarketEvent.exchange_at.is_(None)),
                func.count().filter(MarketEvent.sequence.is_(None)),
                func.avg(MarketEvent.latency_ms),
                func.max(MarketEvent.latency_ms),
            )
            .where(MarketEvent.received_at >= start, MarketEvent.received_at < end)
            .group_by(MarketEvent.symbol, MarketEvent.event_type)
            .order_by(MarketEvent.symbol, MarketEvent.event_type)
        )
        if symbol is not None:
            statement = statement.where(MarketEvent.symbol == symbol)
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        return [
            DailyQualityMetric(
                day=day,
                symbol=row[0],
                event_type=row[1],
                total_events=row[2],
                missing_exchange_time=row[3],
                missing_sequence=row[4],
                average_latency_ms=float(row[5]) if row[5] is not None else None,
                maximum_latency_ms=float(row[6]) if row[6] is not None else None,
            )
            for row in rows
        ]

    async def prune_before(
        self,
        cutoff: datetime,
        *,
        confirmed: bool = False,
    ) -> RetentionResult:
        if not confirmed:
            raise ValueError("Retention deletion requires confirmed=True.")
        if cutoff.tzinfo is None:
            raise ValueError("Retention cutoff must be timezone-aware.")
        async with session_scope(self._session_factory) as session:
            market_result = await session.execute(
                delete(MarketEvent).where(MarketEvent.received_at < cutoff)
            )
            system_result = await session.execute(
                delete(SystemEvent).where(SystemEvent.occurred_at < cutoff)
            )
            if not isinstance(market_result, CursorResult) or not isinstance(
                system_result, CursorResult
            ):
                raise RuntimeError("Retention deletes did not return row counts.")
        return RetentionResult(
            market_events_deleted=market_result.rowcount or 0,
            system_events_deleted=system_result.rowcount or 0,
        )
