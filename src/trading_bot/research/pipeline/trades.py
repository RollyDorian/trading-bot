"""Research-side trade parsing (fixture-driven; not PG normalized history)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from trading_bot.normalization.parsers import (
    PIPELINE_VERSION,
    NormalizationFailure,
    Provenance,
    _decimal,
    _exact_keys,
    _mapping,
    _provenance,
    _timestamp_milliseconds,
)
from trading_bot.storage.models import MarketEvent

TradeSide = Literal["Buy", "Sell"]


@dataclass(frozen=True, slots=True)
class TradeRecord:
    provenance: Provenance
    price: Decimal
    quantity: Decimal
    taker_side: TradeSide
    exchange_trade_at: datetime | None


def parse_trade_event(event: MarketEvent) -> TradeRecord:
    """Parse a Hibachi ``trades`` RAW event for offline research Parquet."""

    if event.event_type != "trades":
        raise NormalizationFailure("unsupported_event_type", "expected trades topic")
    payload = _mapping(event.payload, code="payload_contract", detail="payload must be object")
    _exact_keys(payload, frozenset({"topic", "symbol", "data"}), path="payload")
    if payload["topic"] != "trades" or payload["symbol"] != event.symbol:
        raise NormalizationFailure("identity_mismatch", "envelope/payload identity differ")
    data = _mapping(payload["data"], code="payload_contract", detail="data must be object")
    _exact_keys(data, frozenset({"trade"}), path="payload.data")
    trade = _mapping(data["trade"], code="payload_contract", detail="trade must be object")
    _exact_keys(
        trade,
        frozenset({"price", "quantity", "takerSide", "timestamp"}),
        path="payload.data.trade",
    )
    side_raw = trade["takerSide"]
    if side_raw not in {"Buy", "Sell"}:
        raise NormalizationFailure("invalid_side", "takerSide must be Buy or Sell")
    return TradeRecord(
        provenance=_provenance(event),
        price=_decimal(trade["price"], field="price"),
        quantity=_decimal(trade["quantity"], field="quantity"),
        taker_side=side_raw,
        exchange_trade_at=_timestamp_milliseconds(trade["timestamp"], field="timestamp"),
    )


def _schema_version_from_raw_row(row: dict[str, Any]) -> int:
    """Prefer archive ``raw_schema_version`` over fixture ``schema_version``."""

    raw_sv = row.get("raw_schema_version")
    if raw_sv is not None:
        return int(raw_sv)
    legacy_sv = row.get("schema_version")
    if legacy_sv is not None:
        return int(legacy_sv)
    return 1


def raw_row_to_market_event(row: dict[str, Any]) -> MarketEvent:
    """Build a MarketEvent from research ``events.parquet`` row dict."""

    payload = row.get("payload")
    if payload is None:
        payload_json = row.get("payload_json")
        if isinstance(payload_json, str):
            import json

            payload = json.loads(payload_json)
        else:
            raise NormalizationFailure("missing_payload", "payload/payload_json required")
    received_at = row["received_at"]
    if isinstance(received_at, str):
        received_at = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
    exchange_at = row.get("exchange_at")
    if isinstance(exchange_at, str):
        exchange_at = datetime.fromisoformat(exchange_at.replace("Z", "+00:00"))
    raw_id = row.get("raw_event_id", row.get("id"))
    if raw_id is None:
        raise NormalizationFailure("missing_identity", "raw_event_id/id required")
    if isinstance(received_at, datetime) and received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=UTC)
    elif isinstance(received_at, datetime):
        received_at = received_at.astimezone(UTC)
    event = MarketEvent(
        received_at=received_at,
        exchange_at=(
            exchange_at.astimezone(UTC)
            if exchange_at is not None and getattr(exchange_at, "tzinfo", None)
            else exchange_at
        ),
        source=str(row.get("source", "hibachi_ws")),
        event_type=str(row.get("topic") or row.get("event_type")),
        symbol=str(row.get("symbol")),
        sequence=row.get("sequence"),
        latency_ms=row.get("latency_ms"),
        payload=payload,
        connection_id=row.get("connection_id"),
        local_sequence=row.get("local_sequence"),
        exchange_sequence=row.get("exchange_sequence"),
        # B2/archive events.parquet uses raw_schema_version; older fixtures
        # use schema_version. Default 1 only when both columns are absent.
        schema_version=_schema_version_from_raw_row(row),
    )
    event.id = int(raw_id)
    return event


__all__ = [
    "PIPELINE_VERSION",
    "TradeRecord",
    "parse_trade_event",
    "raw_row_to_market_event",
]
