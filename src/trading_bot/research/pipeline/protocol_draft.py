"""Draft research protocol (not frozen until data-readiness gate passes)."""

from __future__ import annotations

from typing import Any


def draft_research_protocol(
    *,
    recommended_horizons: list[int],
    feature_subset: list[str],
    friction_bps: float,
    data_ready: bool,
) -> dict[str, Any]:
    return {
        "status": "DRAFT",
        "frozen": False,
        "freeze_blocked_reason": (
            None
            if data_ready
            else "DATA_READY_FOR_ML is false; do not freeze or start supervised ML"
        ),
        "target_horizons_seconds": recommended_horizons,
        "feature_subset": feature_subset,
        "execution_assumptions": {
            "style": "taker_cross_spread",
            "fills": "best_ask_buy_best_bid_sell",
            "delay_seconds_base": 1,
            "delay_sensitivity_seconds": [0, 1, 2],
            "delay_note": "0s is theoretical upper bound only on 1s grid",
        },
        "cost_scenarios": {
            "base_plausible_friction_bps": friction_bps,
            "fee": "Hibachi public Tier-1 taker 4.5 bps/side unless higher tier proven",
            "spread": "observed median/path spread from market_state",
            "slippage": "0 extra when notional fits top-of-book; else modeled stress",
            "funding": "placeholder; report contribution separately for short holds",
        },
        "trade_decision_semantics": {
            "signal": "signed feature threshold on exploratory-selected candidates",
            "position": "flat-to-one; fixed holding horizon",
            "no_mid_fills": True,
        },
        "splits": {
            "exploratory_train": "prior_continuous and later exploratory segments",
            "validation": "chronological tail of exploratory only",
            "oos": (
                "next newly verified generation after g_7471913_7871913; "
                "current generation is contaminated by prior inspection"
            ),
            "purge_embargo_seconds": 60,
        },
        "evaluation_metrics": [
            "gross_bps_per_trade",
            "net_bps_per_trade",
            "break_even_friction_bps",
            "trades_per_day",
            "max_drawdown",
            "regime_stability",
        ],
    }
