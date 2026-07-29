from dataclasses import replace
from datetime import timedelta

from tests.test_normalization_parsers import raw
from trading_bot.normalization.orderbook import OrderBookReconstructor, compare_best_quote
from trading_bot.normalization.parsers import (
    BestQuoteRecord,
    OrderBookRecord,
    parse_market_event,
)


def book(name: str, *, raw_id: int) -> OrderBookRecord:
    parsed = parse_market_event(raw(name, raw_id=raw_id))
    assert isinstance(parsed, OrderBookRecord)
    return parsed


def test_snapshot_update_delete_and_empty_update_chain() -> None:
    reconstructor = OrderBookReconstructor()
    snapshot = book("orderbook_snapshot", raw_id=1)
    update = book("orderbook_update", raw_id=2)
    empty = book("orderbook_empty_update", raw_id=3)

    state = reconstructor.apply(snapshot)
    assert (state.best_bid, state.best_ask, state.level_count) == (
        snapshot.bids[0].price,
        snapshot.asks[0].price,
        4,
    )
    state = reconstructor.apply(update)
    assert state.best_ask == snapshot.asks[1].price
    assert state.level_count == 3
    assert reconstructor.apply(empty) == state


def test_reconnect_invalidates_until_snapshot() -> None:
    reconstructor = OrderBookReconstructor()
    reconstructor.apply(book("orderbook_snapshot", raw_id=1))
    assert reconstructor.disconnect("ETH/USDT-P").state == "invalid_waiting_snapshot"
    update = book("orderbook_update", raw_id=2)
    update = replace(
        update,
        provenance=replace(update.provenance, connection_id="new-connection"),
    )
    assert reconstructor.apply(update).state == "invalid_waiting_snapshot"


def test_sequence_gap_invalidates_verified_chain() -> None:
    reconstructor = OrderBookReconstructor()
    snapshot = book("orderbook_snapshot", raw_id=1)
    snapshot = replace(
        snapshot,
        provenance=replace(snapshot.provenance, exchange_sequence=10),
    )
    update = book("orderbook_empty_update", raw_id=2)
    update = replace(
        update,
        provenance=replace(update.provenance, exchange_sequence=12),
    )
    assert reconstructor.apply(snapshot).state == "valid_sequence_verified"
    assert reconstructor.apply(update).state == "invalid_sequence"


def test_crossed_update_is_invalid() -> None:
    reconstructor = OrderBookReconstructor()
    reconstructor.apply(book("orderbook_snapshot", raw_id=1))
    update = book("orderbook_update", raw_id=2)
    crossed = replace(
        update,
        bids=(replace(update.bids[0], price=update.asks[0].price + 1),),
        asks=(),
    )
    assert reconstructor.apply(crossed).state == "invalid_crossed"


def test_best_quote_comparison_is_time_tolerant() -> None:
    reconstructor = OrderBookReconstructor()
    snapshot = book("orderbook_snapshot", raw_id=1)
    state = reconstructor.apply(snapshot)
    quote_record = parse_market_event(raw("ask_bid_price", raw_id=2))
    assert isinstance(quote_record, BestQuoteRecord)
    quote_record = replace(
        quote_record,
        bid_price=state.best_bid,
        ask_price=state.best_ask,
        provenance=replace(
            quote_record.provenance,
            available_at=snapshot.provenance.available_at + timedelta(seconds=1),
        ),
    )
    assert compare_best_quote(state, snapshot, quote_record) == "match"
    late = replace(
        quote_record,
        provenance=replace(
            quote_record.provenance,
            available_at=snapshot.provenance.available_at + timedelta(seconds=3),
        ),
    )
    assert compare_best_quote(state, snapshot, late) == "not_comparable"
