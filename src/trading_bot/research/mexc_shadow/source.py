"""Read-only market-data boundary. No orders, no private endpoints, no credentials."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from trading_bot.research.mexc_shadow.safety import assert_no_credential_keys
from trading_bot.research.mexc_shadow.types import Observation
from trading_bot.research.mexc_shadow.ui_capture.normalize import (
    observation_from_snapshot,
    snapshot_from_mapping,
)


class MarketDataSource(Protocol):
    """Pull interface for already-observed public quotes."""

    def iter_observations(self) -> Iterator[Observation]:
        """Yield causal observations. Must not submit or cancel orders."""


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _pos(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _levels(value: Any) -> tuple[tuple[float, float], ...] | None:
    """Parse optional [price, size] depth. Empty or absent → None."""

    if value is None:
        return None
    if not isinstance(value, list | tuple):
        raise ValueError("orderbook levels must be a list")
    out: list[tuple[float, float]] = []
    for item in value:
        if isinstance(item, Mapping):
            price = _pos(item.get("price") or item.get("p"))
            size = _pos(item.get("size") or item.get("qty") or item.get("q"))
        elif isinstance(item, list | tuple) and len(item) >= 2:
            price = _pos(item[0])
            size = _pos(item[1])
        else:
            raise ValueError("orderbook level must be [price, size]")
        if price is None or size is None:
            continue
        out.append((price, size))
    return tuple(out) or None


def observation_from_mapping(row: Mapping[str, Any]) -> Observation:
    """Map a captured UI/replay row. Unknown extra keys are ignored."""

    assert_no_credential_keys(dict(row))
    if str(row.get("schema") or "") == "mexc_ui_raw_snapshot":
        record = observation_from_snapshot(snapshot_from_mapping(row))
        if record.observation is None:
            raise ValueError(record.skipped_reason or "invalid capture snapshot")
        return record.observation
    bid = _pos(row.get("bid") or row.get("bidPrice") or row.get("bid_price"))
    ask = _pos(row.get("ask") or row.get("askPrice") or row.get("ask_price"))
    if bid is None or ask is None:
        raise ValueError("observation requires positive bid and ask")
    if bid >= ask:
        raise ValueError("observation bid must be strictly below ask")
    observed_raw = row.get("observed_at") or row.get("source_timestamp") or row["received_at"]
    observed = _dt(observed_raw)
    received = _dt(row.get("received_at") or observed)
    mid = _pos(row.get("mid") or row.get("midPrice"))
    raw_book = row.get("orderbook")
    bids_raw = row.get("orderbook_bids") or row.get("bids")
    asks_raw = row.get("orderbook_asks") or row.get("asks")
    if isinstance(raw_book, Mapping):
        bids_raw = raw_book.get("bids", bids_raw)
        asks_raw = raw_book.get("asks", asks_raw)
    return Observation(
        observed_at=observed,
        received_at=received,
        symbol=str(row["symbol"]),
        bid=bid,
        ask=ask,
        source=str(row.get("source") or "mexc_ui_observer"),
        mid=mid,
        last=_pos(row.get("last") or row.get("lastPrice")),
        mark=_pos(row.get("mark") or row.get("markPrice") or row.get("fairPrice")),
        index=_pos(row.get("index") or row.get("indexPrice")),
        bid_size=_pos(row.get("bid_size") or row.get("bidSize")),
        ask_size=_pos(row.get("ask_size") or row.get("askSize")),
        orderbook_bids=_levels(bids_raw),
        orderbook_asks=_levels(asks_raw),
    )


class MexcUiObserver:
    """Read-only adapter over already-captured MEXC UI quote snapshots.

    Accepts flat observer rows or capture schema v1. Python does not open a
    browser, send HTTP, or accept API keys.
    """

    def __init__(self, snapshots: Sequence[Mapping[str, Any]]) -> None:
        self._snapshots = list(snapshots)

    def iter_observations(self) -> Iterator[Observation]:
        for row in self._snapshots:
            yield observation_from_mapping(row)


class ReplayFixtureSource:
    """Deterministic JSON list of public observations (tests and offline replay)."""

    def __init__(self, path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("replay fixture must be a JSON array")
        self._observer = MexcUiObserver(payload)

    def iter_observations(self) -> Iterator[Observation]:
        yield from self._observer.iter_observations()


class MemorySource:
    """In-process observation list for unit tests."""

    def __init__(self, observations: Iterable[Observation]) -> None:
        self._rows = list(observations)

    def iter_observations(self) -> Iterator[Observation]:
        yield from self._rows
