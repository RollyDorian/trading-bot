"""Executable top-of-book source labels and eligibility.

Eligibility is frozen from feed semantics and the documented 5s research
staleness bound in ``MAX_STALE_QUOTE_SECONDS`` / ``MAX_STALE_BOOK_SECONDS``.
Do not retune that bound from TP/SL or first-passage hit rates.
"""

from __future__ import annotations

from trading_bot.research.pipeline import MAX_STALE_BOOK_SECONDS, MAX_STALE_QUOTE_SECONDS

# Native exchange BBO (ask_bid_price) received within the documented quote bound.
TOB_SOURCE_DIRECT_QUOTE_FRESH = "DIRECT_QUOTE_FRESH"
# Reconstructed L2 top, valid reconstructor state, within the documented book bound,
# used only when no fresh native quote exists for this second.
TOB_SOURCE_RECONSTRUCTED_BOOK_FRESH = "RECONSTRUCTED_BOOK_FRESH"
# Fresh quote used as a substitute while a book was invalid. Not executable:
# the native BBO path is DIRECT_QUOTE_FRESH; this label is for injected/legacy rows.
TOB_SOURCE_QUOTE_FALLBACK = "QUOTE_FALLBACK"
# Forward-filled book/quote older than the documented bound. Never executable.
TOB_SOURCE_STALE_CARRY = "STALE_CARRY"
# No causally valid TOB (gap, desync, waiting snapshot, crossed, reconnect).
TOB_SOURCE_INVALID = "INVALID"

EXECUTABLE_TOB_SOURCES = frozenset(
    {
        TOB_SOURCE_DIRECT_QUOTE_FRESH,
        TOB_SOURCE_RECONSTRUCTED_BOOK_FRESH,
    }
)

# Same numeric bound as production/research pipeline cadence (not fitted).
EXECUTABLE_STALENESS_SECONDS = min(MAX_STALE_BOOK_SECONDS, MAX_STALE_QUOTE_SECONDS)

EXECUTABLE_TOB_ELIGIBILITY = {
    "rule": (
        "A decision/path second is executable only when bid/ask come from a "
        "causally valid fresh native quote or a fresh reconstructed book under "
        f"the documented {EXECUTABLE_STALENESS_SECONDS:g}s staleness limit, with "
        "no unresolved gap/desync/reconnect boundary on the open window."
    ),
    "preferred_source": TOB_SOURCE_DIRECT_QUOTE_FRESH,
    "book_only_when_quote_absent_or_stale": True,
    "quote_fallback_executable": False,
    "stale_carry_executable": False,
    "silent_forward_fill_executable": False,
    "max_stale_quote_seconds": MAX_STALE_QUOTE_SECONDS,
    "max_stale_book_seconds": MAX_STALE_BOOK_SECONDS,
    "bound_source": "trading_bot.research.pipeline.MAX_STALE_*_SECONDS",
    "bound_fitted_from_tp_sl": False,
}


def is_executable_tob_source(source: str | None) -> bool:
    """True only for the two fresh, causally valid TOB sources."""

    return source in EXECUTABLE_TOB_SOURCES
