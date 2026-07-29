from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Literal

from trading_bot.normalization.parsers import (
    BestQuoteRecord,
    BookLevel,
    OrderBookRecord,
)

BookState = Literal[
    "valid_sequence_verified",
    "valid_sequence_unverified",
    "valid_best_effort_legacy",
    "invalid_waiting_snapshot",
    "invalid_crossed",
    "invalid_sequence",
]


@dataclass(frozen=True, slots=True)
class ReconstructionResult:
    state: BookState
    best_bid: Decimal | None
    best_ask: Decimal | None
    level_count: int


@dataclass(slots=True)
class _Book:
    connection_id: str | None = None
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    valid: bool = False
    last_exchange_sequence: int | None = None
    last_local_sequence: int | None = None
    quality: BookState = "invalid_waiting_snapshot"


class OrderBookReconstructor:
    """Streaming state machine; it never infers continuity across sessions."""

    def __init__(self) -> None:
        self._books: dict[str, _Book] = {}

    def disconnect(self, symbol: str) -> ReconstructionResult:
        book = self._books.setdefault(symbol, _Book())
        book.valid = False
        book.quality = "invalid_waiting_snapshot"
        book.last_exchange_sequence = None
        book.last_local_sequence = None
        return self._result(book)

    def apply(self, event: OrderBookRecord) -> ReconstructionResult:
        symbol = event.provenance.symbol
        book = self._books.setdefault(symbol, _Book())
        if book.connection_id != event.provenance.connection_id:
            book.bids.clear()
            book.asks.clear()
            book.valid = False
            book.quality = "invalid_waiting_snapshot"
            book.last_exchange_sequence = None
            book.last_local_sequence = None
            book.connection_id = event.provenance.connection_id
        if event.message_type == "Snapshot":
            book.bids = _snapshot_side(event.bids)
            book.asks = _snapshot_side(event.asks)
            book.valid = True
            book.quality = _snapshot_quality(event)
        elif not book.valid:
            return self._result(book)
        elif _sequence_invalid(book, event):
            book.valid = False
            book.quality = "invalid_sequence"
            return self._result(book)
        else:
            _apply_changes(book.bids, event.bids)
            _apply_changes(book.asks, event.asks)
        book.last_exchange_sequence = event.provenance.exchange_sequence
        book.last_local_sequence = event.provenance.local_sequence
        if not book.bids or not book.asks or max(book.bids) >= min(book.asks):
            book.valid = False
            book.quality = "invalid_crossed"
        return self._result(book)

    @staticmethod
    def _result(book: _Book) -> ReconstructionResult:
        if not book.valid:
            return ReconstructionResult(book.quality, None, None, 0)
        return ReconstructionResult(
            book.quality,
            max(book.bids),
            min(book.asks),
            len(book.bids) + len(book.asks),
        )


def _snapshot_side(levels: tuple[BookLevel, ...]) -> dict[Decimal, Decimal]:
    return {level.price: level.quantity for level in levels if level.quantity > 0}


def _apply_changes(book: dict[Decimal, Decimal], levels: tuple[BookLevel, ...]) -> None:
    for level in levels:
        if level.quantity == 0:
            book.pop(level.price, None)
        else:
            book[level.price] = level.quantity


def _snapshot_quality(event: OrderBookRecord) -> BookState:
    if event.provenance.data_quality == "best_effort_legacy":
        return "valid_best_effort_legacy"
    if event.provenance.exchange_sequence is None:
        return "valid_sequence_unverified"
    return "valid_sequence_verified"


def _sequence_invalid(book: _Book, event: OrderBookRecord) -> bool:
    current_exchange = event.provenance.exchange_sequence
    if current_exchange is not None and book.last_exchange_sequence is not None:
        return current_exchange != book.last_exchange_sequence + 1
    current_local = event.provenance.local_sequence
    if current_local is not None and book.last_local_sequence is not None:
        return current_local <= book.last_local_sequence
    return False


def compare_best_quote(
    book: ReconstructionResult,
    book_event: OrderBookRecord,
    quote: BestQuoteRecord,
    *,
    tolerance: timedelta = timedelta(seconds=2),
) -> Literal["match", "mismatch", "not_comparable"]:
    if book.best_bid is None or book.best_ask is None:
        return "not_comparable"
    delta = abs(quote.provenance.available_at - book_event.provenance.available_at)
    if delta > tolerance:
        return "not_comparable"
    if book.best_bid == quote.bid_price and book.best_ask == quote.ask_price:
        return "match"
    return "mismatch"
