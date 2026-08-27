"""Execution-style friction scenarios for taker/maker combinations."""

from __future__ import annotations

from typing import Any

from trading_bot.research.pipeline.cost_evidence import (
    funding_contribution_bps,
    hibachi_public_fee_schedule,
)
from trading_bot.research.pipeline.event_selection import required_move_bps


def execution_style_matrix(
    *,
    median_spread_bps: float,
    maker_adverse_selection_bps: dict[str, float | None],
    maker_fill_rates: dict[str, float | None],
    holding_seconds: float = 15.0,
    latency_bps_taker_side: float = 1.0,
) -> dict[str, Any]:
    """Build TAKER_TAKER / MAKER_TAKER / MAKER_MAKER_* required-move scenarios."""

    fees = hibachi_public_fee_schedule()
    taker = float(fees["tier1_taker_fee_rate"]) * 10_000.0
    maker = float(fees["maker_fee_rate"]) * 10_000.0
    funding = funding_contribution_bps(holding_seconds)

    def nonfill_penalty(fill_rate: float | None) -> float:
        # Unfilled signals are opportunity cost, not free trades. Conservative
        # accounting: scale required edge by inverse fill rate when fill_rate>0.
        if fill_rate is None or fill_rate <= 0:
            return 1e9
        # Represent as extra bps burden equivalent to diluting edge by fill_rate.
        # required_effective = raw_required / fill_rate => penalty = required*(1/fr-1)
        # Applied later using base components without circularity: use fixed proxy.
        return max(0.0, (1.0 / fill_rate - 1.0) * (taker + median_spread_bps))

    styles: dict[str, Any] = {}

    styles["TAKER_TAKER"] = {
        "entry_fee_bps": taker,
        "exit_fee_bps": taker,
        "spread_bps": median_spread_bps,
        "slippage_bps": 0.0,
        "latency_bps": 2.0 * latency_bps_taker_side,
        "funding_bps": funding,
        "adverse_selection_bps": 0.0,
        "queue_or_nonfill_penalty_bps": 0.0,
    }
    styles["TAKER_TAKER"]["required_move_bps"] = required_move_bps(**styles["TAKER_TAKER"])

    for scenario in ("optimistic", "base", "conservative"):
        adv = maker_adverse_selection_bps.get(scenario)
        # If post-fill signed mid is negative, adverse selection costs |adv|.
        adverse_cost = abs(min(0.0, float(adv))) if adv is not None else 0.0
        fill_rate = maker_fill_rates.get(scenario)
        entry_exit_slip = 0.5 * median_spread_bps
        queue_penalty = nonfill_penalty(fill_rate)
        mt_required = required_move_bps(
            entry_fee_bps=maker,
            exit_fee_bps=taker,
            spread_bps=entry_exit_slip,
            slippage_bps=0.0,
            latency_bps=latency_bps_taker_side,
            funding_bps=funding,
            adverse_selection_bps=adverse_cost,
            queue_or_nonfill_penalty_bps=queue_penalty,
        )
        styles[f"MAKER_TAKER_{scenario.upper()}"] = {
            "entry_fee_bps": maker,
            "exit_fee_bps": taker,
            "spread_bps": 0.0,  # maker entry captures spread; taker exit pays half-ish
            # Exit still crosses: charge half spread on exit approximately.
            "exit_spread_bps_note": "taker exit charged as 0.5 * median spread below",
            "slippage_bps": entry_exit_slip,
            "latency_bps": latency_bps_taker_side,
            "funding_bps": funding,
            "adverse_selection_bps": adverse_cost,
            "queue_or_nonfill_penalty_bps": queue_penalty,
            "fill_rate": fill_rate,
            "required_move_bps": mt_required,
        }

        mm_queue = 2.0 * queue_penalty
        mm_required = required_move_bps(
            entry_fee_bps=maker,
            exit_fee_bps=maker,
            spread_bps=0.0,
            slippage_bps=0.0,
            latency_bps=0.0,
            funding_bps=funding,
            adverse_selection_bps=adverse_cost,
            queue_or_nonfill_penalty_bps=mm_queue,
        )
        styles[f"MAKER_MAKER_{scenario.upper()}"] = {
            "entry_fee_bps": maker,
            "exit_fee_bps": maker,
            "spread_bps": 0.0,
            "slippage_bps": 0.0,
            "latency_bps": 0.0,
            "funding_bps": funding,
            "adverse_selection_bps": adverse_cost,
            # Both legs may fail to fill; compound non-fill burden.
            "queue_or_nonfill_penalty_bps": mm_queue,
            "fill_rate_entry_proxy": fill_rate,
            "required_move_bps": mm_required,
        }

    return {
        "median_spread_bps": median_spread_bps,
        "fee_schedule": fees,
        "styles": styles,
        "notes": [
            "Maker is not modeled as zero-fee taker.",
            "Non-fills are penalized; they are never profitable zero-cost trades.",
            "Adverse selection uses post-fill signed mid when available.",
        ],
    }
