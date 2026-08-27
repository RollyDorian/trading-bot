"""Strict Binance USD-M bookTicker / aggTrade parsers (fixture-driven)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from trading_bot.external_market_data.contract import (
    AGG_TRADE_REQUIRED_FIELDS,
    BOOK_TICKER_REQUIRED_FIELDS,
    ENVELOPE_SCHEMA_VERSION,
    INSTRUMENT,
    VENUE,
)
from trading_bot.external_market_data.envelope import EventType, ExternalRawEnvelope


class BinanceParseError(ValueError):
    """Unexpected or incomplete USD-M public payload."""


def _ms_to_utc(ms: Any) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=UTC)


def unwrap_stream_message(message: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Handle combined-stream wrapper {stream, data} or raw event object."""

    if "stream" in message and "data" in message and isinstance(message["data"], dict):
        return str(message["stream"]), message["data"]
    return None, message


def parse_binance_usdm_event(
    message: dict[str, Any],
    *,
    received_at: datetime,
    connection_id: str,
    local_sequence: int,
    expected_event: EventType | None = None,
) -> ExternalRawEnvelope:
    stream, data = unwrap_stream_message(message)
    event_name = data.get("e")
    if event_name == "bookTicker":
        event_type: EventType = "book_ticker"
        required = BOOK_TICKER_REQUIRED_FIELDS
    elif event_name == "aggTrade":
        event_type = "agg_trade"
        required = AGG_TRADE_REQUIRED_FIELDS
    else:
        raise BinanceParseError(f"unsupported_or_unknown_event:{event_name!r}")

    if expected_event is not None and event_type != expected_event:
        raise BinanceParseError(
            f"event_type_mismatch expected={expected_event} got={event_type}"
        )

    missing = sorted(field for field in required if field not in data)
    if missing:
        raise BinanceParseError(f"missing_fields:{missing}")

    symbol = str(data["s"])
    if symbol.upper() != INSTRUMENT:
        raise BinanceParseError(f"unexpected_symbol:{symbol}")

    exchange_at = _ms_to_utc(data["T"])
    book_update_id = int(data["u"]) if event_type == "book_ticker" else None
    agg_trade_id = int(data["a"]) if event_type == "agg_trade" else None
    first_trade_id = int(data["f"]) if event_type == "agg_trade" else None
    last_trade_id = int(data["l"]) if event_type == "agg_trade" else None

    return ExternalRawEnvelope(
        venue=VENUE,
        instrument=INSTRUMENT,
        event_type=event_type,
        received_at=received_at,
        connection_id=connection_id,
        local_sequence=local_sequence,
        schema_version=ENVELOPE_SCHEMA_VERSION,
        payload=dict(data),
        exchange_at=exchange_at,
        book_update_id=book_update_id,
        agg_trade_id=agg_trade_id,
        first_trade_id=first_trade_id,
        last_trade_id=last_trade_id,
        stream=stream,
        parse_ok=True,
        validation_error=None,
    )
