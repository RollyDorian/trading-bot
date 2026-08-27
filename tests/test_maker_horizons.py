"""Tests for maker execution, extended horizons, and event selection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from trading_bot.research.pipeline.event_selection import (
    evaluate_event_class,
    required_move_bps,
)
from trading_bot.research.pipeline.execution_styles import execution_style_matrix
from trading_bot.research.pipeline.horizons import (
    EXTENDED_LABEL_HORIZONS_SECONDS,
    write_labels_extended,
)
from trading_bot.research.pipeline.incremental import reserve_clean_oos_future
from trading_bot.research.pipeline.maker_execution import (
    MakerOrderIntent,
    post_fill_mid_moves_bps,
    simulate_maker_fill,
    summarize_maker_campaign,
)


def _rows(n: int = 40) -> list[dict]:
    base = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    rows = []
    for i in range(n):
        rows.append(
            {
                "decision_time": base + timedelta(seconds=i),
                "best_bid": 100.0,
                "best_ask": 100.1,
                "mid": 100.05 + (0.01 if i > 5 else 0.0),
                "buy_volume": 0.0,
                "sell_volume": 0.05 if i >= 3 else 0.0,
                "signed_trade_flow_1s": 1.0 if i == 0 else 0.0,
                "spread_bps": 1.0,
                "trade_count": 1,
            }
        )
    return rows


def test_maker_fill_scenarios_and_expiry() -> None:
    rows = _rows()
    by_time = {r["decision_time"]: r for r in rows}
    intent = MakerOrderIntent(
        decision_time=rows[0]["decision_time"],
        side="buy",
        limit_price=100.0,
        notional_usd=10.0,
        signal=1.0,
        feature="signed_trade_flow_1s",
        max_wait_seconds=10,
    )
    opt = simulate_maker_fill(by_time, intent, scenario="optimistic")
    assert opt.filled is True
    assert opt.time_to_fill_seconds is not None
    assert opt.time_to_fill_seconds >= 3

    # No future leakage: placement uses only decision_time; fill scans strictly later.
    assert opt.fill_time is not None
    assert opt.fill_time > intent.decision_time

    dead = MakerOrderIntent(
        decision_time=rows[0]["decision_time"],
        side="buy",
        limit_price=100.0,
        notional_usd=1_000_000.0,
        signal=1.0,
        feature="signed_trade_flow_1s",
        max_wait_seconds=2,
    )
    expired = simulate_maker_fill(by_time, dead, scenario="base")
    assert expired.filled is False
    assert expired.reason == "expired_unfilled"


def test_conservative_requires_trade_through() -> None:
    rows = _rows()
    # Keep ask above limit so conservative buy does not fill on volume alone.
    for row in rows:
        row["best_ask"] = 100.2
        row["sell_volume"] = 10.0
    by_time = {r["decision_time"]: r for r in rows}
    intent = MakerOrderIntent(
        decision_time=rows[0]["decision_time"],
        side="buy",
        limit_price=100.0,
        notional_usd=10.0,
        signal=1.0,
        feature="signed_trade_flow_1s",
        max_wait_seconds=20,
    )
    cons = simulate_maker_fill(by_time, intent, scenario="conservative")
    assert cons.filled is False

    rows[5]["best_ask"] = 99.9
    by_time = {r["decision_time"]: r for r in rows}
    cons2 = simulate_maker_fill(by_time, intent, scenario="conservative")
    assert cons2.filled is True
    assert cons2.reason == "ask_trade_through_limit"


def test_adverse_selection_and_unfilled_accounting() -> None:
    rows = _rows()
    by_time = {r["decision_time"]: r for r in rows}
    fill_time = rows[3]["decision_time"]
    moves = post_fill_mid_moves_bps(
        by_time, fill_time=fill_time, fill_side="buy", horizons=(1, 5)
    )
    assert "1s" in moves
    summary = summarize_maker_campaign(
        rows,
        feature="signed_trade_flow_1s",
        abs_threshold=0.5,
        notional_usd=10.0,
        max_wait_seconds=10,
    )
    base = summary["scenarios"]["base"]
    assert base["submitted"] >= 1
    assert base["unfilled_rate"] is not None
    # Unfilled are counted against fill_rate, never as trades.
    assert base["fills"] + round(base["unfilled_rate"] * base["submitted"]) >= base[
        "submitted"
    ] - 1


def test_required_move_and_extended_horizons() -> None:
    assert 120 in EXTENDED_LABEL_HORIZONS_SECONDS
    assert 600 in EXTENDED_LABEL_HORIZONS_SECONDS
    need = required_move_bps(
        entry_fee_bps=4.5,
        exit_fee_bps=4.5,
        spread_bps=0.05,
        slippage_bps=0.0,
        latency_bps=2.0,
        funding_bps=0.0,
    )
    assert abs(need - 11.05) < 1e-9


def test_write_labels_extended_no_leakage_into_past(tmp_path: Path) -> None:
    base = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    rows = [
        {
            "decision_time": base + timedelta(seconds=i),
            "mid": 100.0 + 0.01 * i,
            "latest_raw_event_id": i,
        }
        for i in range(700)
    ]
    ms = tmp_path / "ms.parquet"
    pq.write_table(pa.Table.from_pylist(rows), ms)
    out = tmp_path / "labels.parquet"
    stats = write_labels_extended(ms, out, horizons=(5, 120, 600))
    assert stats["rows"] == 700
    labels = pq.read_table(out).to_pylist()
    # First row can see 600s ahead; last rows must be null (no future invent).
    assert labels[0]["fwd_ret_600s_bps"] is not None
    assert labels[-1]["fwd_ret_600s_bps"] is None
    # Label uses only future mid: 5s return from t0 is (100.05/100-1)*1e4
    assert abs(float(labels[0]["fwd_ret_5s_bps"]) - 5.0) < 1e-6


def test_clean_oos_reservation_blocks_contaminated() -> None:
    registry = {
        "segments": [
            {
                "segment_id": "g_old",
                "kind": "partition_generation",
                "role": "oos_contaminated",
                "id_start": 1,
                "id_end_inclusive": 10,
                "source_evidence": {},
            }
        ]
    }
    try:
        reserve_clean_oos_future(registry, segment_id="g_old")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    registry2, added = reserve_clean_oos_future(
        {"segments": []}, segment_id="g_future_clean"
    )
    assert added is True
    assert registry2["segments"][0]["role"] == "oos_clean_future"
    assert registry2["segments"][0]["source_evidence"]["inspected_during_selection"] is (
        False
    )


def test_execution_style_matrix_and_event_class() -> None:
    styles = execution_style_matrix(
        median_spread_bps=0.05,
        maker_adverse_selection_bps={"optimistic": -1.0, "base": -2.0, "conservative": None},
        maker_fill_rates={"optimistic": 0.5, "base": 0.2, "conservative": 0.05},
    )
    assert styles["styles"]["TAKER_TAKER"]["required_move_bps"] > 10.0
    assert styles["styles"]["MAKER_TAKER_BASE"]["queue_or_nonfill_penalty_bps"] > 0
    # Non-fills must inflate required move vs naive zero-fee maker.
    assert (
        styles["styles"]["MAKER_TAKER_BASE"]["required_move_bps"]
        > styles["styles"]["TAKER_TAKER"]["entry_fee_bps"]
    )

    rows = _rows(20)
    for i, row in enumerate(rows):
        row["signed_trade_flow_1s"] = 10.0 if i % 2 == 0 else -10.0
        row["fwd_ret_15s_bps"] = 1.0 if i % 2 == 0 else -1.0
    report = evaluate_event_class(
        rows,
        name="all",
        predicate=lambda r: True,
        feature_for_sign="signed_trade_flow_1s",
        horizon_s=15,
        required_bps=0.5,
        seconds_span=20.0,
    )
    assert report["events"] >= 10
    assert report["clears_required_move"] is True
