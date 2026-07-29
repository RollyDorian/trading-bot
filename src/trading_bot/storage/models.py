from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
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
    connection_id: Mapped[str | None] = mapped_column(String(36))
    local_sequence: Mapped[int | None] = mapped_column(BigInteger)
    exchange_sequence: Mapped[int | None] = mapped_column(BigInteger)
    schema_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default="1"
    )
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


numeric_type = Numeric(38, 18)


class NormalizedProvenance:
    """Columns required to reproduce a typed row from append-only RAW."""

    raw_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exchange_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    connection_id: Mapped[str | None] = mapped_column(String(36))
    local_sequence: Mapped[int | None] = mapped_column(BigInteger)
    exchange_sequence: Mapped[int | None] = mapped_column(BigInteger)
    raw_schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    pipeline_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    data_quality: Mapped[str] = mapped_column(String(32), nullable=False)


class BestQuote(NormalizedProvenance, Base):
    __tablename__ = "best_quotes"
    __table_args__ = (
        UniqueConstraint(
            "raw_event_id",
            "pipeline_version",
            name="uq_best_quotes_raw_pipeline",
        ),
        Index("ix_best_quotes_symbol_available", "symbol", "available_at"),
        {"schema": "normalized"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    bid_price: Mapped[Decimal] = mapped_column(numeric_type, nullable=False)
    bid_size: Mapped[Decimal] = mapped_column(numeric_type, nullable=False)
    ask_price: Mapped[Decimal] = mapped_column(numeric_type, nullable=False)
    ask_size: Mapped[Decimal] = mapped_column(numeric_type, nullable=False)


class ReferencePrice(NormalizedProvenance, Base):
    __tablename__ = "reference_prices"
    __table_args__ = (
        UniqueConstraint(
            "raw_event_id",
            "pipeline_version",
            name="uq_reference_prices_raw_pipeline",
        ),
        Index(
            "ix_reference_prices_symbol_kind_available",
            "symbol",
            "price_kind",
            "available_at",
        ),
        {"schema": "normalized"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    price_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    price: Mapped[Decimal] = mapped_column(numeric_type, nullable=False)


class FundingEstimate(NormalizedProvenance, Base):
    __tablename__ = "funding_estimates"
    __table_args__ = (
        UniqueConstraint(
            "raw_event_id",
            "pipeline_version",
            name="uq_funding_estimates_raw_pipeline",
        ),
        Index("ix_funding_estimates_symbol_available", "symbol", "available_at"),
        {"schema": "normalized"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    estimated_rate: Mapped[Decimal] = mapped_column(numeric_type, nullable=False)
    next_funding_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrderBookEvent(NormalizedProvenance, Base):
    __tablename__ = "orderbook_events"
    __table_args__ = (
        UniqueConstraint(
            "raw_event_id",
            "pipeline_version",
            name="uq_orderbook_events_raw_pipeline",
        ),
        Index("ix_orderbook_events_symbol_available", "symbol", "available_at"),
        {"schema": "normalized"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_type: Mapped[str] = mapped_column(String(16), nullable=False)
    depth: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    granularity: Mapped[Decimal] = mapped_column(numeric_type, nullable=False)
    bids: Mapped[list[dict[str, str]]] = mapped_column(json_type, nullable=False)
    asks: Mapped[list[dict[str, str]]] = mapped_column(json_type, nullable=False)
    changed_level_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)


class NormalizerCheckpoint(Base):
    __tablename__ = "checkpoints"
    __table_args__ = ({"schema": "pipeline"},)

    consumer: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_raw_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    pipeline_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class NormalizationError(Base):
    __tablename__ = "normalization_errors"
    __table_args__ = (
        UniqueConstraint(
            "raw_event_id",
            "pipeline_version",
            name="uq_normalization_errors_raw_pipeline",
        ),
        Index("ix_normalization_errors_code_created", "error_code", "created_at"),
        {"schema": "pipeline"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    raw_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pipeline_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False)
    error_detail: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
