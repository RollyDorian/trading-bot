"""Tests for data-readiness, incremental discovery, and edge/cost characterization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from trading_bot.research.pipeline.cost_evidence import (
    break_even_matrix,
    depth_slippage_estimate,
    funding_contribution_bps,
    hibachi_public_fee_schedule,
    round_trip_friction_bps,
)
from trading_bot.research.pipeline.edge import (
    break_even_bps,
    characterize_signal,
    conditional_bucket_stats,
    join_features_labels,
    predeclared_conjunctions,
    trade_frequency_frontier,
)
from trading_bot.research.pipeline.incremental import (
    CorpusSegment,
    discover_new_completed_windows,
    plan_incremental_materialization,
    register_segments,
)
from trading_bot.research.pipeline.readiness import (
    ReadinessTargets,
    assign_tertile_regimes,
    continuous_intervals,
    evaluate_data_readiness,
)


def test_discover_skips_known_windows() -> None:
    index = [
        {"dataset_id": "a_v2"},
        {"dataset_id": "b_v2"},
        {"dataset_id": "c_v2"},
    ]
    new = discover_new_completed_windows(index, known_dataset_ids={"a_v2", "c_v2"})
    assert [row["dataset_id"] for row in new] == ["b_v2"]


def test_register_segments_deduplicates() -> None:
    registry: dict = {"segments": []}
    first = CorpusSegment("g1", "partition_generation", "oos_clean_future", 1, 10, {})
    registry, added = register_segments(registry, [first])
    assert added == ["g1"]
    registry, added2 = register_segments(registry, [first])
    assert added2 == []
    assert len(registry["segments"]) == 1


def test_plan_incremental_no_recompute_when_known() -> None:
    registry = {"segments": [{"segment_id": "win1"}]}
    plan = plan_incremental_materialization(
        registry=registry,
        b2_completed_index=[{"dataset_id": "win1"}, {"dataset_id": "win2"}],
        already_materialized_dataset_ids=set(),
    )
    assert plan["new_windows"] == ["win2"]
    assert plan["action"] == "MATERIALIZE_INCREMENTAL"


def test_data_readiness_fails_on_short_history() -> None:
    coverage = {
        "rows": 1000,
        "usable_hours": 10.0,
        "valid_book_pct": 100.0,
        "rows_per_utc_day": {"2026-08-06": 500, "2026-08-07": 500},
        "regimes": {
            "counts": {
                "vol_low": 300,
                "vol_medium": 400,
                "vol_high": 300,
                "spread_tight": 300,
                "spread_medium": 400,
                "spread_wide": 300,
                "activity_low": 300,
                "activity_medium": 400,
                "activity_high": 300,
                "trend_up": 500,
                "trend_down": 500,
            }
        },
    }
    result = evaluate_data_readiness(
        exploratory_coverages=[coverage],
        verified_generation_ids=["g1"],
        targets=ReadinessTargets(
            calendar_days=14, usable_hours=100, verified_generations=3
        ),
        oos_holdout_clean=False,
    )
    assert result["DATA_READY_FOR_ML"] is False
    assert result["ACTION"] == "CONTINUE_COLLECTION"
    assert result["checks"]["calendar_days"] is False
    assert result["checks"]["clean_oos_holdout"] is False


def test_regime_assignment_is_causal_tertiles() -> None:
    base = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    rows = []
    for i in range(90):
        rows.append(
            {
                "decision_time": base + timedelta(seconds=i),
                "spread_bps": float(i % 3),
                "rv_60s_bps": float(i % 3),
                "trade_count": float(i % 3),
                "ret_60s_bps": 1.0 if i % 2 == 0 else -1.0,
            }
        )
    regimes = assign_tertile_regimes(rows)
    assert regimes["shares"]
    assert "vol_low" in regimes["counts"]


def test_continuous_intervals_split_on_gap() -> None:
    base = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    times = [base + timedelta(seconds=i) for i in range(5)]
    times.append(base + timedelta(hours=2))
    intervals = continuous_intervals(times, max_gap_seconds=5)
    assert len(intervals) == 2


def test_quantile_bucket_and_break_even() -> None:
    stats = conditional_bucket_stats([1.0, 2.0, 3.0, 4.0])
    assert stats["n"] == 4
    assert stats["mean"] == 2.5
    assert break_even_bps(-8.2) == 8.2
    matrix = break_even_matrix(
        [{"signal": "imbalance", "horizon": 5, "gross_bps": 8.2}],
        current_plausible_friction_bps=15.0,
    )
    assert matrix[0]["economic_status"] == "NOT_TRADEABLE"
    assert matrix[0]["max_tolerable_friction_bps"] == 8.2


def test_depth_slippage_top_of_book() -> None:
    fit = depth_slippage_estimate(notional_usd=100, top_size=1.0, best_price=2000.0)
    assert fit["fits_top_of_book"] is True
    assert fit["extra_slippage_bps"] == 0.0
    miss = depth_slippage_estimate(notional_usd=5000, top_size=0.1, best_price=2000.0)
    assert miss["fits_top_of_book"] is False
    assert miss["extra_slippage_bps"] is None


def test_fee_schedule_verified_public() -> None:
    fees = hibachi_public_fee_schedule()
    assert fees["classification"] == "VERIFIED_CURRENT"
    assert fees["tier1_taker_fee_rate"] == 0.00045
    assert fees["maker_fee_rate"] == 0.0
    assert funding_contribution_bps(15) < 0.1
    friction = round_trip_friction_bps(
        taker_fee_rate=0.00045,
        slippage_bps_per_side=0.0,
        latency_bps_per_side=1.0,
        spread_bps_round_trip=0.5,
        funding_bps=0.05,
    )
    assert friction == 9.0 + 2.0 + 0.5 + 0.05


def test_frontier_and_conjunctions_on_tiny_table(tmp_path: Path) -> None:
    base = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    feature_rows = []
    label_rows = []
    for i in range(200):
        ts = base + timedelta(seconds=i)
        imb = 0.9 if i % 10 == 0 else 0.01
        feature_rows.append(
            {
                "decision_time": ts,
                "imbalance": imb,
                "ofi_5s": imb,
                "microprice_dev_bps": imb,
                "signed_trade_flow_1s": 0.0,
                "ofi_1s": imb,
                "ofi_15s": imb,
                "spread_bps": 0.2,
                "trade_count": 1 if i % 10 == 0 else 0,
                "valid_book": True,
            }
        )
        label_rows.append(
            {
                "decision_time": ts,
                "fwd_ret_5s_bps": 3.0 if imb > 0.5 else 0.1,
                "fwd_ret_15s_bps": 3.0 if imb > 0.5 else 0.1,
                "fwd_ret_30s_bps": 3.0 if imb > 0.5 else 0.1,
                "fwd_ret_60s_bps": 3.0 if imb > 0.5 else 0.1,
            }
        )
    features = tmp_path / "features.parquet"
    labels = tmp_path / "labels.parquet"
    pq.write_table(pa.Table.from_pylist(feature_rows), features)
    pq.write_table(pa.Table.from_pylist(label_rows), labels)
    rows = join_features_labels(features, labels)
    report = characterize_signal(rows, "imbalance", 5, min_bucket_n=5)
    assert report["buckets"]["all_signed"]["n"] > 0
    frontier = trade_frequency_frontier(
        rows,
        "imbalance",
        5,
        [0.5],
        friction_bps_round_trip=15.0,
        seconds_span=200.0,
    )
    assert frontier[0]["trades"] >= 1
    # No OOS path parameter exists for threshold fitting helpers.
    assert "oos" not in trade_frequency_frontier.__code__.co_varnames
    conj = predeclared_conjunctions(rows, 5)
    assert len(conj) == 3
