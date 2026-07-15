from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.storage.database import session_scope
from trading_bot.storage.models import MarketEvent, SystemEvent


@dataclass(frozen=True, slots=True)
class MarketEventInput:
    received_at: datetime
    exchange_at: datetime | None
    source: str
    event_type: str
    symbol: str
    sequence: int | None
    latency_ms: float | None
    payload: dict[str, Any]


class EventRepository:
    """Persists each input as an append-only row and propagates database failures."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def append_market_event(self, event: MarketEventInput) -> None:
        async with session_scope(self._session_factory) as session:
            session.add(MarketEvent(**asdict(event)))

    async def append_system_event(
        self,
        *,
        severity: str,
        event_type: str,
        component: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        async with session_scope(self._session_factory) as session:
            session.add(
                SystemEvent(
                    severity=severity,
                    event_type=event_type,
                    component=component,
                    message=message,
                    details=details or {},
                )
            )
