"""STRATEGY_SPACE_RETHINK runner — exploratory Hibachi artifacts only."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trading_bot.research.pipeline.cost_evidence import build_cost_evidence_report
from trading_bot.research.pipeline.external_feed_design import (
    external_relative_value_design,
)
from trading_bot.research.pipeline.incremental import load_registry
from trading_bot.research.pipeline.opportunity_base_rate import opportunity_base_rate_report
from trading_bot.research.pipeline.readiness import (
    evaluate_data_readiness,
    summarize_market_state_coverage,
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
)

ROOT = Path("data/research/full_corpus")
PRIOR = ROOT / "runs" / "prior_continuous"
REGISTRY = ROOT / "corpus_registry.json"
REPORT_DIR = ROOT / "reports" / "strategy_space"
DOCS = Path("docs")
CLEAN_OOS = "RESERVED_NEXT_VERIFIED_GENERATION_AFTER_g_7871913"


def _write_reports(final: dict[str, Any], markdown: str) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(final, indent=2, sort_keys=True, default=str)
    (REPORT_DIR / "STRATEGY_SPACE_RETHINK_v1.json").write_text(
        payload, encoding="utf-8"
    )
    (REPORT_DIR / "STRATEGY_SPACE_RETHINK_v1.md").write_text(markdown, encoding="utf-8")
    (DOCS / "strategy_space_rethink_v1.json").write_text(payload, encoding="utf-8")
    (DOCS / "strategy_space_rethink_v1.md").write_text(markdown, encoding="utf-8")
    (DOCS / "strategy_space_scorecard_v1.json").write_text(
        json.dumps(final["SCORECARD"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )


def _render_markdown(final: dict[str, Any]) -> str:
    lines: list[str] = [
        "# Strategy space rethink v1",
        "",
        f"STATUS: `{final['STATUS']}`",
        f"DECISION: `{final['DECISION']}`",
        f"ML_STATUS: `{final['ML_STATUS']}`",
        f"RECOMMENDED_HYPOTHESIS: `{final['RECOMMENDED_HYPOTHESIS']}`",
        "",
        "## CURRENT_HYPOTHESIS (rejected)",
        final["CURRENT_HYPOTHESIS"],
        "",
        "## OPPORTUNITY_BASE_RATE (non-overlapping stride)",
        "",
        "| horizon | n | p50 | p95 | p99 | frac>=10bps | frac>=15bps |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    non_ov = final["OPPORTUNITY_BASE_RATE"]["non_overlapping_stride"]
    for key in ("15s", "30s", "60s", "120s", "300s", "600s", "1800s", "3600s"):
        row = non_ov.get(key) or {}
        frac = row.get("frac_ge_threshold") or {}
        if not row.get("n"):
            continue
        lines.append(
            f"| {key} | {row.get('n')} | {row.get('p50_bps')} | {row.get('p95_bps')} | "
            f"{row.get('p99_bps')} | {frac.get('10')} | {frac.get('15')} |"
        )
    lines += [
        "",
        "Overlapping 1s rows are dependent; prefer non-overlapping figures above.",
        "",
        "## BASIS_DISLOCATION",
        "- decision: see scorecard `BASIS_DISLOCATION_MEAN_REVERSION`",
        (
            "- extreme |basis_mark| p99: "
            f"{(final['BASIS_DISLOCATION'].get('basis_mark_abs') or {}).get('p99')}"
        ),
        (
            "- executable fade mean @300s: "
            + str(
                (
                    (final["BASIS_DISLOCATION"].get("executable_fade_proxy") or {}).get(
                        "300s"
                    )
                    or {}
                ).get("mean_signed_executable_mid_bps")
            )
        ),
        "",
        "## FUNDING_CARRY",
        (
            "- abs funding p50: "
            + str((final["FUNDING_CARRY"].get("funding_rate") or {}).get("abs_p50"))
        ),
        (
            "- 8h expected |carry| bps: "
            + str(
                (
                    (final["FUNDING_CARRY"].get("carry_vs_costs") or {}).get("8h") or {}
                ).get("expected_abs_carry_bps")
            )
        ),
        "",
        "## LIQUIDITY_EVENTS",
        "See JSON for per-event forward abs moves (60s cooldown).",
        "",
        "## VOLATILITY / OPPORTUNITY TARGET",
        "```json",
        json.dumps(
            {
                "stage1": final["VOLATILITY_TARGET"].get(
                    "stage1_opportunity_prevalence_nonoverlap"
                ),
                "precursor_lift": final["VOLATILITY_TARGET"].get(
                    "stage1_precursor_lift"
                ),
            },
            indent=2,
            default=str,
        ),
        "```",
        "",
        "## RELATIVE_VALUE_EXTERNAL",
        final["RELATIVE_VALUE_EXTERNAL"]["summary"],
        (
            "- recommended_decision: "
            f"`{final['RELATIVE_VALUE_EXTERNAL']['recommended_decision']}`"
        ),
        (
            "- deploy_in_this_milestone: "
            f"`{final['RELATIVE_VALUE_EXTERNAL']['deploy_in_this_milestone']}`"
        ),
        "",
        "## SCORECARD (ranked)",
        "",
        "| rank | class | decision | data | new_feed | gross | break-even |",
        "|---:|---|---|---|---|---|---|",
    ]
    for card in final["SCORECARD"]:
        lines.append(
            f"| {card['rank']} | {card['STRATEGY_CLASS']} | {card['DECISION']} | "
            f"{card['EXISTING_DATA_SUPPORT']} | {card['NEW_PUBLIC_DATA_REQUIRED']} | "
            f"{card['EXPLORATORY_GROSS_OPPORTUNITY']} | {card['BREAK_EVEN_FRICTION']} |"
        )
    lines += [
        "",
        "## NEW_DATA_REQUIRED",
        json.dumps(final["NEW_DATA_REQUIRED"], indent=2),
        "",
        "## OOS",
        json.dumps(final["OOS"], indent=2),
        "",
        "## PRODUCTION",
        json.dumps(final["PRODUCTION"], indent=2),
        "",
        "## NEXT (not executed)",
        final["NEXT"],
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    ms_path = PRIOR / "market_state_1s" / "market_state_1s.parquet"
    print("=== load market_state ===", flush=True)
    rows = pq.read_table(ms_path).to_pylist()
    cost = build_cost_evidence_report(ms_path)
    taker_friction = float(
        cost["plausible_friction_bps"][
            "tier1_taker_plus_median_spread_plus_modeled_latency"
        ]
    )

    print("=== opportunity base rate ===", flush=True)
    opportunity = opportunity_base_rate_report(rows)
    print("=== basis / funding / liquidity / vol ===", flush=True)
    basis = screen_basis_dislocation(rows)
    funding = screen_funding_carry(rows)
    liquidity = screen_liquidity_events(rows)
    volatility = screen_volatility_target(rows)

    frac_60_10 = (
        (opportunity.get("non_overlapping_stride") or {})
        .get("60s", {})
        .get("frac_ge_threshold", {})
        .get("10")
    )
    external = external_relative_value_design(
        hibachi_only_directional_rejected=True,
        short_horizon_gross_bps=2.3,
        taker_friction_bps=taker_friction,
        nonoverlap_frac_ge_10bps_60s=frac_60_10,
    )

    scorecard = build_strategy_scorecard(
        opportunity=opportunity,
        basis=basis,
        funding=funding,
        liquidity=liquidity,
        volatility=volatility,
        external=external,
    )
    recommendation = recommend_milestone_decision(scorecard)

    registry = load_registry(REGISTRY) if REGISTRY.exists() else {"segments": []}
    coverage = summarize_market_state_coverage(ms_path)
    readiness = evaluate_data_readiness(
        exploratory_coverages=[coverage],
        verified_generation_ids=[
            s["segment_id"]
            for s in registry.get("segments", [])
            if not str(s.get("segment_id", "")).startswith("RESERVED_")
        ],
        oos_holdout_clean=True,
    )

    new_data = {
        "required_for_primary_hypothesis": recommendation["RECOMMENDED_HYPOTHESIS"]
        in {
            "EXTERNAL_RELATIVE_VALUE_LEAD_LAG",
            "CROSS_VENUE_BASIS_OR_CARRY",
            "FUNDING_CARRY",
        },
        "feeds": (
            external["data_requirements"]
            if recommendation["DECISION"] == "DESIGN_EXTERNAL_FEED_PILOT"
            else {"public_hibachi_only": True}
        ),
        "storage_estimate": external.get("storage_estimate"),
        "deploy_now": False,
    }

    final: dict[str, Any] = {
        "STATUS": "STRATEGY_SPACE_RETHINK_READY",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "CURRENT_HYPOTHESIS": (
            "REJECTED: short-horizon directional microstructure prediction → "
            "direct ETH perp trade. Best exploratory gross ~2.3 bps vs ~"
            f"{taker_friction:.2f} bps TAKER_TAKER; maker fills adversely selected; "
            "longer horizons decay. Do not start ML on this target."
        ),
        "OPPORTUNITY_BASE_RATE": opportunity,
        "BASIS_DISLOCATION": basis,
        "FUNDING_CARRY": funding,
        "LIQUIDITY_EVENTS": liquidity,
        "VOLATILITY_TARGET": volatility,
        "RELATIVE_VALUE_EXTERNAL": external,
        "SCORECARD": scorecard,
        "NEW_DATA_REQUIRED": new_data,
        "DATA_READINESS": readiness,
        "OOS": {
            "contaminated": "g_7471913_7871913",
            "clean_future_reserved": CLEAN_OOS,
            "used_during_screening": False,
            "note": (
                "When a new family protocol is frozen, designate a fresh future "
                "clean OOS period; do not reuse contaminated generation."
            ),
        },
        "PRODUCTION": {
            "collector_observed": "check_at_report_time",
            "note": "Reported only; research did not remediate or mutate production.",
            "b2": "read-only",
            "no_pg_historical_scan": True,
        },
        "ML_STATUS": "BLOCKED",
        "RECOMMENDED_HYPOTHESIS": recommendation["RECOMMENDED_HYPOTHESIS"],
        "DECISION": recommendation["DECISION"],
        "NEXT": recommendation["NEXT"],
        "NEXT_NOT_EXECUTED": True,
        "taker_friction_bps": taker_friction,
    }

    markdown = _render_markdown(final)
    _write_reports(final, markdown)
    print(
        json.dumps(
            {
                "STATUS": final["STATUS"],
                "DECISION": final["DECISION"],
                "RECOMMENDED_HYPOTHESIS": final["RECOMMENDED_HYPOTHESIS"],
                "ML_STATUS": "BLOCKED",
                "scorecard_top3": [
                    {
                        "rank": c["rank"],
                        "class": c["STRATEGY_CLASS"],
                        "decision": c["DECISION"],
                    }
                    for c in scorecard[:3]
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
