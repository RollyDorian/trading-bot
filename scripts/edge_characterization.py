"""Run DATA_ACCUMULATION_AND_EDGE_CHARACTERIZATION on exploratory corpus only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trading_bot.research.pipeline.cost_evidence import (
    break_even_matrix,
    build_cost_evidence_report,
)
from trading_bot.research.pipeline.edge import characterize_exploratory_corpus
from trading_bot.research.pipeline.incremental import (
    bootstrap_registry_from_validated_inventory,
    load_registry,
    plan_incremental_materialization,
)
from trading_bot.research.pipeline.protocol_draft import draft_research_protocol
from trading_bot.research.pipeline.readiness import (
    evaluate_data_readiness,
    summarize_market_state_coverage,
)

ROOT = Path("data/research/full_corpus")
REPORTS = ROOT / "reports" / "edge_characterization"
INVENTORY = Path("tests/fixtures/research/production_verified_inventory.json")
REGISTRY = ROOT / "corpus_registry.json"


def _extreme_summary(edge_report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for signal in edge_report.get("signals", []):
        for key, bucket in (signal.get("buckets") or {}).items():
            if not str(key).startswith("abs_top_"):
                continue
            rows.append(
                {
                    "feature": signal["feature"],
                    "horizon_s": signal["horizon_s"],
                    "bucket": key,
                    "n": bucket.get("n"),
                    "gross_bps": bucket.get("gross_expected_bps"),
                    "stderr": bucket.get("stderr"),
                    "status": bucket.get("status"),
                }
            )
    rows.sort(
        key=lambda item: abs(item["gross_bps"] or 0.0),
        reverse=True,
    )
    return rows[:20]


def _frontier_highlights(edge_report: dict[str, Any]) -> list[dict[str, Any]]:
    highlights: list[dict[str, Any]] = []
    for signal in edge_report.get("signals", []):
        frontier = signal.get("frontier") or []
        if not frontier:
            continue
        # Prefer highest |gross| among thresholds with n>=30.
        candidates = [item for item in frontier if (item.get("trades") or 0) >= 30]
        if not candidates:
            candidates = frontier
        best = max(
            candidates,
            key=lambda item: abs(item.get("gross_bps_per_trade") or 0.0),
        )
        highlights.append(
            {
                "feature": signal["feature"],
                "horizon_s": signal["horizon_s"],
                **best,
            }
        )
    highlights.sort(
        key=lambda item: abs(item.get("gross_bps_per_trade") or 0.0),
        reverse=True,
    )
    return highlights[:15]


def main() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    prior_ms = ROOT / "runs/prior_continuous/market_state_1s/market_state_1s.parquet"
    prior_feat = ROOT / "runs/prior_continuous/features/features_v1.parquet"
    prior_lab = ROOT / "runs/prior_continuous/labels/labels_v1.parquet"

    registry = bootstrap_registry_from_validated_inventory(
        INVENTORY, registry_path=REGISTRY
    )
    # Local B2 completed index if present (operator machine / synced).
    b2_index_path = ROOT / "b2_completed_index.json"
    if b2_index_path.exists():
        b2_index = json.loads(b2_index_path.read_text(encoding="utf-8"))
    else:
        b2_index = []
    materialized: set[str] = set()
    raw_root = ROOT / "raw"
    if raw_root.exists():
        for path in raw_root.rglob("events.parquet"):
            materialized.add(path.parent.name)
    # Windows already listed in the first full-corpus materialize manifest.
    manifest_path = ROOT / "materialize_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for bucket in ("prior", "generation"):
            for item in manifest.get(bucket, []):
                if item.get("dataset_id"):
                    materialized.add(str(item["dataset_id"]))
    for segment in registry.get("segments", []):
        materialized.add(str(segment["segment_id"]))
    # Only consider windows at/after the validated generation span for "new".
    # Older B2 COMPLETED objects outside the research inventory are ignored here.
    relevant = [
        row
        for row in b2_index
        if str(row.get("dataset_id", "")) >= "eth-usdt-p_20260810T210000"
    ]
    incremental_plan = plan_incremental_materialization(
        registry=load_registry(REGISTRY),
        b2_completed_index=relevant,
        already_materialized_dataset_ids=materialized,
    )
    incremental_plan["relevant_window_filter"] = ">= eth-usdt-p_20260810T210000"
    incremental_plan["materialized_window_count"] = len(
        [item for item in materialized if item.startswith("eth-usdt-p_")]
    )

    prior_coverage = summarize_market_state_coverage(prior_ms)
    # Verified generations currently: prior continuous + one closed generation.
    verified_gens = [
        segment["segment_id"]
        for segment in registry.get("segments", [])
        if segment.get("kind") in {"partition_generation", "pre_partition_continuous"}
    ]
    readiness = evaluate_data_readiness(
        exploratory_coverages=[prior_coverage],
        verified_generation_ids=verified_gens,
        oos_holdout_clean=False,
    )

    cost_report = build_cost_evidence_report(prior_ms)
    friction = float(
        cost_report["plausible_friction_bps"][
            "tier1_taker_plus_median_spread_plus_modeled_latency"
        ]
    )
    edge_report = characterize_exploratory_corpus(
        prior_feat,
        prior_lab,
        prior_ms,
        friction_bps_round_trip=friction,
    )

    extremes = _extreme_summary(edge_report)
    frontiers = _frontier_highlights(edge_report)
    break_evens = break_even_matrix(
        [
            {
                "signal": item["feature"],
                "horizon_s": item["horizon_s"],
                "bucket": item["bucket"],
                "gross_bps": item["gross_bps"],
                "n": item["n"],
            }
            for item in extremes
            if item.get("gross_bps") is not None
        ],
        current_plausible_friction_bps=friction,
    )

    max_gross = max((abs(item.get("gross_bps") or 0.0) for item in extremes), default=0.0)
    # Economic gate for modeling: need gross edge that can clear plausible friction
    # with some margin on exploratory data, plus readiness.
    edge_sufficient = max_gross >= friction
    if readiness["DATA_READY_FOR_ML"] and edge_sufficient:
        ml_decision = "READY_FOR_MODEL_SELECTION"
        status = "EDGE_CHARACTERIZATION_READY"
    elif max_gross > 0 and max_gross < 0.5 * friction:
        ml_decision = "EDGE_INSUFFICIENT_FOR_CURRENT_HORIZON"
        status = "EDGE_CHARACTERIZATION_READY"
    else:
        ml_decision = "COLLECT_MORE_DATA_FIRST"
        status = "EDGE_CHARACTERIZATION_READY"

    # Recommend horizons/features from extreme gross (still DRAFT).
    top_features = []
    for item in extremes:
        if item["feature"] not in top_features and (item.get("gross_bps") or 0) > 0:
            top_features.append(item["feature"])
        if len(top_features) >= 4:
            break
    top_horizons = sorted(
        {
            int(item["horizon_s"])
            for item in extremes[:8]
            if (item.get("gross_bps") or 0) > 0
        }
    ) or [5, 15]

    protocol = draft_research_protocol(
        recommended_horizons=top_horizons,
        feature_subset=top_features
        or ["imbalance", "microprice_dev_bps", "ofi_5s"],
        friction_bps=friction,
        data_ready=bool(readiness["DATA_READY_FOR_ML"]),
    )

    final = {
        "STATUS": status,
        "DATA_READINESS": readiness,
        "NEW_DATA": incremental_plan,
        "COST_MODEL": cost_report,
        "SIGNALS_EXTREME": extremes,
        "TRADE_FRONTIER": frontiers,
        "BREAK_EVEN": break_evens,
        "CONJUNCTIONS": edge_report.get("conjunctions"),
        "OOS": {
            "current_holdout": "g_7471913_7871913",
            "clean": False,
            "contamination_note": (
                "Inspected during full-corpus validation IC/baselines; "
                "not eligible for threshold fitting."
            ),
            "future_untouched_holdout_plan": (
                "Reserve the next newly verified generation or contiguous "
                "multi-day block after ACTIVE g_7871913 closes."
            ),
        },
        "PROTOCOL_DRAFT": protocol,
        "PRODUCTION": {
            "collector": "running; not modified by this milestone",
            "b2": "read-only discovery; no mutation",
        },
        "ML_DECISION": ml_decision,
        "NEXT_NOT_EXECUTED": True,
        "max_exploratory_gross_bps": max_gross,
        "plausible_friction_bps": friction,
    }

    out_json = REPORTS / "EDGE_CHARACTERIZATION_v1.json"
    out_json.write_text(
        json.dumps(final, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    # Compact markdown
    lines = [
        "# Edge characterization v1",
        "",
        f"STATUS: `{status}`",
        f"ML_DECISION: `{ml_decision}`",
        f"DATA_READY_FOR_ML: `{readiness['DATA_READY_FOR_ML']}`",
        f"calendar_days: {readiness['calendar_days']['observed']} / "
        f"{readiness['calendar_days']['target']}",
        f"usable_hours: {readiness['usable_hours']['observed']:.2f}",
        f"verified_generations: {readiness['verified_generations']['observed']} / "
        f"{readiness['verified_generations']['target']}",
        f"ACTION: `{readiness['ACTION']}`",
        "",
        f"NEW_DATA action: `{incremental_plan['action']}` "
        f"({incremental_plan['new_window_count']} new windows)",
        "",
        f"Plausible friction (bps): `{friction:.3f}`",
        f"Max exploratory extreme gross (bps): `{max_gross:.3f}`",
        "",
        "## Top extreme signed gross",
        "```json",
        json.dumps(extremes[:10], indent=2, default=str),
        "```",
        "",
        "## Break-even sample",
        "```json",
        json.dumps(break_evens[:8], indent=2, default=str),
        "```",
        "",
        "## OOS",
        json.dumps(final["OOS"], indent=2),
        "",
        "## NEXT (not executed)",
        "Per milestone instructions.",
        "",
    ]
    (REPORTS / "EDGE_CHARACTERIZATION_v1.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )
    # Also publish under docs/
    docs = Path("docs")
    docs.mkdir(exist_ok=True)
    (docs / "edge_characterization_v1.md").write_text("\n".join(lines), encoding="utf-8")
    (docs / "edge_characterization_v1.json").write_text(
        json.dumps(final, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    (docs / "research_protocol_draft_v1.json").write_text(
        json.dumps(protocol, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "STATUS": status,
                "ML_DECISION": ml_decision,
                "DATA_READY_FOR_ML": readiness["DATA_READY_FOR_ML"],
                "friction_bps": friction,
                "max_gross_bps": max_gross,
                "report": str(out_json),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
