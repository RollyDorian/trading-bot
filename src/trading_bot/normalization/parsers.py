from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast

from trading_bot.storage.models import MarketEvent

PIPELINE_VERSION = 1
Quality = Literal["validated", "sequence_unverified", "best_effort_legacy"]


class NormalizationFailure(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail[:160]


@dataclass(frozen=True, slots=True)
class Provenance:
    raw_event_id: int
    received_at: datetime
    available_at: datetime
    exchange_at: datetime | None
    symbol: str
    source: str
    connection_id: str | None
    local_sequence: int | None
    exchange_sequence: int | None
    raw_schema_version: int
    pipeline_version: int
    data_quality: Quality


@dataclass(frozen=True, slots=True)
class BestQuoteRecord:
    provenance: Provenance
    bid_price: Decimal
    bid_size: Decimal
    ask_price: Decimal
    ask_size: Decimal


@dataclass(frozen=True, slots=True)
class ReferencePriceRecord:
    provenance: Provenance
    price_kind: Literal["mark", "spot"]
    price: Decimal


@dataclass(frozen=True, slots=True)
class FundingEstimateRecord:
    provenance: Provenance
    estimated_rate: Decimal
    next_funding_at: datetime


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    quantity: Decimal

    def compact(self) -> dict[str, str]:
        return {"price": str(self.price), "quantity": str(self.quantity)}


@dataclass(frozen=True, slots=True)
class OrderBookRecord:
    provenance: Provenance
    message_type: Literal["Snapshot", "Update"]
    depth: int
    granularity: Decimal
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]


type ParsedRecord = (
    BestQuoteRecord | ReferencePriceRecord | FundingEstimateRecord | OrderBookRecord
)

_ROOT_KEYS = {
    "ask_bid_price": frozenset({"topic", "symbol", "data"}),
    "mark_price": frozenset({"topic", "symbol", "data"}),
    "spot_price": frozenset({"topic", "symbol", "data"}),
    "funding_rate_estimation": frozenset({"topic", "symbol", "data"}),
    "orderbook": frozenset(
        {
            "topic",
            "symbol",
            "messageType",
            "depth",
            "granularity",
            "timestamp_ms",
            "data",
        }
    ),
}


def _mapping(value: Any, *, code: str, detail: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise NormalizationFailure(code, detail)
    return value


def _exact_keys(value: dict[str, Any], expected: frozenset[str], *, path: str) -> None:
    if frozenset(value) != expected:
        raise NormalizationFailure("payload_contract", f"{path} fields do not match contract")


def _decimal(
    value: Any,
    *,
    field: str,
    allow_zero: bool = False,
    allow_negative: bool = False,
) -> Decimal:
    if not isinstance(value, str):
        raise NormalizationFailure("invalid_decimal", f"{field} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise NormalizationFailure("invalid_decimal", f"{field} is invalid") from error
    if not parsed.is_finite():
        raise NormalizationFailure("invalid_decimal", f"{field} is invalid")
    if (not allow_negative and parsed < 0) or (not allow_zero and parsed == 0):
        raise NormalizationFailure("impossible_value", f"{field} is outside the valid range")
    return parsed


def _timestamp_seconds(value: Any, *, field: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise NormalizationFailure("invalid_timestamp", f"{field} must be Unix seconds")
    try:
        parsed = datetime.fromtimestamp(value, tz=UTC)
    except (OSError, OverflowError, ValueError) as error:
        raise NormalizationFailure("invalid_timestamp", f"{field} is invalid") from error
    return parsed


def _timestamp_milliseconds(value: Any, *, field: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise NormalizationFailure("invalid_timestamp", f"{field} must be Unix milliseconds")
    return _timestamp_seconds(value / 1000, field=field)


def _provenance(event: MarketEvent, *, orderbook: bool = False) -> Provenance:
    if event.id is None:
        raise NormalizationFailure("missing_identity", "RAW event identity is missing")
    if event.received_at.tzinfo is None:
        raise NormalizationFailure("invalid_timestamp", "received_at must be timezone-aware")
    exchange_at = event.exchange_at
    if exchange_at is not None and exchange_at.tzinfo is None:
        raise NormalizationFailure("invalid_timestamp", "exchange_at must be timezone-aware")
    if orderbook:
        if event.schema_version == 1:
            quality: Quality = "best_effort_legacy"
        else:
            quality = "sequence_unverified"
    else:
        quality = "validated"
    return Provenance(
        raw_event_id=event.id,
        received_at=event.received_at.astimezone(UTC),
        available_at=event.received_at.astimezone(UTC),
        exchange_at=exchange_at.astimezone(UTC) if exchange_at is not None else None,
        symbol=event.symbol,
        source=event.source,
        connection_id=event.connection_id,
        local_sequence=event.local_sequence,
        exchange_sequence=event.exchange_sequence,
        raw_schema_version=event.schema_version,
        pipeline_version=PIPELINE_VERSION,
        data_quality=quality,
    )


def _validated_payload(event: MarketEvent) -> tuple[dict[str, Any], dict[str, Any]]:
    expected = _ROOT_KEYS.get(event.event_type)
    if expected is None:
        raise NormalizationFailure(
            "unsupported_event_type",
            "RAW event type is not supported by this pipeline version",
        )
    payload = _mapping(event.payload, code="payload_contract", detail="payload must be an object")
    _exact_keys(payload, expected, path="payload")
    if payload["topic"] != event.event_type or payload["symbol"] != event.symbol:
        raise NormalizationFailure("identity_mismatch", "RAW envelope and payload identity differ")
    data = _mapping(
        payload["data"],
        code="payload_contract",
        detail="payload.data must be an object",
    )
    return payload, data


def _parse_best_quote(event: MarketEvent, data: dict[str, Any]) -> BestQuoteRecord:
    _exact_keys(
        data,
        frozenset({"bidPrice", "bidSize", "askPrice", "askSize"}),
        path="payload.data",
    )
    bid_price = _decimal(data["bidPrice"], field="bidPrice")
    ask_price = _decimal(data["askPrice"], field="askPrice")
    if bid_price >= ask_price:
        raise NormalizationFailure("crossed_quote", "best quote must have bid below ask")
    return BestQuoteRecord(
        provenance=_provenance(event),
        bid_price=bid_price,
        bid_size=_decimal(data["bidSize"], field="bidSize", allow_zero=True),
        ask_price=ask_price,
        ask_size=_decimal(data["askSize"], field="askSize", allow_zero=True),
    )


def _parse_reference_price(
    event: MarketEvent,
    data: dict[str, Any],
) -> ReferencePriceRecord:
    if event.event_type == "mark_price":
        key: Literal["markPrice", "spotPrice"] = "markPrice"
        kind: Literal["mark", "spot"] = "mark"
    else:
        key = "spotPrice"
        kind = "spot"
    _exact_keys(data, frozenset({key}), path="payload.data")
    return ReferencePriceRecord(
        provenance=_provenance(event),
        price_kind=kind,
        price=_decimal(data[key], field=key),
    )


def _parse_funding(event: MarketEvent, data: dict[str, Any]) -> FundingEstimateRecord:
    _exact_keys(data, frozenset({"fundingRateEstimation"}), path="payload.data")
    estimate = _mapping(
        data["fundingRateEstimation"],
        code="payload_contract",
        detail="fundingRateEstimation must be an object",
    )
    _exact_keys(
        estimate,
        frozenset({"estimatedFundingRate", "nextFundingTimestamp"}),
        path="payload.data.fundingRateEstimation",
    )
    return FundingEstimateRecord(
        provenance=_provenance(event),
        estimated_rate=_decimal(
            estimate["estimatedFundingRate"],
            field="estimatedFundingRate",
            allow_zero=True,
            allow_negative=True,
        ),
        next_funding_at=_timestamp_seconds(
            estimate["nextFundingTimestamp"],
            field="nextFundingTimestamp",
        ),
    )


def _book_side(data: dict[str, Any], side: str) -> tuple[BookLevel, ...]:
    book_side = _mapping(
        data[side],
        code="payload_contract",
        detail=f"{side} must be an object",
    )
    _exact_keys(
        book_side,
        frozenset({"startPrice", "endPrice", "levels"}),
        path=f"payload.data.{side}",
    )
    _decimal(book_side["startPrice"], field=f"{side}.startPrice")
    _decimal(book_side["endPrice"], field=f"{side}.endPrice")
    levels = book_side["levels"]
    if not isinstance(levels, list):
        raise NormalizationFailure("payload_contract", f"{side}.levels must be an array")
    parsed: list[BookLevel] = []
    prices: set[Decimal] = set()
    for item in levels:
        level = _mapping(
            item,
            code="payload_contract",
            detail=f"{side}.levels entries must be objects",
        )
        _exact_keys(level, frozenset({"price", "quantity"}), path=f"{side}.levels[]")
        price = _decimal(level["price"], field=f"{side}.price")
        if price in prices:
            raise NormalizationFailure("duplicate_level", f"{side} contains a duplicate price")
        prices.add(price)
        parsed.append(
            BookLevel(
                price=price,
                quantity=_decimal(
                    level["quantity"],
                    field=f"{side}.quantity",
                    allow_zero=True,
                ),
            )
        )
    return tuple(parsed)


def _parse_orderbook(
    event: MarketEvent,
    payload: dict[str, Any],
    data: dict[str, Any],
) -> OrderBookRecord:
    message_type = payload["messageType"]
    if not isinstance(message_type, str) or message_type not in {"Snapshot", "Update"}:
        raise NormalizationFailure("unknown_message_type", "orderbook messageType is unsupported")
    if isinstance(payload["depth"], bool) or not isinstance(payload["depth"], int):
        raise NormalizationFailure("payload_contract", "depth must be an integer")
    if not 1 <= payload["depth"] <= 100:
        raise NormalizationFailure("impossible_value", "depth is outside the valid range")
    _timestamp_milliseconds(payload["timestamp_ms"], field="timestamp_ms")
    granularity = _decimal(payload["granularity"], field="granularity")
    _exact_keys(data, frozenset({"bid", "ask"}), path="payload.data")
    bids = _book_side(data, "bid")
    asks = _book_side(data, "ask")
    if message_type == "Snapshot":
        if not bids or not asks:
            raise NormalizationFailure("invalid_snapshot", "snapshot must contain both sides")
        if max(level.price for level in bids) >= min(level.price for level in asks):
            raise NormalizationFailure("crossed_book", "snapshot book is crossed")
    return OrderBookRecord(
        provenance=_provenance(event, orderbook=True),
        message_type=cast(Literal["Snapshot", "Update"], message_type),
        depth=payload["depth"],
        granularity=granularity,
        bids=bids,
        asks=asks,
    )


def parse_market_event(event: MarketEvent) -> ParsedRecord:
    payload, data = _validated_payload(event)
    if event.event_type == "ask_bid_price":
        return _parse_best_quote(event, data)
    if event.event_type in {"mark_price", "spot_price"}:
        return _parse_reference_price(event, data)
    if event.event_type == "funding_rate_estimation":
        return _parse_funding(event, data)
    if event.event_type == "orderbook":
        return _parse_orderbook(event, payload, data)
    raise NormalizationFailure("unsupported_event_type", "RAW event type is unsupported")
