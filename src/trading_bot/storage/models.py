from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Float, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

json_type = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class MarketEvent(Base):
    """Unmodified market message plus normalized replay and quality fields."""

    __tablename__ = "market_events"
    __table_args__ = (
        Index("ix_market_events_symbol_exchange_at", "symbol", "exchange_at"),
        Index("ix_market_events_type_received_at", "event_type", "received_at"),
        Index("ix_market_events_source_sequence", "source", "sequence"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    exchange_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    sequence: Mapped[int | None] = mapped_column(BigInteger)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict[str, Any]] = mapped_column(json_type, nullable=False)


class SystemEvent(Base):
    """Connectivity, validation, desynchronization, and lifecycle events."""

    __tablename__ = "system_events"
    __table_args__ = (Index("ix_system_events_type_occurred_at", "event_type", "occurred_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(json_type, nullable=False, default=dict)
