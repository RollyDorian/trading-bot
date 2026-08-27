"""EXECUTION_AND_HORIZON_REASSESSMENT runner (exploratory corpus only)."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trading_bot.research.pipeline.cost_evidence import build_cost_evidence_report
from trading_bot.research.pipeline.edge import _percentile, join_features_labels
from trading_bot.research.pipeline.event_selection import (
    evaluate_event_class,
    predeclared_event_classes,
)
from trading_bot.research.pipeline.execution_styles import execution_style_matrix
from trading_bot.research.pipeline.horizons import (
    horizon_decay_report,
    simple_longer_horizon_baseline,
    write_labels_extended,
)
from trading_bot.research.pipeline.incremental import (
    bootstrap_registry_from_validated_inventory,
    load_registry,
    plan_incremental_materialization,
    reserve_clean_oos_future,
    save_registry,
)
from trading_bot.research.pipeline.maker_execution import (
    MAKER_DATA_SUPPORT,
    summarize_maker_campaign,
)
from trading_bot.research.pipeline.readiness import (
    evaluate_data_readiness,
    summarize_market_state_coverage,
)

ROOT = Path("data/research/full_corpus")
REPORTS = ROOT / "reports" / "execution_horizon"
INVENTORY = Path("tests/fixtures/research/production_verified_inventory.json")
REGISTRY = ROOT / "corpus_registry.json"
PRIOR = ROOT / "runs" / "prior_continuous"
# Placeholder designation only — do not inspect this future generation for fitting.
CLEAN_OOS_PLACEHOLDER = "RESERVED_NEXT_VERIFIED_GENERATION_AFTER_g_7871913"


def _enrich_market_with_features(
    market_rows: list[dict[str, Any]],
    feature_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    feat_by_time = {row["decision_time"]: row for row in feature_rows}
    keys = (
        "signed_trade_flow_1s",
        "ofi_5s",
        "imbalance",
        "microprice_dev_bps",
    )
    out: list[dict[str, Any]] = []
    for row in market_rows:
        item = dict(row)
        feat = feat_by_time.get(row["decision_time"])
        if feat is not None:
            for key in keys:
                if item.get(key) is None and feat.get(key) is not None:
                    item[key] = feat[key]
        out.append(item)
    return out


def main() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    ms_path = PRIOR / "market_state_1s" / "market_state_1s.parquet"
    feat_path = PRIOR / "features" / "features_v1.parquet"
    ext_labels_path = PRIOR / "labels" / "labels_extended_v1.parquet"

    print("=== extended labels ===", flush=True)
    label_stats = write_labels_extended(ms_path, ext_labels_path)

    market_rows = pq.read_table(ms_path).to_pylist()
    feature_rows = pq.read_table(feat_path).to_pylist()
    label_rows = pq.read_table(ext_labels_path).to_pylist()

    joined = join_features_labels(feat_path, ext_labels_path)
    # Event selection needs market fields that may already live on features.
    if joined:
        span = (
            joined[-1]["decision_time"] - joined[0]["decision_time"]
        ).total_seconds()
    else:
        span = 0.0

    cost = build_cost_evidence_report(ms_path)
    median_spread = float(cost["spread_distribution_bps"]["p50"] or 0.0)
    taker_friction = float(
        cost["plausible_friction_bps"][
            "tier1_taker_plus_median_spread_plus_modeled_latency"
        ]
    )

    flow_abs = sorted(
        abs(float(row["signed_trade_flow_1s"]))
        for row in joined
        if row.get("signed_trade_flow_1s") is not None
        and math.isfinite(float(row["signed_trade_flow_1s"]))
    )
    flow_p99 = _percentile(flow_abs, 0.99) or 0.0

    print("=== maker campaign ===", flush=True)
    maker_rows = _enrich_market_with_features(market_rows, feature_rows)
    maker = summarize_maker_campaign(
        maker_rows,
        feature="signed_trade_flow_1s",
        abs_threshold=flow_p99,
        notional_usd=1_000.0,
        max_wait_seconds=30,
        hold_seconds=15,
    )

    adverse = {
        name: (stats.get("post_fill_signed_mid_15s_mean_bps"))
        for name, stats in maker["scenarios"].items()
    }
    fill_rates = {
        name: stats.get("fill_rate") for name, stats in maker["scenarios"].items()
    }
    styles = execution_style_matrix(
        median_spread_bps=median_spread,
        maker_adverse_selection_bps=adverse,
        maker_fill_rates=fill_rates,
        holding_seconds=15.0,
    )

    print("=== horizon decay ===", flush=True)
    decay = horizon_decay_report(feature_rows, label_rows)

    longer_baselines = []
    for horizon in (120, 300, 600):
        longer_baselines.append(
            simple_longer_horizon_baseline(
                joined,
                feature="signed_trade_flow_1s",
                horizon_s=horizon,
                abs_threshold=flow_p99,
                friction_bps=taker_friction,
            )
        )

    print("=== event selection ===", flush=True)
    classes = predeclared_event_classes(joined)
    event_reports = []
    for style_name, style in styles["styles"].items():
        required = float(style["required_move_bps"])
        for class_name, predicate in classes.items():
            if "flow" in class_name or "trade" in class_name:
                feature_sign = "signed_trade_flow_1s"
            elif "ofi" in class_name:
                feature_sign = "ofi_5s"
            elif "microprice" in class_name:
                feature_sign = "microprice_dev_bps"
            else:
                feature_sign = "imbalance"
            for horizon in (15, 30, 60, 120, 300):
                event_reports.append(
                    {
                        "execution_style": style_name,
                        **evaluate_event_class(
                            joined,
                            name=class_name,
                            predicate=predicate,
                            feature_for_sign=feature_sign,
                            horizon_s=horizon,
                            required_bps=required,
                            seconds_span=span,
                        ),
                    }
                )

    registry = bootstrap_registry_from_validated_inventory(
        INVENTORY, registry_path=REGISTRY
    )
    registry, oos_reserved = reserve_clean_oos_future(
        registry,
        segment_id=CLEAN_OOS_PLACEHOLDER,
        kind="partition_generation",
        source_evidence={
            "note": (
                "Placeholder reservation until the next verified closed generation "
                "after ACTIVE g_7871913 is archived. Do not inspect for thresholds."
            )
        },
    )
    save_registry(REGISTRY, registry)
    coverage = summarize_market_state_coverage(ms_path)
    readiness = evaluate_data_readiness(
        exploratory_coverages=[coverage],
        verified_generation_ids=[
            s["segment_id"]
            for s in registry.get("segments", [])
            if s.get("role") != "oos_clean_future"
            or not str(s.get("segment_id", "")).startswith("RESERVED_")
        ],
        oos_holdout_clean=True,
    )
    b2_index_path = ROOT / "b2_completed_index.json"
    b2_index = (
        json.loads(b2_index_path.read_text(encoding="utf-8"))
        if b2_index_path.exists()
        else []
    )
    materialized = set()
    if (ROOT / "raw").exists():
        for path in (ROOT / "raw").rglob("events.parquet"):
            materialized.add(path.parent.name)
    relevant = [
        row
        for row in b2_index
        if str(row.get("dataset_id", "")) >= "eth-usdt-p_20260810T210000"
    ]
    incremental = plan_incremental_materialization(
        registry=load_registry(REGISTRY),
        b2_completed_index=relevant,
        already_materialized_dataset_ids=materialized
        | {s["segment_id"] for s in registry.get("segments", [])},
    )

    base_maker = maker["scenarios"].get("base") or {}
    cons_maker = maker["scenarios"].get("conservative") or {}
    maker_promising = False
    if (
        (base_maker.get("fills") or 0) >= 50
        and (cons_maker.get("fills") or 0) >= 30
        and (base_maker.get("post_fill_signed_mid_hold_mean_bps") or -1e9) > 0
        and (styles["styles"]["MAKER_TAKER_BASE"]["required_move_bps"] < 1e8)
    ):
        maker_promising = float(
            base_maker.get("post_fill_signed_mid_hold_mean_bps") or 0
        ) > float(styles["styles"]["MAKER_TAKER_BASE"]["required_move_bps"])

    longer_promising = any(
        (item.get("net_bps") or -1e9) > 0 and (item.get("trades") or 0) >= 50
        for item in longer_baselines
    )
    flow_curve = (decay.get("curves") or {}).get("signed_trade_flow_1s") or []
    for point in flow_curve:
        if point.get("horizon_s") in (300, 600):
            extreme = point.get("extreme_signed") or {}
            gross = extreme.get("gross_expected_bps")
            n = extreme.get("n") or 0
            if gross is not None and float(gross) > taker_friction and n >= 50:
                longer_promising = True

    clears = [
        e
        for e in event_reports
        if e.get("clears_required_move")
        and e.get("sample_status") == "ok"
        and e.get("execution_style") == "TAKER_TAKER"
    ]

    if maker_promising and longer_promising:
        decision = "BOTH_PROMISING"
    elif maker_promising:
        decision = "MAKER_EXECUTION_PROMISING"
    elif longer_promising:
        decision = "LONGER_HORIZON_PROMISING"
    else:
        max_longer_gross = max(
            (abs(float(item.get("gross_bps") or 0.0)) for item in longer_baselines),
            default=0.0,
        )
        max_extreme_long = 0.0
        for point in flow_curve:
            g = (point.get("extreme_signed") or {}).get("gross_expected_bps")
            if g is not None:
                max_extreme_long = max(max_extreme_long, abs(float(g)))
        # Structurally below half of taker friction → rethink; else collect more.
        if max(max_longer_gross, max_extreme_long, 2.3) < 0.5 * taker_friction:
            decision = "STRATEGY_RETHINK_REQUIRED"
        elif not readiness["DATA_READY_FOR_ML"] and not clears:
            decision = "COLLECT_MORE_DATA"
        else:
            decision = "STRATEGY_RETHINK_REQUIRED"

    next_text = {
        "MAKER_EXECUTION_PROMISING": (
            "continue collection and validate the passive execution hypothesis "
            "on a fresh untouched generation before considering ML"
        ),
        "LONGER_HORIZON_PROMISING": (
            "continue collection and validate the selected longer-horizon strategy "
            "hypothesis on a fresh untouched generation before considering ML"
        ),
        "BOTH_PROMISING": (
            "pre-register both hypotheses and validate them on future untouched "
            "data without threshold retuning"
        ),
        "COLLECT_MORE_DATA": (
            "continue collection; do not choose execution/horizon from insufficient "
            "samples"
        ),
        "STRATEGY_RETHINK_REQUIRED": (
            "review alternative strategy classes or market/execution assumptions "
            "before further predictive modeling"
        ),
    }[decision]

    final = {
        "STATUS": "EXECUTION_HORIZON_REASSESSMENT_READY",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "DATA_READINESS": readiness,
        "NEW_DATA": incremental,
        "MAKER_DATA_SUPPORT": MAKER_DATA_SUPPORT,
        "MAKER_FILL": maker,
        "EXECUTION_STYLES": styles,
        "HORIZONS": {
            "label_stats": label_stats,
            "decay": decay,
            "longer_baselines": longer_baselines,
        },
        "EVENT_SELECTION": {
            "count": len(event_reports),
            "clears_taker_taker_ok_sample": clears[:20],
            "top_by_gross": sorted(
                [e for e in event_reports if e.get("gross_bps") is not None],
                key=lambda e: abs(float(e["gross_bps"])),
                reverse=True,
            )[:20],
        },
        "BREAK_EVEN": {
            style: styles["styles"][style]["required_move_bps"]
            for style in styles["styles"]
        },
        "PRODUCTION": {
            "collector_observed": "check_at_report_time",
            "note": (
                "Reported separately; research did not restart/remediate. "
                "No production DROP/B2 mutation/PG historical scan."
            ),
            "b2": "read-only",
            "no_pg_historical_scan": True,
        },
        "OOS": {
            "contaminated": "g_7471913_7871913",
            "clean_future_reserved": CLEAN_OOS_PLACEHOLDER,
            "newly_reserved": oos_reserved,
            "inspected_during_selection": False,
        },
        "DECISION": decision,
        "ML_STATUS": "BLOCKED",
        "NEXT": next_text,
        "NEXT_NOT_EXECUTED": True,
        "taker_friction_bps": taker_friction,
        "flow_p99_threshold": flow_p99,
    }

    out_json = REPORTS / "EXECUTION_HORIZON_REASSESSMENT_v1.json"
    out_json.write_text(
        json.dumps(final, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )

    md_lines = _render_markdown(final, decision, next_text, maker, longer_baselines)
    (REPORTS / "EXECUTION_HORIZON_REASSESSMENT_v1.md").write_text(
        "\n".join(md_lines), encoding="utf-8"
    )
    docs = Path("docs")
    (docs / "execution_horizon_reassessment_v1.md").write_text(
        "\n".join(md_lines), encoding="utf-8"
    )
    (docs / "execution_horizon_reassessment_v1.json").write_text(
        json.dumps(final, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "STATUS": final["STATUS"],
                "DECISION": decision,
                "ML_STATUS": "BLOCKED",
                "report": str(out_json),
                "maker_base_fill_rate": base_maker.get("fill_rate"),
                "maker_base_post_fill_15s": base_maker.get(
                    "post_fill_signed_mid_15s_mean_bps"
                ),
                "maker_cons_fill_rate": cons_maker.get("fill_rate"),
                "longer_baselines": longer_baselines,
                "NEXT": next_text,
            },
            indent=2,
            default=str,
        )
    )


def _render_markdown(
    final: dict[str, Any],
    decision: str,
    next_text: str,
    maker: dict[str, Any],
    longer_baselines: list[dict[str, Any]],
) -> list[str]:
    readiness = final["DATA_READINESS"]
    return [
        "# Execution and horizon reassessment v1",
        "",
        f"STATUS: `{final['STATUS']}`",
        f"DECISION: `{decision}`",
        "ML_STATUS: `BLOCKED`",
        "",
        "## DATA_READINESS",
        f"- days: {readiness['calendar_days']['observed']}/"
        f"{readiness['calendar_days']['target']}",
        f"- usable_hours: {readiness['usable_hours']['observed']:.2f}",
        f"- clean_oos_holdout: `{readiness['checks'].get('clean_oos_holdout')}`",
        f"- ACTION: `{readiness['ACTION']}`",
        "",
        "## MAKER_DATA_SUPPORT",
        MAKER_DATA_SUPPORT["assessment"],
        "",
        "```json",
        json.dumps(
            {k: v for k, v in MAKER_DATA_SUPPORT.items() if k != "assessment"},
            indent=2,
        ),
        "```",
        "",
        "## MAKER_FILL (signed_trade_flow p99 join-TOB)",
        "```json",
        json.dumps(maker["scenarios"], indent=2, default=str),
        "```",
        "",
        "## EXECUTION_STYLES required moves",
        "```json",
        json.dumps(final["BREAK_EVEN"], indent=2),
        "```",
        "",
        "## HORIZONS — longer baselines (taker friction)",
        "```json",
        json.dumps(longer_baselines, indent=2, default=str),
        "```",
        "",
        "## EVENT_SELECTION",
        f"- evaluated cells: {final['EVENT_SELECTION']['count']}",
        f"- TAKER_TAKER clears with ok sample: "
        f"{len(final['EVENT_SELECTION']['clears_taker_taker_ok_sample'])}",
        "",
        "### Top by |gross|",
        "```json",
        json.dumps(final["EVENT_SELECTION"]["top_by_gross"][:10], indent=2, default=str),
        "```",
        "",
        "## OOS",
        "```json",
        json.dumps(final["OOS"], indent=2),
        "```",
        "",
        "## PRODUCTION",
        "```json",
        json.dumps(final["PRODUCTION"], indent=2),
        "```",
        "",
        "## NEXT (not executed)",
        next_text,
        "",
    ]


if __name__ == "__main__":
    main()
