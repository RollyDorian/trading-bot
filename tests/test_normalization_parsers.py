import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from trading_bot.normalization.parsers import (
    BestQuoteRecord,
    FundingEstimateRecord,
    NormalizationFailure,
    OrderBookRecord,
    ReferencePriceRecord,
    parse_market_event,
)
from trading_bot.storage.models import MarketEvent

FIXTURES = Path(__file__).parent / "fixtures" / "hibachi"


def payload(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def raw(name: str, *, raw_id: int = 1, schema_version: int = 2) -> MarketEvent:
    body = payload(name)
    event = MarketEvent(
        received_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
        exchange_at=None,
        source="hibachi_ws",
        event_type=str(body["topic"]),
        symbol=str(body["symbol"]),
        sequence=None,
        connection_id="00000000-0000-0000-0000-000000000001",
        local_sequence=raw_id,
        exchange_sequence=None,
        schema_version=schema_version,
        latency_ms=None,
        payload=body,
    )
    event.id = raw_id
    return event


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("ask_bid_price", BestQuoteRecord),
        ("mark_price", ReferencePriceRecord),
        ("spot_price", ReferencePriceRecord),
        ("funding_rate_estimation", FundingEstimateRecord),
        ("orderbook_snapshot", OrderBookRecord),
        ("orderbook_update", OrderBookRecord),
        ("orderbook_empty_update", OrderBookRecord),
    ],
)
def test_captured_contract_fixtures_parse_strictly(
    fixture: str,
    expected: type[object],
) -> None:
    assert isinstance(parse_market_event(raw(fixture)), expected)


def test_repeated_message_is_deterministic() -> None:
    first = parse_market_event(raw("orderbook_update", raw_id=10))
    second = parse_market_event(raw("orderbook_update", raw_id=10))
    assert first == second


def test_zero_quantity_is_preserved_as_removal_instruction() -> None:
    record = parse_market_event(raw("orderbook_update"))
    assert isinstance(record, OrderBookRecord)
    assert record.asks[0].quantity == 0


def test_empty_update_is_not_coerced_to_snapshot() -> None:
    record = parse_market_event(raw("orderbook_empty_update"))
    assert isinstance(record, OrderBookRecord)
    assert record.message_type == "Update"
    assert record.bids == ()
    assert record.asks == ()


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value.update(messageType="Delta"), "unknown_message_type"),
        (lambda value: value.update(messageType={}), "unknown_message_type"),
        (
            lambda value: value["data"]["bid"]["levels"][0].update(price="nan"),
            "invalid_decimal",
        ),
        (lambda value: value.update(timestamp_ms="2026-07-29"), "invalid_timestamp"),
        (
            lambda value: value["data"]["bid"]["levels"][0].update(quantity="-1"),
            "impossible_value",
        ),
        (lambda value: value.update(undocumented=True), "payload_contract"),
    ],
)
def test_malformed_orderbook_fails_with_bounded_code(
    mutation: Any,
    code: str,
) -> None:
    event = raw("orderbook_update")
    event.payload = copy.deepcopy(event.payload)
    mutation(event.payload)
    with pytest.raises(NormalizationFailure) as caught:
        parse_market_event(event)
    assert caught.value.code == code
    assert len(caught.value.detail) <= 160


def test_unknown_event_type_is_an_error() -> None:
    event = raw("mark_price")
    event.event_type = "trades"
    with pytest.raises(NormalizationFailure, match="not supported") as caught:
        parse_market_event(event)
    assert caught.value.code == "unsupported_event_type"


def test_naive_envelope_timestamp_fails_closed() -> None:
    event = raw("mark_price")
    event.received_at = datetime(2026, 7, 29)
    with pytest.raises(NormalizationFailure) as caught:
        parse_market_event(event)
    assert caught.value.code == "invalid_timestamp"


def test_orderbook_quality_never_claims_exact() -> None:
    current = parse_market_event(raw("orderbook_snapshot"))
    legacy = parse_market_event(raw("orderbook_snapshot", schema_version=1))
    assert isinstance(current, OrderBookRecord)
    assert isinstance(legacy, OrderBookRecord)
    assert current.provenance.data_quality == "sequence_unverified"
    assert legacy.provenance.data_quality == "best_effort_legacy"
