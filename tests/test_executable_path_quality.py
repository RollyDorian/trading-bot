"""Executable TOB path quality: stale/fallback cannot resolve barriers.

Feed-semantics eligibility only. No TP/SL retune, no ML.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from tests.test_normalization_parsers import raw
from trading_bot.research.pipeline.executable_tob import (
    EXECUTABLE_STALENESS_SECONDS,
    TOB_SOURCE_DIRECT_QUOTE_FRESH,
    TOB_SOURCE_QUOTE_FALLBACK,
    TOB_SOURCE_RECONSTRUCTED_BOOK_FRESH,
    TOB_SOURCE_STALE_CARRY,
    is_executable_tob_source,
)
from trading_bot.research.pipeline.first_passage_opportunity import analyze_first_passage
from trading_bot.research.pipeline.market_state import build_market_state_1s
from trading_bot.research.pipeline.normalize_offline import normalize_events_parquet
from trading_bot.research.pipeline.tp_sl_first_touch import analyze_tp_sl_first_touch


def _series(
    mids: list[float],
    *,
    spread: float = 0.0002,
    start: datetime | None = None,
    tob_source: str = TOB_SOURCE_DIRECT_QUOTE_FRESH,
    connection: str = "conn-a",
) -> dict[str, list[object]]:
    base = start or datetime(2026, 8, 19, 20, 50, tzinfo=UTC)
    epoch: list[int] = []
    bid: list[float] = []
    ask: list[float] = []
    mid_out: list[float] = []
    sources: list[str] = []
    conns: list[str] = []
    half = spread / 2.0
    for i, mid in enumerate(mids):
        ts = base + timedelta(seconds=i)
        epoch.append(int(ts.timestamp()))
        bid.append(mid - half)
        ask.append(mid + half)
        mid_out.append(mid)
        sources.append(tob_source)
        conns.append(connection)
    return {
        "epoch": epoch,
        "bid": bid,
        "ask": ask,
        "mid": mid_out,
        "tob_source": sources,
        "connection_id": conns,
    }


def test_documented_staleness_bound_is_not_fitted() -> None:
    assert EXECUTABLE_STALENESS_SECONDS == 5.0
    assert is_executable_tob_source(TOB_SOURCE_DIRECT_QUOTE_FRESH)
    assert is_executable_tob_source(TOB_SOURCE_RECONSTRUCTED_BOOK_FRESH)
    assert not is_executable_tob_source(TOB_SOURCE_QUOTE_FALLBACK)
    assert not is_executable_tob_source(TOB_SOURCE_STALE_CARRY)


def test_stale_quote_cannot_trigger_tp() -> None:
    # Quiet then a +25 bps print that would hit TP20 if it were executable.
    mids = [100.0] * 5 + [100.25] + [100.25] * 20
    packed = _series(mids)
    sources = list(packed["tob_source"])
    sources[5] = TOB_SOURCE_STALE_CARRY
    report = analyze_tp_sl_first_touch(
        packed["epoch"],  # type: ignore[arg-type]
        packed["bid"],  # type: ignore[arg-type]
        packed["ask"],  # type: ignore[arg-type]
        packed["mid"],  # type: ignore[arg-type]
        horizons=(8,),
        tp_grid=(20.0,),
        sl_grid=(10.0,),
        delays_seconds=(0,),
        tob_source=sources,
    )
    cell = report["delay_0s"]["rolling_1s"]["8s"]["20"]["10"]["long"]
    assert cell["n_tp_first"] == 0
    assert cell["n_data_invalid"] >= 1


def test_fallback_quote_cannot_trigger_tp_if_not_executable() -> None:
    mids = [100.0] * 5 + [100.25] + [100.25] * 20
    packed = _series(mids)
    sources = list(packed["tob_source"])
    sources[5] = TOB_SOURCE_QUOTE_FALLBACK
    report = analyze_tp_sl_first_touch(
        packed["epoch"],  # type: ignore[arg-type]
        packed["bid"],  # type: ignore[arg-type]
        packed["ask"],  # type: ignore[arg-type]
        packed["mid"],  # type: ignore[arg-type]
        horizons=(8,),
        tp_grid=(20.0,),
        sl_grid=(10.0,),
        delays_seconds=(0,),
        tob_source=sources,
    )
    cell = report["delay_0s"]["rolling_1s"]["8s"]["20"]["10"]["long"]
    assert cell["n_tp_first"] == 0
    assert cell["n_data_invalid"] >= 1


def test_data_gap_before_barrier_is_data_invalid_not_timeout() -> None:
    # 4s of prices, then a 2s hole, then a move. H=8 cannot be observed.
    base = datetime(2026, 8, 19, 21, 8, tzinfo=UTC)
    epoch = [int((base + timedelta(seconds=i)).timestamp()) for i in range(4)]
    epoch += [int((base + timedelta(seconds=i)).timestamp()) for i in range(8, 16)]
    mid = [100.0] * 4 + [100.0] * 8
    bid = [m - 0.0001 for m in mid]
    ask = [m + 0.0001 for m in mid]
    report = analyze_tp_sl_first_touch(
        epoch,
        bid,
        ask,
        mid,
        horizons=(8,),
        tp_grid=(20.0,),
        sl_grid=(10.0,),
        delays_seconds=(0,),
        tob_source=[TOB_SOURCE_DIRECT_QUOTE_FRESH] * len(epoch),
    )
    cell = report["delay_0s"]["rolling_1s"]["8s"]["20"]["10"]["long"]
    assert cell["n_timeout"] == 0
    assert cell["n_data_invalid"] >= 1


def test_reconnect_boundary_invalidates_path_until_resync() -> None:
    mids = [100.0] * 20
    packed = _series(mids)
    conns = list(packed["connection_id"])
    for i in range(4, len(conns)):
        conns[i] = "conn-b"
    report = analyze_tp_sl_first_touch(
        packed["epoch"],  # type: ignore[arg-type]
        packed["bid"],  # type: ignore[arg-type]
        packed["ask"],  # type: ignore[arg-type]
        packed["mid"],  # type: ignore[arg-type]
        horizons=(8,),
        tp_grid=(20.0,),
        sl_grid=(10.0,),
        delays_seconds=(0,),
        tob_source=packed["tob_source"],  # type: ignore[arg-type]
        connection_id=conns,
    )
    cell = report["delay_0s"]["rolling_1s"]["8s"]["20"]["10"]["long"]
    # Starts on conn-a cannot observe through the reconnect; that is not TIMEOUT.
    assert cell["n_data_invalid"] >= 1
    # Starts after resync on conn-b can still time out on a quiet path.
    assert cell["n_timeout"] >= 1


def test_fresh_book_normal_first_touch_unchanged() -> None:
    mids = [100.0 + 0.05 * i for i in range(12)]
    packed = _series(mids, tob_source=TOB_SOURCE_RECONSTRUCTED_BOOK_FRESH)
    report = analyze_tp_sl_first_touch(
        packed["epoch"],  # type: ignore[arg-type]
        packed["bid"],  # type: ignore[arg-type]
        packed["ask"],  # type: ignore[arg-type]
        packed["mid"],  # type: ignore[arg-type]
        horizons=(10,),
        tp_grid=(20.0,),
        sl_grid=(10.0,),
        delays_seconds=(0,),
        tob_source=packed["tob_source"],  # type: ignore[arg-type]
    )
    cell = report["delay_0s"]["rolling_1s"]["10s"]["20"]["10"]["long"]
    assert cell["n_tp_first"] >= 1
    assert cell["n_tp_or_sl_resolved_on_stale_or_quote_fallback"] == 0


def test_genuine_large_move_across_fresh_quotes_survives() -> None:
    # Ramp ~8 bps/s so TP 20 hits without a 1s span covering TP+SL.
    mids = [100.0 + 0.08 * i for i in range(12)]
    packed = _series(mids)
    tp_report = analyze_tp_sl_first_touch(
        packed["epoch"],  # type: ignore[arg-type]
        packed["bid"],  # type: ignore[arg-type]
        packed["ask"],  # type: ignore[arg-type]
        packed["mid"],  # type: ignore[arg-type]
        horizons=(8,),
        tp_grid=(20.0,),
        sl_grid=(10.0,),
        delays_seconds=(0,),
        tob_source=packed["tob_source"],  # type: ignore[arg-type]
    )
    fp = analyze_first_passage(
        packed["epoch"],  # type: ignore[arg-type]
        packed["bid"],  # type: ignore[arg-type]
        packed["ask"],  # type: ignore[arg-type]
        packed["mid"],  # type: ignore[arg-type]
        horizons=(5, 10),
        thresholds=(20.0, 50.0),
        tob_source=packed["tob_source"],  # type: ignore[arg-type]
    )
    cell = tp_report["delay_0s"]["rolling_1s"]["8s"]["20"]["10"]["long"]
    assert cell["n_tp_first"] >= 1
    exec_10 = fp["executable_tob"]["rolling_1s"]["10s"]["thresholds"]["50"]
    assert exec_10["long_hit_count"] >= 1


def test_first_passage_gap_is_data_invalid_not_no_hit() -> None:
    """An incomplete H is DATA_INVALID, not a zero-hit TIMEOUT analogue."""

    base = datetime(2026, 8, 19, 20, 50, tzinfo=UTC)
    epoch = [int((base + timedelta(seconds=i)).timestamp()) for i in range(6)]
    mid = [100.0] * 6
    bid = [m - 0.0001 for m in mid]
    ask = [m + 0.0001 for m in mid]
    report = analyze_first_passage(
        epoch,
        bid,
        ask,
        mid,
        horizons=(5, 10),
        thresholds=(20.0,),
        tob_source=[TOB_SOURCE_DIRECT_QUOTE_FRESH] * len(epoch),
    )
    h5 = report["executable_tob"]["rolling_1s"]["5s"]["thresholds"]["20"]
    h10 = report["executable_tob"]["rolling_1s"]["10s"]["thresholds"]["20"]
    assert h5["n_valid_starts"] >= 1
    assert h10["n_valid_starts"] == 0
    assert h10["n_data_invalid"] >= 1


def test_market_state_prefers_fresh_quote_over_ghost_book(tmp_path: Path) -> None:
    """Native BBO wins over a reconstructed ask left behind by missed deletes."""

    base = datetime(2026, 8, 19, 21, 8, tzinfo=UTC)
    rows: list[dict[str, object]] = []
    raw_id = 13_800_000
    local_seq = 0
    connection = "conn-ghost"

    def add(topic: str, fixture: str, when: datetime) -> None:
        nonlocal raw_id, local_seq
        local_seq += 1
        event = raw(fixture, raw_id=raw_id)
        payload = json.loads(json.dumps(event.payload))
        rows.append(
            {
                "raw_event_id": raw_id,
                "received_at": when,
                "exchange_at": event.exchange_at,
                "source": "hibachi_ws",
                "topic": topic,
                "symbol": "ETH/USDT-P",
                "sequence": event.sequence,
                "connection_id": connection,
                "local_sequence": local_seq,
                "exchange_sequence": None,
                "raw_schema_version": 2,
                "latency_ms": 1.0,
                "payload_json": json.dumps(payload),
            }
        )
        raw_id += 1

    snap = raw("orderbook_snapshot")
    snap_payload = json.loads(json.dumps(snap.payload))
    snap_payload["data"]["ask"]["levels"][0]["price"] = "2374.98"
    snap_payload["data"]["ask"]["startPrice"] = "2374.98"
    add("orderbook", "orderbook_snapshot", base)
    rows[-1]["payload_json"] = json.dumps(snap_payload)
    quote = raw("ask_bid_price")
    quote_payload = json.loads(json.dumps(quote.payload))
    quote_payload["data"]["bidPrice"] = "2315.10"
    quote_payload["data"]["askPrice"] = "2315.40"
    quote_payload["data"]["timestampMs"] = 1_787_170_080_000
    # Same floor-second as the snapshot so the quote is causally available at T.
    add("ask_bid_price", "ask_bid_price", base)
    rows[-1]["payload_json"] = json.dumps(quote_payload)
    for sec in range(1, 4):
        add("orderbook", "orderbook_empty_update", base + timedelta(seconds=sec))
        add("ask_bid_price", "ask_bid_price", base + timedelta(seconds=sec))
        rows[-1]["payload_json"] = json.dumps(quote_payload)

    events = tmp_path / "events.parquet"
    pq.write_table(pa.Table.from_pylist(rows), events)
    norm = tmp_path / "normalized_events"
    stats = normalize_events_parquet(events, norm)
    assert stats.by_topic.get("ask_bid_price", 0) >= 1
    assert stats.error_rows == 0
    ms_path = tmp_path / "market_state_1s.parquet"
    build_market_state_1s(norm, ms_path)
    out = pq.read_table(ms_path).to_pylist()
    assert out
    assert all(row["tob_source"] == TOB_SOURCE_DIRECT_QUOTE_FRESH for row in out)
    assert all(abs(float(row["best_ask"]) - 2315.40) < 0.01 for row in out)
    assert all(float(row["best_ask"]) < 2400.0 for row in out)


def test_normalize_writes_trades_when_exchange_at_is_all_null(tmp_path: Path) -> None:
    """Archive trades often have null envelope exchange_at; that must still flush."""

    event = raw("trades")
    rows = [
        {
            "raw_event_id": 42,
            "received_at": datetime(2026, 8, 11, 12, tzinfo=UTC),
            "exchange_at": None,
            "source": "hibachi_ws",
            "topic": "trades",
            "symbol": "ETH/USDT-P",
            "sequence": None,
            "connection_id": "conn-a",
            "local_sequence": 1,
            "exchange_sequence": None,
            "raw_schema_version": 2,
            "latency_ms": 1.0,
            "payload_json": json.dumps(event.payload),
        }
    ]
    events = tmp_path / "events.parquet"
    pq.write_table(pa.Table.from_pylist(rows), events)
    norm = tmp_path / "normalized_events"
    stats = normalize_events_parquet(events, norm)
    assert stats.trade_rows == 1
    assert stats.error_rows == 0
    assert (norm / "trades.parquet").is_file()
    trades = pq.read_table(norm / "trades.parquet").to_pylist()
    assert len(trades) == 1
    assert trades[0]["price"] == "2000.25"
