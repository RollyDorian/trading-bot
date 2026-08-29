"""ETH_TP_SL_FIRST_TOUCH_FEASIBILITY_V1: classification, costs, economics.

Synthetic 1s paths only. No ML, no feature selection, no OOS inspection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from trading_bot.research.pipeline.cost_evidence import hibachi_public_fee_schedule
from trading_bot.research.pipeline.first_passage_corpus import V1_UNTOUCHED_OOS_UTC_DATES
from trading_bot.research.pipeline.tp_sl_first_touch import (
    CONTROL_HORIZONS_SECONDS,
    FORENSIC_EXCURSION_THRESHOLDS_BPS,
    FORENSIC_SUBMINUTE_HORIZONS_SECONDS,
    PRIMARY_HORIZONS_SECONDS,
    SL_THRESHOLDS_BPS,
    TP_SL_PROTOCOL_NAME,
    TP_THRESHOLDS_BPS,
    analyze_tp_sl_first_touch,
    assess_primary_contamination,
    audit_cost_decomposition,
    barrier_interval_hits,
    classify_executable_path,
    discovery_dates_from_full_corpus_doc,
    economics_for_cell,
    entry_barrier_outcome,
    load_tp_sl_series_from_parquet,
    render_tp_sl_markdown,
    resolve_step,
    scan_forensic_excursions,
)


def _series(
    mids: list[float],
    *,
    spread: float = 0.0002,
    start: datetime | None = None,
) -> tuple[list[int], list[float], list[float], list[float]]:
    base = start or datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    epoch: list[int] = []
    bid: list[float] = []
    ask: list[float] = []
    mid_out: list[float] = []
    half = spread / 2.0
    for i, mid in enumerate(mids):
        ts = base + timedelta(seconds=i)
        epoch.append(int(ts.timestamp()))
        bid.append(mid - half)
        ask.append(mid + half)
        mid_out.append(mid)
    return epoch, bid, ask, mid_out


def test_frozen_grids_match_lead_spec() -> None:
    assert PRIMARY_HORIZONS_SECONDS == (120, 180, 300)
    assert CONTROL_HORIZONS_SECONDS == (60, 600)
    assert TP_THRESHOLDS_BPS == (20.0, 25.0, 30.0)
    assert SL_THRESHOLDS_BPS == (5.0, 10.0, 15.0, 20.0)
    assert FORENSIC_SUBMINUTE_HORIZONS_SECONDS == (5, 10, 15, 30, 60)
    assert FORENSIC_EXCURSION_THRESHOLDS_BPS == (50.0, 75.0, 100.0)


def test_discovery_loader_refuses_untouched_oos_dates() -> None:
    dates = discovery_dates_from_full_corpus_doc(
        Path("docs/eth_first_passage_full_corpus_v1.json")
    )
    assert "2026-08-06" in dates
    for banned in V1_UNTOUCHED_OOS_UTC_DATES:
        assert banned not in dates


def test_interval_both_barriers_is_ambiguous_not_ordered() -> None:
    # From -4 to +25 the 1s span (29 bps) is >= TP+SL=25, so the unidentified
    # path could have touched SL=-5 before TP=+20. Do not invent the order.
    assert resolve_step(-4.0, 25.0, tp_bps=20.0, sl_bps=5.0) == "AMBIGUOUS"


def test_large_one_second_span_is_ambiguous_even_if_only_tp_prints() -> None:
    # Interval [0, 35] contains +20 but not -10; span 35 >= TP+SL=30 so the
    # unidentified intra-second path could have hit SL first.
    assert resolve_step(0.0, 35.0, tp_bps=20.0, sl_bps=10.0) == "AMBIGUOUS"


def test_interval_only_tp_is_not_ambiguous() -> None:
    hit_tp, hit_sl = barrier_interval_hits(-0.05, 25.0, tp_bps=20.0, sl_bps=10.0)
    assert hit_tp is True
    assert hit_sl is False


def test_classify_long_tp_first_uses_ask_entry_and_future_bid() -> None:
    # Tight TOB so entry is not already through SL. Future bid vs entry ask.
    ask0 = 100.01
    bid0 = 99.99
    # +20 bps executable long: bid_j >= ask0 * 1.002
    bids = [bid0, bid0, ask0 * 1.0025]
    asks = [ask0, ask0, ask0 * 1.0025 + 0.02]
    exec_entry = (bids[0] / asks[0] - 1.0) * 10_000.0
    path = [(bids[k] / asks[0] - 1.0) * 10_000.0 for k in range(1, 3)]
    outcome, lag, realized = classify_executable_path(
        exec_entry, path, tp_bps=20.0, sl_bps=10.0
    )
    assert exec_entry > -5.0  # entry must not already breach SL=10
    assert outcome == "TP_FIRST"
    assert lag == 2
    assert realized is not None and realized >= 20.0


def test_classify_short_tp_first_uses_bid_entry_and_future_ask() -> None:
    bid0 = 100.0
    ask0 = 100.02
    # Short executable: (bid0 / ask_j - 1)*10000 >= 20 → ask_j <= bid0 / 1.002
    ask_hit = bid0 / 1.0025
    asks = [ask0, ask0, ask_hit]
    exec_entry = (bid0 / asks[0] - 1.0) * 10_000.0
    path = [(bid0 / asks[k] - 1.0) * 10_000.0 for k in range(1, 3)]
    outcome, lag, realized = classify_executable_path(
        exec_entry, path, tp_bps=20.0, sl_bps=10.0
    )
    assert outcome == "TP_FIRST"
    assert lag == 2
    assert realized is not None and realized >= 20.0


def test_classify_sl_first_before_tp() -> None:
    exec_entry = -0.05
    # Dip to -12 bps (SL=10) then recover above TP. First resolving step is SL.
    path = [-12.0, 25.0]
    outcome, lag, realized = classify_executable_path(
        exec_entry, path, tp_bps=20.0, sl_bps=10.0
    )
    assert outcome == "SL_FIRST"
    assert lag == 1
    assert realized is not None and realized <= -10.0


def test_classify_timeout_records_executable_return_at_h() -> None:
    exec_entry = -0.05
    path = [0.5, 1.0, -0.2]
    outcome, lag, realized = classify_executable_path(
        exec_entry, path, tp_bps=20.0, sl_bps=10.0
    )
    assert outcome == "TIMEOUT"
    assert lag is None
    assert realized == -0.2


def test_classify_ambiguous_when_one_second_span_covers_both_barriers() -> None:
    exec_entry = -0.05
    path = [-4.0, 25.0]  # lag-2 interval [-4, 25] contains -5 and +20
    outcome, lag, realized = classify_executable_path(
        exec_entry, path, tp_bps=20.0, sl_bps=5.0
    )
    assert outcome == "AMBIGUOUS"
    assert lag == 2
    assert realized is None


def test_classify_invalid_print_is_ambiguous() -> None:
    outcome, lag, realized = classify_executable_path(
        -0.05, [1.0, float("nan")], tp_bps=20.0, sl_bps=10.0
    )
    assert outcome == "AMBIGUOUS"
    assert lag == 2
    assert realized is None


def test_cost_audit_does_not_double_count_executable_spread() -> None:
    audit = audit_cost_decomposition(
        median_spread_bps=0.053,
        holding_seconds=300.0,
        latency_bps_per_side=1.0,
    )
    assert audit["spread_already_in_executable_gross"] is True
    assert audit["subtract_spread_again_from_net"] is False
    fees = hibachi_public_fee_schedule()
    expected_fee = 2.0 * float(fees["tier1_taker_fee_rate"]) * 10_000.0
    assert abs(audit["fee_round_trip_bps"] - expected_fee) < 1e-9
    assert abs(audit["latency_round_trip_bps"] - 2.0) < 1e-9
    extra = audit["extra_cost_bps_excluding_spread"]
    # Legacy ~11 bps RT includes spread; extra cost here must be fee+latency(+tiny funding).
    assert extra >= expected_fee
    assert extra < float(audit["legacy_round_trip_friction_bps"])
    assert abs(extra - (expected_fee + 2.0 + float(audit["funding_bps"]))) < 1e-9
    assert audit["legacy_round_trip_includes_spread"] is True
    assert "double" in audit["note"].lower()


def test_break_even_tp_first_uses_barrier_payoff_without_spread_again() -> None:
    audit = audit_cost_decomposition(
        median_spread_bps=0.053, holding_seconds=120.0, latency_bps_per_side=1.0
    )
    extra = float(audit["extra_cost_bps_excluding_spread"])
    cell = economics_for_cell(
        tp_bps=20.0,
        sl_bps=10.0,
        n_valid=100,
        n_tp_first=30,
        n_sl_first=50,
        n_timeout=20,
        n_ambiguous=0,
        mean_gross_tp=20.4,
        mean_gross_sl=-10.2,
        mean_gross_timeout=-1.0,
        extra_cost_bps=extra,
    )
    # Two-outcome barrier: p* = (SL + extra) / (TP + SL)
    expected = (10.0 + extra) / (20.0 + 10.0)
    assert cell["break_even_tp_first_prob_two_outcome_barrier"] is not None
    assert abs(cell["break_even_tp_first_prob_two_outcome_barrier"] - expected) < 1e-9
    assert cell["payoff_ratio_barrier_tp_over_sl"] == 2.0
    assert cell["unconditional_tp_first_rate"] == 0.3
    assert cell["required_precision_tp_first"] == expected
    assert cell["required_lift_abs"] is not None
    assert abs(cell["required_lift_abs"] - (expected - 0.3)) < 1e-9
    # Net EV subtracts extra cost, not spread-on-top-of-executable-gross.
    assert cell["unconditional_net_ev_bps"] is not None


def test_analyze_counts_four_outcomes_and_nonoverlap_offsets() -> None:
    # Quiet path: all TIMEOUT. Horizon 8 so offsets 0,2,4,6 exist.
    epoch, bid, ask, mid = _series([100.0] * 40)
    report = analyze_tp_sl_first_touch(
        epoch,
        bid,
        ask,
        mid,
        horizons=(8,),
        tp_grid=(20.0,),
        sl_grid=(10.0,),
        delays_seconds=(0,),
    )
    assert report["protocol"] == TP_SL_PROTOCOL_NAME
    cell = report["delay_0s"]["non_overlapping"]["8s"]["20"]["10"]["long"]
    off0 = cell["per_offset"]["0"]
    assert off0["n_valid_starts"] == 4  # starts 0,8,16,24 with 40 rows, H=8
    assert off0["n_timeout"] == off0["n_valid_starts"]
    assert off0["n_tp_first"] == 0
    assert off0["n_sl_first"] == 0
    assert off0["n_ambiguous"] == 0
    assert off0["mean_timeout_gross_bps"] is not None
    pooled = cell["pooled_descriptive_dependent"]
    assert "dependent" in pooled["note"].lower()


def test_analyze_long_tp_then_timeout_on_control_path() -> None:
    # +5 bps/s so TP 20 is crossed without a 1s span >= TP+SL.
    mids = [100.0 + 0.05 * i for i in range(12)]
    epoch, bid, ask, mid = _series(mids, spread=0.0002)
    report = analyze_tp_sl_first_touch(
        epoch,
        bid,
        ask,
        mid,
        horizons=(10,),
        tp_grid=(20.0,),
        sl_grid=(10.0,),
        delays_seconds=(0,),
    )
    rolling = report["delay_0s"]["rolling_1s"]["10s"]["20"]["10"]["long"]
    assert rolling["n_tp_first"] >= 1
    assert rolling["n_valid_starts"] > 0
    assert rolling["tp_first_time_s"]["p50"] is not None


def test_latency_sensitivity_changes_net_ev_not_gross_counts() -> None:
    epoch, bid, ask, mid = _series([100.0] * 30)
    report = analyze_tp_sl_first_touch(
        epoch,
        bid,
        ask,
        mid,
        horizons=(8,),
        tp_grid=(20.0,),
        sl_grid=(10.0,),
        delays_seconds=(0,),
        latency_bps_per_side_grid=(0.0, 1.0, 2.0),
    )
    cell = report["delay_0s"]["non_overlapping"]["8s"]["20"]["10"]["long"]
    ev0 = cell["economics_by_latency_bps_per_side"]["0"]["unconditional_net_ev_bps"]
    ev1 = cell["economics_by_latency_bps_per_side"]["1"]["unconditional_net_ev_bps"]
    ev2 = cell["economics_by_latency_bps_per_side"]["2"]["unconditional_net_ev_bps"]
    assert ev0 is not None and ev1 is not None and ev2 is not None
    # Higher modeled latency makes net EV worse; TP-first count is unchanged.
    assert ev0 > ev1 > ev2
    assert cell["n_tp_first"] == cell["economics_by_latency_bps_per_side"]["2"][
        "n_tp_first"
    ]


def test_path_delay_shifts_entry_prices() -> None:
    # +22 bps at t=1: span < TP+SL so this is TP_FIRST, not AMBIGUOUS.
    # Delay-1 enters after the jump, so further 20 bps may not print.
    mids = [100.0, 100.22] + [100.22] * 20
    epoch, bid, ask, mid = _series(mids, spread=0.0002)
    report = analyze_tp_sl_first_touch(
        epoch,
        bid,
        ask,
        mid,
        horizons=(8,),
        tp_grid=(20.0,),
        sl_grid=(10.0,),
        delays_seconds=(0, 1),
    )
    d0 = report["delay_0s"]["rolling_1s"]["8s"]["20"]["10"]["long"]
    d1 = report["delay_1s"]["rolling_1s"]["8s"]["20"]["10"]["long"]
    assert d0["n_tp_first"] >= 1
    # After delay-1 entry at the already-high ask, further 20 bps may not print.
    assert d1["n_tp_first"] <= d0["n_tp_first"]


def test_forensic_flags_subminute_large_executable_jump() -> None:
    mids = [100.0] * 20
    mids[3] = 100.80  # +80 bps in one second
    epoch, bid, ask, mid = _series(mids, spread=0.0002)
    appendix = scan_forensic_excursions(
        epoch,
        bid,
        ask,
        mid,
        horizons=FORENSIC_SUBMINUTE_HORIZONS_SECONDS,
        thresholds=FORENSIC_EXCURSION_THRESHOLDS_BPS,
        top_n=5,
    )
    assert appendix["n_subminute_windows_ge_50bps"] >= 1
    top = appendix["largest_excursions"][0]
    assert top["abs_exec_mfe_bps"] >= 50.0
    assert top["horizon_seconds"] <= 60


def test_forensic_mae_tail_is_separate_from_tp_sl_grid() -> None:
    # Adverse -80 bps then +25 bps inside 15s: MAE tail if TP=20 ever hits.
    mids = [100.0, 99.20] + [100.25] * 20
    epoch, bid, ask, mid = _series(mids, spread=0.0002)
    appendix = scan_forensic_excursions(
        epoch, bid, ask, mid, mae_tp_bps=20.0, mae_tail_bps=50.0, top_n=5
    )
    assert appendix["n_mae_tails_before_tp20"] >= 1
    assert appendix["mae_tails"][0]["mae_before_tp_bps"] >= 50.0


def test_markdown_is_feasibility_surface_not_a_strategy() -> None:
    epoch, bid, ask, mid = _series([100.0] * 20)
    report = analyze_tp_sl_first_touch(
        epoch,
        bid,
        ask,
        mid,
        horizons=(8,),
        tp_grid=(20.0,),
        sl_grid=(10.0,),
        delays_seconds=(0,),
    )
    text = render_tp_sl_markdown(
        {
            **report,
            "STATUS": "ETH_TP_SL_FIRST_TOUCH_FEASIBILITY_READY",
            "DECISION": "STOP_FOR_LEAD_REVIEW",
            "ML_STATUS": "NOT_STARTED",
            "corpus": {"discovery_utc_dates": ["2026-07-30"]},
            "cost_audit": audit_cost_decomposition(
                median_spread_bps=0.05, holding_seconds=120.0
            ),
            "forensic_qa": {"contamination_status": "PASS", "cases": []},
        }
    )
    assert "STOP_FOR_LEAD_REVIEW" in text
    assert "feasibility" in text.lower()
    assert "not a strategy" in text.lower() or "do not optimize" in text.lower()


def test_loader_keeps_forensic_columns(tmp_path: Path) -> None:
    base = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    rows = []
    for i in range(5):
        mid = 100.0
        rows.append(
            {
                "decision_time": base + timedelta(seconds=i),
                "best_bid": mid - 0.01,
                "best_ask": mid + 0.01,
                "mid": mid,
                "valid_book": True,
                "book_age_seconds": 0.2,
                "quote_age_seconds": 0.1,
                "mark_price": mid + 0.02,
                "spread_bps": 2.0,
            }
        )
    path = tmp_path / "market_state_1s.parquet"
    pq.write_table(pa.Table.from_pylist(rows), path)
    loaded = load_tp_sl_series_from_parquet([path])
    assert len(loaded["epoch_s"]) == 5
    assert loaded["valid_book"][0] is True
    assert loaded["mark_price"][0] is not None


def test_wide_spread_entry_is_sl_first_at_lag_zero() -> None:
    outcome = entry_barrier_outcome(-20.0, tp_bps=20.0, sl_bps=10.0)
    assert outcome == "SL_FIRST"
    classified, lag, realized = classify_executable_path(
        -20.0, [ -19.0, -18.0], tp_bps=20.0, sl_bps=10.0
    )
    assert classified == "SL_FIRST"
    assert lag == 0
    assert realized == -20.0


def test_missing_tob_does_not_count_horizon_longer_than_remain() -> None:
    epoch, bid, ask, mid = _series([100.0] * 12)
    ask[4] = bid[4]
    report = analyze_tp_sl_first_touch(
        epoch,
        bid,
        ask,
        mid,
        horizons=(8, 16),
        tp_grid=(20.0,),
        sl_grid=(10.0,),
        delays_seconds=(0,),
    )
    h8 = report["delay_0s"]["rolling_1s"]["8s"]["20"]["10"]["long"]
    h16 = report["delay_0s"]["rolling_1s"]["16s"]["20"]["10"]["long"]
    # Crossed/missing TOB is unobservable data, not an intra-second dual-barrier.
    assert h8["n_data_invalid"] >= 1
    assert h8["n_ambiguous"] == 0
    assert h16["n_valid_starts"] == 0


def test_contamination_pass_when_primary_resolves_are_clean() -> None:
    epoch, bid, ask, mid = _series([100.0] * 40)
    stats = analyze_tp_sl_first_touch(
        epoch,
        bid,
        ask,
        mid,
        horizons=PRIMARY_HORIZONS_SECONDS,
        tp_grid=TP_THRESHOLDS_BPS,
        sl_grid=SL_THRESHOLDS_BPS,
        delays_seconds=(0,),
    )
    verdict = assess_primary_contamination(
        stats=stats, forensic={"largest_excursions": [], "n_quality_flagged_top_cases": 0}
    )
    assert verdict["contamination_status"] == "PASS"
    assert verdict["escalate"] is False
