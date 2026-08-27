"""Deterministic causal ``market_state_1s`` construction from normalized events."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trading_bot.normalization.orderbook import OrderBookReconstructor
from trading_bot.normalization.parsers import BookLevel, OrderBookRecord, Provenance
from trading_bot.research.pipeline import (
    MAX_STALE_BOOK_SECONDS,
    MAX_STALE_FUNDING_SECONDS,
    MAX_STALE_MARK_SECONDS,
    MAX_STALE_QUOTE_SECONDS,
    MAX_STALE_SPOT_SECONDS,
    RESEARCH_PIPELINE_VERSION,
)


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _floor_second(value: datetime) -> datetime:
    value = value.astimezone(UTC)
    return value.replace(microsecond=0)


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


@dataclass
class _AsOf:
    available_at: datetime
    raw_event_id: int
    payload: dict[str, Any]


def _load_topic(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = list(pq.read_table(path).to_pylist())
    rows.sort(key=lambda r: (_dt(r["available_at"]), int(r["raw_event_id"])))
    return rows


def _book_levels(raw_json: str) -> tuple[BookLevel, ...]:
    items = json.loads(raw_json)
    return tuple(
        BookLevel(price=Decimal(item["price"]), quantity=Decimal(item["quantity"]))
        for item in items
    )


def _to_orderbook_record(row: dict[str, Any]) -> OrderBookRecord:
    avail = _dt(row["available_at"])
    prov = Provenance(
        raw_event_id=int(row["raw_event_id"]),
        received_at=avail,
        available_at=avail,
        exchange_at=_dt(row["exchange_at"]) if row.get("exchange_at") else None,
        symbol=str(row["symbol"]),
        source=str(row["source"]),
        connection_id=row.get("connection_id"),
        local_sequence=row.get("local_sequence"),
        exchange_sequence=row.get("exchange_sequence"),
        raw_schema_version=int(row["raw_schema_version"]),
        pipeline_version=int(row["pipeline_version"]),
        data_quality=row["data_quality"],
    )
    return OrderBookRecord(
        provenance=prov,
        message_type=row["message_type"],
        depth=int(row["depth"]),
        granularity=Decimal(str(row["granularity"])),
        bids=_book_levels(row["bids_json"]),
        asks=_book_levels(row["asks_json"]),
    )


def build_market_state_1s(
    normalized_dir: Path,
    output_path: Path,
    *,
    symbol: str = "ETH/USDT-P",
) -> dict[str, Any]:
    """Build one causal 1s market-state row when usable prices exist.

    Decision time is the floor second ``T``. Only information with
    ``available_at <= T`` may enter the row (no lookahead).
    """

    orderbook = _load_topic(normalized_dir / "orderbook.parquet")
    quotes = _load_topic(normalized_dir / "ask_bid_price.parquet")
    marks = _load_topic(normalized_dir / "mark_price.parquet")
    spots = _load_topic(normalized_dir / "spot_price.parquet")
    funding = _load_topic(normalized_dir / "funding_rate_estimation.parquet")
    trades = _load_topic(normalized_dir / "trades.parquet")

    if not orderbook and not quotes:
        raise ValueError("market_state_1s requires orderbook or ask_bid_price events")

    all_times = [_dt(r["available_at"]) for r in orderbook + quotes + trades]
    start = _floor_second(min(all_times))
    end = _floor_second(max(all_times))

    trade_buckets: dict[datetime, list[dict[str, Any]]] = {}
    for tr in trades:
        key = _floor_second(_dt(tr["available_at"]))
        trade_buckets.setdefault(key, []).append(tr)

    reconstructor = OrderBookReconstructor()
    ob_i = q_i = m_i = s_i = f_i = 0
    last_quote: _AsOf | None = None
    last_mark: _AsOf | None = None
    last_spot: _AsOf | None = None
    last_funding: _AsOf | None = None
    last_book_at: datetime | None = None
    last_book_id: int | None = None
    last_connection: str | None = None
    last_book_state = "invalid_waiting_snapshot"
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    mid_history: list[tuple[datetime, float]] = []
    previous_top: tuple[Decimal, Decimal, Decimal, Decimal] | None = None
    ofi_history: list[tuple[datetime, float]] = []
    rows_out: list[dict[str, Any]] = []

    cursor = start
    while cursor <= end:
        while ob_i < len(orderbook) and _dt(orderbook[ob_i]["available_at"]) <= cursor:
            record = _to_orderbook_record(orderbook[ob_i])
            result = reconstructor.apply(record)
            last_book_state = result.state
            last_book_at = record.provenance.available_at
            last_book_id = record.provenance.raw_event_id
            last_connection = record.provenance.connection_id
            best_bid, best_ask = result.best_bid, result.best_ask
            ob_i += 1
        while q_i < len(quotes) and _dt(quotes[q_i]["available_at"]) <= cursor:
            last_quote = _AsOf(
                _dt(quotes[q_i]["available_at"]),
                int(quotes[q_i]["raw_event_id"]),
                quotes[q_i],
            )
            q_i += 1
        while m_i < len(marks) and _dt(marks[m_i]["available_at"]) <= cursor:
            last_mark = _AsOf(
                _dt(marks[m_i]["available_at"]),
                int(marks[m_i]["raw_event_id"]),
                marks[m_i],
            )
            m_i += 1
        while s_i < len(spots) and _dt(spots[s_i]["available_at"]) <= cursor:
            last_spot = _AsOf(
                _dt(spots[s_i]["available_at"]),
                int(spots[s_i]["raw_event_id"]),
                spots[s_i],
            )
            s_i += 1
        while f_i < len(funding) and _dt(funding[f_i]["available_at"]) <= cursor:
            last_funding = _AsOf(
                _dt(funding[f_i]["available_at"]),
                int(funding[f_i]["raw_event_id"]),
                funding[f_i],
            )
            f_i += 1

        valid_book = (
            last_book_state.startswith("valid_")
            and best_bid is not None
            and best_ask is not None
        )
        book_age = (
            (cursor - last_book_at).total_seconds() if last_book_at is not None else None
        )
        if valid_book and book_age is not None and book_age > MAX_STALE_BOOK_SECONDS:
            valid_book = False

        bid_size = ask_size = None
        quote_age = None
        quote_fresh = False
        if last_quote is not None:
            bid_size = _dec(last_quote.payload.get("bid_size"))
            ask_size = _dec(last_quote.payload.get("ask_size"))
            quote_age = (cursor - last_quote.available_at).total_seconds()
            quote_fresh = quote_age <= MAX_STALE_QUOTE_SECONDS

        # Emit only from a fresh valid book or a fresh quote. Stale reconstructed
        # tops must not bridge multi-hour archive gaps with invented 1s rows.
        emit_bid = best_bid if valid_book else None
        emit_ask = best_ask if valid_book else None
        if not valid_book and quote_fresh and last_quote is not None:
            emit_bid = _dec(last_quote.payload["bid_price"])
            emit_ask = _dec(last_quote.payload["ask_price"])

        current_top: tuple[Decimal, Decimal, Decimal, Decimal] | None = None
        if quote_fresh and last_quote is not None and bid_size is not None and ask_size is not None:
            quote_bid = _dec(last_quote.payload["bid_price"])
            quote_ask = _dec(last_quote.payload["ask_price"])
            if quote_bid is not None and quote_ask is not None:
                current_top = (quote_bid, bid_size, quote_ask, ask_size)

        ofi_1s = None
        if current_top is not None and previous_top is not None:
            bid_price, current_bid_size, ask_price, current_ask_size = current_top
            prev_bid_price, prev_bid_size, prev_ask_price, prev_ask_size = previous_top
            bid_flow = (
                current_bid_size
                if bid_price > prev_bid_price
                else current_bid_size - prev_bid_size
                if bid_price == prev_bid_price
                else Decimal(0)
            )
            ask_flow = (
                current_ask_size
                if ask_price < prev_ask_price
                else current_ask_size - prev_ask_size
                if ask_price == prev_ask_price
                else Decimal(0)
            )
            ofi_1s = float(bid_flow - ask_flow)
            ofi_history.append((cursor, ofi_1s))
        # A missing/stale quote breaks the one-second OFI chain instead of bridging a gap.
        previous_top = current_top
        while ofi_history and (cursor - ofi_history[0][0]).total_seconds() >= 15:
            ofi_history.pop(0)

        def _ofi_sum(
            seconds: int,
            *,
            current_ofi: float | None = ofi_1s,
            at: datetime = cursor,
        ) -> float | None:
            if current_ofi is None:
                return None
            return sum(
                value
                for ts, value in ofi_history
                if (at - ts).total_seconds() < seconds
            )

        if emit_bid is None or emit_ask is None or emit_bid >= emit_ask:
            # Jump toward the next available event when both book and quote are stale
            # so discontinuous archive windows do not burn CPU on empty hours.
            if not valid_book and not quote_fresh:
                next_times: list[datetime] = []
                if ob_i < len(orderbook):
                    next_times.append(_floor_second(_dt(orderbook[ob_i]["available_at"])))
                if q_i < len(quotes):
                    next_times.append(_floor_second(_dt(quotes[q_i]["available_at"])))
                future_trade_seconds = [ts for ts in trade_buckets if ts > cursor]
                if future_trade_seconds:
                    next_times.append(min(future_trade_seconds))
                if next_times:
                    jump_to = min(next_times)
                    if jump_to > cursor:
                        # Gap jump breaks continuity; do not carry returns/OFI across it.
                        mid_history.clear()
                        ofi_history.clear()
                        previous_top = None
                        cursor = jump_to
                        continue
            cursor += timedelta(seconds=1)
            continue

        mid = float((emit_bid + emit_ask) / 2)
        spread = float(emit_ask - emit_bid)
        spread_bps = spread / mid * 10_000 if mid else None
        microprice = None
        imbalance = None
        if bid_size is not None and ask_size is not None and quote_fresh:
            bs = float(bid_size)
            as_ = float(ask_size)
            denom = bs + as_
            if denom > 0:
                imbalance = (bs - as_) / denom
                microprice = float(
                    (
                        emit_ask * Decimal(str(bs)) + emit_bid * Decimal(str(as_))
                    )
                    / Decimal(str(denom))
                )

        mid_history.append((cursor, mid))
        while mid_history and (cursor - mid_history[0][0]).total_seconds() > 60:
            mid_history.pop(0)

        decision_cursor = cursor
        decision_mid = mid

        def _ret(
            seconds: int,
            *,
            at: datetime = decision_cursor,
            price_now: float = decision_mid,
        ) -> float | None:
            target = at - timedelta(seconds=seconds)
            for ts, price in reversed(mid_history):
                if ts <= target:
                    return (price_now / price - 1.0) * 10_000
            return None

        def _vol(seconds: int, *, at: datetime = decision_cursor) -> float | None:
            window = [p for ts, p in mid_history if (at - ts).total_seconds() <= seconds]
            if len(window) < 3:
                return None
            rets = [
                (window[i] / window[i - 1] - 1.0)
                for i in range(1, len(window))
                if window[i - 1] > 0
            ]
            if len(rets) < 2:
                return None
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            return float((var**0.5) * 10_000)

        bucket_trades = trade_buckets.get(cursor, [])
        buy_vol = sum(
            float(t["quantity"]) for t in bucket_trades if t["taker_side"] == "Buy"
        )
        sell_vol = sum(
            float(t["quantity"]) for t in bucket_trades if t["taker_side"] == "Sell"
        )
        trade_count = len(bucket_trades)
        # Signed trade flow is taker-initiated volume; it is not order-flow imbalance (OFI).
        signed_trade_flow = buy_vol - sell_vol
        vwap = None
        if trade_count:
            notional = sum(
                float(t["quantity"]) * float(t["price"]) for t in bucket_trades
            )
            qty = buy_vol + sell_vol
            vwap = notional / qty if qty else None

        def _age(item: _AsOf | None, *, at: datetime = decision_cursor) -> float | None:
            return None if item is None else (at - item.available_at).total_seconds()

        mark = None
        mark_age = _age(last_mark)
        if last_mark is not None and mark_age is not None and mark_age <= MAX_STALE_MARK_SECONDS:
            mark = float(last_mark.payload["price"])
        spot = None
        spot_age = _age(last_spot)
        if last_spot is not None and spot_age is not None and spot_age <= MAX_STALE_SPOT_SECONDS:
            spot = float(last_spot.payload["price"])
        funding_rate = None
        funding_age = _age(last_funding)
        if (
            last_funding is not None
            and funding_age is not None
            and funding_age <= MAX_STALE_FUNDING_SECONDS
        ):
            funding_rate = float(last_funding.payload["estimated_rate"])

        basis_mark = (mark - mid) if mark is not None else None
        basis_spot = (spot - mid) if spot is not None else None

        rows_out.append(
            {
                "decision_time": cursor,
                "symbol": symbol,
                "latest_raw_event_id": last_book_id
                or (last_quote.raw_event_id if last_quote else None),
                "connection_id": last_connection
                or (last_quote.payload.get("connection_id") if last_quote else None),
                "best_bid": float(emit_bid),
                "best_ask": float(emit_ask),
                "mid": mid,
                "spread": spread,
                "spread_bps": spread_bps,
                "bid_size": float(bid_size) if bid_size is not None else None,
                "ask_size": float(ask_size) if ask_size is not None else None,
                "imbalance": imbalance,
                "microprice": microprice,
                "microprice_dev_bps": (
                    (microprice / mid - 1.0) * 10_000
                    if microprice is not None and mid
                    else None
                ),
                "buy_volume": buy_vol,
                "sell_volume": sell_vol,
                "signed_trade_flow_1s": signed_trade_flow,
                "ofi_1s": ofi_1s,
                "ofi_5s": _ofi_sum(5),
                "ofi_15s": _ofi_sum(15),
                "trade_count": trade_count,
                "vwap": vwap,
                "mark_price": mark,
                "spot_price": spot,
                "funding_rate": funding_rate,
                "basis_mark": basis_mark,
                "basis_mark_bps": (
                    basis_mark / mid * 10_000 if basis_mark is not None else None
                ),
                "basis_spot": basis_spot,
                "basis_spot_bps": (
                    basis_spot / mid * 10_000 if basis_spot is not None else None
                ),
                "ret_1s_bps": _ret(1),
                "ret_5s_bps": _ret(5),
                "ret_15s_bps": _ret(15),
                "ret_60s_bps": _ret(60),
                "rv_5s_bps": _vol(5),
                "rv_15s_bps": _vol(15),
                "rv_60s_bps": _vol(60),
                "book_age_seconds": book_age,
                "quote_age_seconds": quote_age,
                "mark_age_seconds": mark_age,
                "spot_age_seconds": spot_age,
                "funding_age_seconds": funding_age,
                "valid_book": valid_book,
                "book_state": last_book_state,
                "quote_fresh": quote_fresh,
                "research_pipeline_version": RESEARCH_PIPELINE_VERSION,
            }
        )
        cursor += timedelta(seconds=1)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows_out), output_path, compression="zstd")
    return {
        "rows": len(rows_out),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "path": str(output_path),
        "valid_book_rows": sum(1 for r in rows_out if r["valid_book"]),
    }
