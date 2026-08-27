"""Tests for strategy-space rethink screens and scorecard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trading_bot.research.pipeline.external_feed_design import (
    external_relative_value_design,
)
from trading_bot.research.pipeline.opportunity_base_rate import (
    absolute_executable_move_bps,
    opportunity_base_rate_report,
)
from trading_bot.research.pipeline.strategy_scorecard import (
    build_strategy_scorecard,
    recommend_milestone_decision,
)
from trading_bot.research.pipeline.strategy_screening import (
    screen_basis_dislocation,
    screen_funding_carry,
    screen_liquidity_events,
    screen_volatility_target,
    style_break_even_bps,
)


def _rows(n: int = 400) -> list[dict]:
    base = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    rows = []
    for i in range(n):
        mid = 100.0 + 0.001 * i + (0.05 if i % 50 == 0 else 0.0)
        rows.append(
            {
                "decision_time": base + timedelta(seconds=i),
                "mid": mid,
                "best_bid": mid - 0.01,
                "best_ask": mid + 0.01,
                "spread_bps": 2.0 if i % 80 == 0 else 0.05,
                "bid_size": 0.1 if i % 90 == 0 else 5.0,
                "ask_size": 0.1 if i % 90 == 0 else 5.0,
                "ofi_5s": 10.0 if i % 70 == 0 else 0.1,
                "microprice_dev_bps": 5.0 if i % 60 == 0 else 0.1,
                "trade_count": 20 if i % 75 == 0 else 1,
                "basis_mark_bps": 8.0 if i % 100 == 0 else 0.2,
                "basis_spot_bps": 3.0 if i % 100 == 0 else 0.1,
                "funding_rate": 0.0001,
                "rv_60s_bps": 0.1 if i < n // 3 else (5.0 if i > 2 * n // 3 else 1.0),
                "ret_5s_bps": 1.0 if i % 2 == 0 else -1.0,
            }
        )
    return rows


def test_absolute_move_and_nonoverlapping_base_rate() -> None:
    assert abs(absolute_executable_move_bps(100.0, 100.1) - 10.0) < 1e-9
    report = opportunity_base_rate_report(_rows(200), horizons=(15, 60))
    assert "15s" in report["non_overlapping_stride"]
    non = report["non_overlapping_stride"]["15s"]
    ov = report["overlapping_1s"]["15s"]
    # Non-overlapping must have fewer dependent samples than overlapping.
    assert non["n"] < ov["n"]
    assert "10" in non["frac_ge_threshold"]


def test_style_break_even_differs_by_execution() -> None:
    taker = style_break_even_bps(
        holding_seconds=60.0, entry_taker=True, exit_taker=True
    )
    maker_exit = style_break_even_bps(
        holding_seconds=60.0, entry_taker=True, exit_taker=False
    )
    assert taker["required_move_bps"] > maker_exit["required_move_bps"]


def test_screens_run_and_are_causal() -> None:
    rows = _rows()
    basis = screen_basis_dislocation(rows)
    assert basis["family"] == "BASIS_DISLOCATION"
    funding = screen_funding_carry(rows)
    assert funding["funding_rate"]["n"] > 0
    liq = screen_liquidity_events(rows)
    assert "spread_widen_p99" in liq["events"]
    # Cooldown enforces sparse events vs every matching second.
    assert liq["events"]["spread_widen_p99"]["cooldown_s"] == 60
    vol = screen_volatility_target(rows)
    assert "stage1_opportunity_prevalence_nonoverlap" in vol


def test_external_design_does_not_deploy() -> None:
    design = external_relative_value_design(
        hibachi_only_directional_rejected=True,
        short_horizon_gross_bps=2.3,
        taker_friction_bps=11.05,
        nonoverlap_frac_ge_10bps_60s=0.01,
    )
    assert design["deploy_in_this_milestone"] is False
    assert design["isolation_requirements"]["external_feed_failure_kills_hibachi"] is (
        False
    )
    assert design["recommended_decision"] in {"PRIORITIZE", "WATCH"}


def test_scorecard_rejects_short_horizon_and_ranks() -> None:
    rows = _rows()
    opportunity = opportunity_base_rate_report(rows, horizons=(60, 300))
    basis = screen_basis_dislocation(rows)
    funding = screen_funding_carry(rows)
    liquidity = screen_liquidity_events(rows)
    volatility = screen_volatility_target(rows)
    external = external_relative_value_design(
        nonoverlap_frac_ge_10bps_60s=0.01,
    )
    cards = build_strategy_scorecard(
        opportunity=opportunity,
        basis=basis,
        funding=funding,
        liquidity=liquidity,
        volatility=volatility,
        external=external,
    )
    short = next(
        c
        for c in cards
        if c["STRATEGY_CLASS"] == "SHORT_HORIZON_DIRECTIONAL_MICROSTRUCTURE"
    )
    assert short["DECISION"] == "REJECT_FOR_NOW"
    assert cards[0]["rank"] == 1
    rec = recommend_milestone_decision(cards)
    assert rec["DECISION"] in {
        "PRIORITIZE_EXISTING_DATA_STRATEGY",
        "DESIGN_EXTERNAL_FEED_PILOT",
        "COLLECT_MORE_BEFORE_DECIDING",
        "NO_PROMISING_STRATEGY_CLASS",
    }
    assert "NEXT" in rec
