"""Design-only assessment for an isolated public external ETH market-data feed.

No feed is implemented or deployed in this milestone.
"""

from __future__ import annotations

from typing import Any

from trading_bot.research.pipeline.strategy_screening import style_break_even_bps


def external_relative_value_design(
    *,
    hibachi_only_directional_rejected: bool = True,
    short_horizon_gross_bps: float = 2.3,
    taker_friction_bps: float = 11.05,
    nonoverlap_frac_ge_10bps_60s: float | None = None,
) -> dict[str, Any]:
    """Evaluate whether a public external reference feed is justified to design."""

    _ = nonoverlap_frac_ge_10bps_60s  # retained for report context / future gates
    ceiling_note = (
        "Hibachi-only short-horizon directional gross (~2.3 bps) is far below "
        f"~{taker_friction_bps:.2f} bps friction; maker/longer-horizon paths failed. "
        "A lead-lag / relative-value mechanism is a materially different economic "
        "hypothesis that existing Hibachi RAW cannot falsify."
    )
    # Indicative single-venue Hibachi execution still applies for the Hibachi leg.
    one_leg = style_break_even_bps(holding_seconds=5.0, entry_taker=True, exit_taker=True)
    # Two-leg hedge would pay fees on both venues (unknown external fees → placeholder).
    two_leg = {
        "hibachi_leg_bps": one_leg["required_move_bps"],
        "external_leg_fee_bps_placeholder": 2.0,  # illustrative; not verified
        "extra_latency_sync_bps_placeholder": 2.0,
        "required_move_bps_indicative": one_leg["required_move_bps"] + 4.0,
        "note": "Not arbitrage; placeholders only until external fee schedule verified.",
    }

    # Storage estimate: public trades + top-of-book at 1–10 Hz is far lighter than
    # Hibachi depth-20 continuous book, but still non-zero on the research host.
    storage = {
        "assumed_topics": ["public_trades", "best_bid_ask_or_top_book"],
        "assumed_rate_events_per_second": {"trades": 5, "quotes": 10},
        "bytes_per_event_estimate": 400,
        "gib_per_day_estimate": round(
            (5 + 10) * 400 * 86400 / (1024**3),
            3,
        ),
        "note": (
            "Estimate only. Must stay off the production Hibachi collector host "
            "hot path; prefer research/materialization host or isolated container."
        ),
    }

    isolation = {
        "separate_process_or_compose_service": True,
        "separate_failure_domain": True,
        "must_not_share_write_path_with_hibachi_collector": True,
        "external_feed_failure_kills_hibachi": False,
        "raw_provenance_required": [
            "source",
            "symbol",
            "received_at",
            "source_timestamp",
            "sequence_when_available",
            "schema_version",
            "connection_id",
        ],
        "private_or_account_apis": False,
        "candidate_venues_public_only": [
            {
                "venue": "binance_or_equivalent_liquid_eth_perp_or_spot",
                "status": "CANDIDATE_NOT_SELECTED",
                "note": "Selection requires separate approval; not deployed here.",
            }
        ],
    }

    recommended = "WATCH"
    if hibachi_only_directional_rejected and short_horizon_gross_bps < (
        0.5 * taker_friction_bps
    ):
        # Hibachi-alone directional ceiling is structurally weak → design feed.
        recommended = "PRIORITIZE"

    return {
        "summary": ceiling_note,
        "recommended_decision": recommended,
        "indicative_break_even_bps": one_leg["required_move_bps"],
        "two_leg_break_even_bps": two_leg["required_move_bps_indicative"],
        "two_leg_detail": two_leg,
        "expected_gross_opportunity_note": (
            "Unknown until synchronized external+Hibachi RAW exists. "
            "Mechanism value is lead-lag residual, not Hibachi-alone IC."
        ),
        "data_requirements": {
            "public_market_data_only": True,
            "minimum_topics": ["trades", "top_of_book"],
            "timestamp_precision": "milliseconds_or_better_preferred",
            "synchronization": (
                "Align on received_at for causal research; treat source clocks as "
                "approximate; never invent exchange_sequence."
            ),
            "latency_requirement": (
                "Research-grade first (seconds-ok for design validation); "
                "sub-second only if later justified."
            ),
        },
        "storage_estimate": storage,
        "isolation_requirements": isolation,
        "falsification_before_ml": [
            "Measure Hibachi mid response after external mid shocks (lead-lag).",
            "Require non-overlapping events with gross executable Hibachi move "
            f">~{one_leg['required_move_bps']:.1f} bps after costs.",
            "If Hibachi leads or co-moves with no lag residual, REJECT family.",
        ],
        "deploy_in_this_milestone": False,
    }
