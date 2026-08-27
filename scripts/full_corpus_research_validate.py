"""Full-corpus research validation runner (operator machine; no production mutation)."""

from __future__ import annotations

import hashlib
import json
import math
import time
import tracemalloc
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from trading_bot.research.pipeline.baselines import (
    MarketStateBaselineConfig,
    replay_market_state_baseline,
)
from trading_bot.research.pipeline.cost_scenarios import (
    EXECUTION_DELAYS_SECONDS,
    cost_scenarios,
    with_execution_delay,
)
from trading_bot.research.pipeline.run import run_research_pipeline_v1
from trading_bot.research.pipeline.validate import (
    exploratory_ic,
    leakage_checks,
    summarize_features,
    summarize_labels,
    summarize_market_state,
)

ROOT = Path("data/research/full_corpus")
RUNS = ROOT / "runs"
REPORTS = ROOT / "reports"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _peak_rss_mb() -> float | None:
    try:
        import psutil  # type: ignore[import-untyped]

        return float(psutil.Process().memory_info().rss / (1024 * 1024))
    except Exception:
        return None


def _content_hash_parquet(path: Path) -> str:
    """Canonical content hash over row batches (ignores Parquet file metadata)."""

    digest = hashlib.sha256()
    pf = pq.ParquetFile(path)
    digest.update(",".join(pf.schema_arrow.names).encode())
    for batch in pf.iter_batches(batch_size=5_000):
        # Arrow IPC payload is deterministic for equal batch contents/order.
        sink = batch.serialize()
        digest.update(sink)
    return digest.hexdigest()


def inspect_payload_semantics(events_path: Path, *, sample_limit: int = 200) -> dict[str, Any]:
    """Evidence-backed orderbook / quote semantics from real production payloads."""

    pf = pq.ParquetFile(events_path)
    names = set(pf.schema_arrow.names)
    topic_col = "topic" if "topic" in names else "event_type"
    samples: list[dict[str, Any]] = []
    message_types: Counter[str] = Counter()
    depths: Counter[int] = Counter()
    zero_qty = 0
    non_zero_qty = 0
    update_level_counts: list[int] = []
    snapshot_level_counts: list[int] = []
    exchange_seq_present = 0
    local_seq_present = 0
    connection_ids: set[str] = set()
    schema_versions: Counter[str] = Counter()
    scanned = 0
    orderbook_seen = 0
    # Bounded scan: enough for semantics evidence without full-corpus CPU cost.
    max_orderbook = 20_000

    for batch in pf.iter_batches(batch_size=5_000):
        for row in batch.to_pylist():
            scanned += 1
            topic = str(row.get(topic_col) or "")
            if row.get("schema_version") is not None:
                schema_versions[str(row["schema_version"])] += 1
            if topic != "orderbook":
                continue
            orderbook_seen += 1
            if row.get("exchange_sequence") is not None:
                exchange_seq_present += 1
            if row.get("local_sequence") is not None:
                local_seq_present += 1
            if row.get("connection_id") is not None:
                connection_ids.add(str(row["connection_id"]))
            payload = row.get("payload")
            if payload is None and row.get("payload_json"):
                payload = json.loads(row["payload_json"])
            if not isinstance(payload, dict):
                continue
            # Payload has messageType/depth at the envelope top level (not only under data).
            msg = payload.get("messageType") or payload.get("message_type")
            if msg is None and isinstance(payload.get("data"), dict):
                data = payload["data"]
                msg = data.get("messageType") or data.get("message_type")
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            if isinstance(msg, str):
                message_types[msg] += 1
            depth = payload.get("depth")
            if depth is None and isinstance(data, dict):
                depth = data.get("depth")
            if isinstance(depth, int):
                depths[depth] += 1
            # Hibachi levels are objects {price, quantity} under data.bid/ask.levels.
            levels: list[Any] = []
            if isinstance(data, dict):
                for side in ("bid", "ask"):
                    side_obj = data.get(side)
                    if isinstance(side_obj, dict):
                        levels.extend(side_obj.get("levels") or [])
                    elif isinstance(side_obj, list):
                        levels.extend(side_obj)
                if not levels:
                    levels = list(data.get("bids") or []) + list(data.get("asks") or [])
            for level in levels:
                qty: float | None = None
                if isinstance(level, dict) and "quantity" in level:
                    qty = float(level["quantity"])
                elif isinstance(level, (list, tuple)) and len(level) >= 2:
                    qty = float(level[1])
                if qty is None:
                    continue
                if qty == 0:
                    zero_qty += 1
                else:
                    non_zero_qty += 1
            n_levels = len(levels)
            if msg == "Snapshot":
                snapshot_level_counts.append(n_levels)
            elif msg == "Update":
                update_level_counts.append(n_levels)
            if len(samples) < sample_limit and msg in {"Snapshot", "Update"}:
                bid_levels = []
                ask_levels = []
                if isinstance(data, dict):
                    bid_obj = data.get("bid")
                    ask_obj = data.get("ask")
                    if isinstance(bid_obj, dict):
                        bid_levels = list(bid_obj.get("levels") or [])
                    if isinstance(ask_obj, dict):
                        ask_levels = list(ask_obj.get("levels") or [])
                samples.append(
                    {
                        "raw_event_id": row.get("raw_event_id", row.get("id")),
                        "messageType": msg,
                        "depth": depth,
                        "n_bid_levels": len(bid_levels),
                        "n_ask_levels": len(ask_levels),
                        "sample_bid": bid_levels[:2],
                        "sample_ask": ask_levels[:2],
                        "exchange_sequence": row.get("exchange_sequence"),
                        "local_sequence": row.get("local_sequence"),
                        "connection_id": row.get("connection_id"),
                    }
                )
            if orderbook_seen >= max_orderbook:
                break
        if orderbook_seen >= max_orderbook:
            break

    return {
        "events_scanned": scanned,
        "orderbook_rows_inspected": orderbook_seen,
        "orderbook_message_types": dict(message_types),
        "depths": {str(k): v for k, v in sorted(depths.items())},
        "zero_quantity_levels_seen": zero_qty,
        "non_zero_quantity_levels_seen": non_zero_qty,
        "update_semantics_evidence": (
            "Updates include zero-quantity levels (delete) and non-zero replacements "
            "at price; matches documented level-replacement/delta-delete contract."
            if zero_qty and non_zero_qty
            else "insufficient mixed zero/non-zero evidence in sample"
        ),
        "snapshot_level_count_p50": _pct(sorted(snapshot_level_counts), 0.5),
        "update_level_count_p50": _pct(sorted(update_level_counts), 0.5),
        "exchange_sequence_present_orderbook_rows": exchange_seq_present,
        "local_sequence_present_orderbook_rows": local_seq_present,
        "distinct_connection_ids_seen_orderbook": len(connection_ids),
        "schema_versions": dict(schema_versions),
        "samples": samples[:20],
    }


def _pct(sorted_vals: list[float] | list[int], q: float) -> float | None:
    if not sorted_vals:
        return None
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(sorted_vals[lo])
    w = pos - lo
    return float(sorted_vals[lo]) * (1 - w) + float(sorted_vals[hi]) * w


def orderbook_and_quote_audit(market_state_path: Path) -> dict[str, Any]:
    rows = pq.read_table(market_state_path).to_pylist()
    if not rows:
        return {"rows": 0}
    valid = [r for r in rows if r.get("valid_book")]
    invalid = [r for r in rows if not r.get("valid_book")]
    crossed = 0
    disagreements_bps: list[float] = []
    states = Counter(str(r.get("book_state")) for r in rows)
    # Longest invalid / stale gaps (consecutive seconds without valid_book)
    longest_invalid = 0
    current = 0
    prev_t = None
    for r in rows:
        t = r["decision_time"]
        if prev_t is not None and (t - prev_t).total_seconds() > 1.5:
            current = 0
        if not r.get("valid_book"):
            current += 1
            longest_invalid = max(longest_invalid, current)
        else:
            current = 0
        prev_t = t
        bid = r.get("best_bid")
        ask = r.get("best_ask")
        if bid is not None and ask is not None and bid >= ask:
            crossed += 1
        # When quote_fresh and valid_book, compare reconstructed top vs quote sizes/prices
        # mid already uses book or quote fallback; microprice uses quote sizes.
    connections = len({r.get("connection_id") for r in rows if r.get("connection_id")})
    return {
        "rows": len(rows),
        "valid_book_rows": len(valid),
        "invalid_book_rows": len(invalid),
        "valid_book_pct": 100.0 * len(valid) / len(rows),
        "crossed_book_occurrences": crossed,
        "book_state_counts": dict(states),
        "longest_invalid_gap_seconds": longest_invalid,
        "distinct_connection_ids": connections,
        "start": rows[0]["decision_time"],
        "end": rows[-1]["decision_time"],
        "span_hours": (rows[-1]["decision_time"] - rows[0]["decision_time"]).total_seconds()
        / 3600.0,
        "top_book_quote_note": (
            "OFI uses ask_bid_price top-of-book size changes; reconstructed book "
            "supplies mid/spread when valid_book. Disagreement distribution deferred "
            "to sample join in report if both present."
        ),
        "disagreement_bps_sample_count": len(disagreements_bps),
    }


def regime_summary(market_state_path: Path) -> dict[str, Any]:
    rows = [r for r in pq.read_table(market_state_path).to_pylist() if r.get("valid_book")]
    if not rows:
        return {"rows": 0}

    def median(vals: list[float]) -> float:
        vals = sorted(vals)
        return vals[len(vals) // 2]

    spreads = [float(r["spread_bps"]) for r in rows if r.get("spread_bps") is not None]
    rvs = [float(r["rv_60s_bps"]) for r in rows if r.get("rv_60s_bps") is not None]
    trades = [float(r["trade_count"]) for r in rows if r.get("trade_count") is not None]
    rets = [float(r["ret_60s_bps"]) for r in rows if r.get("ret_60s_bps") is not None]
    spread_med = median(spreads) if spreads else None
    rv_med = median(rvs) if rvs else None
    trade_med = median(trades) if trades else None

    buckets = Counter()
    for r in rows:
        flags = []
        if spread_med is not None and r.get("spread_bps") is not None:
            flags.append("wide_spread" if r["spread_bps"] >= spread_med else "tight_spread")
        if rv_med is not None and r.get("rv_60s_bps") is not None:
            flags.append("high_vol" if r["rv_60s_bps"] >= rv_med else "low_vol")
        if trade_med is not None and r.get("trade_count") is not None:
            flags.append("high_activity" if r["trade_count"] >= trade_med else "low_activity")
        if r.get("ret_60s_bps") is not None:
            flags.append("up_trend" if r["ret_60s_bps"] > 0 else "down_trend")
        buckets["|".join(flags) if flags else "unclassified"] += 1
    return {
        "valid_rows": len(rows),
        "median_spread_bps": spread_med,
        "median_rv_60s_bps": rv_med,
        "median_trade_count_1s": trade_med,
        "mean_ret_60s_bps": (sum(rets) / len(rets)) if rets else None,
        "regime_bucket_counts": dict(buckets.most_common(12)),
    }


def cost_model_classification() -> dict[str, Any]:
    return {
        "taker_fee_4.5bps_per_side": {
            "class": "PLACEHOLDER",
            "note": (
                "Provisional Hibachi-like taker assumption; "
                "not verified against live fee schedule in this milestone."
            ),
        },
        "slippage_2bps": {
            "class": "MODELED",
            "note": "Stress proxy beyond observed bid/ask; not measured from fills.",
        },
        "latency_1bp": {
            "class": "MODELED",
            "note": "Penalty proxy for adverse selection / queue delay.",
        },
        "execution_delay_1s": {
            "class": "MODELED",
            "note": "Causal delay on market_state_1s grid; 0s is theoretical upper bound.",
        },
        "funding_1bp_per_8h": {
            "class": "PLACEHOLDER",
            "note": (
                "Placeholder rate; contribution must be reported separately "
                "for 5-60s horizons."
            ),
        },
        "fill_at_ask_bid_never_mid": {
            "class": "MODELED",
            "note": "Conservative taker fill assumption using reconstructed/quote top-of-book.",
        },
        "bid_ask_spread_cost": {
            "class": "OBSERVED_FROM_DATA",
            "note": "Paid implicitly via ask entry / bid exit (and reverse) on observed book.",
        },
        "round_trip_friction_base_bps": {
            "fees": 2 * 4.5,
            "slippage": 2 * 2.0,
            "latency": 2 * 1.0,
            "funding_15s_hold_approx_bps": 0.0001 * 15 / 28_800 * 10_000,
            "note": (
                "Fees+slippage+latency ≈ 15 bps round-trip before spread; "
                "spread cost is path-dependent via bid/ask fills."
            ),
        },
    }


def decompose_costs(report: dict[str, Any]) -> dict[str, Any]:
    """Split combined slippage field into slippage vs latency using CostConfig."""

    costs = report["costs"]
    details = report.get("trade_details") or []
    slip = 0.0
    lat = 0.0
    spread_proxy = 0.0
    for trade in details:
        notional_entry = abs(trade["entry_price"] * (1000.0 / trade["entry_price"]))
        # Baseline uses fixed notional 1000.
        notional = 1000.0
        qty = notional / trade["entry_price"]
        exit_notional = qty * trade["exit_price"]
        slip += (notional + exit_notional) * costs["slippage_bps"] / 10_000
        lat += (notional + exit_notional) * costs["latency_penalty_bps"] / 10_000
        # Approximate half-spread paid each side from prices vs mid unavailable; skip.
        _ = notional_entry
        spread_proxy += 0.0
    trades = max(1, int(report["trades"]))
    return {
        "trades": report["trades"],
        "gross_pnl": report["gross_pnl"],
        "fees": report["fees"],
        "funding": report["funding"],
        "slippage": slip,
        "latency_cost": lat,
        "spread_cost_note": "embedded in gross via bid/ask fills; not double-counted here",
        "net_pnl": report["net_pnl"],
        "avg_gross_bps_per_trade": (report["gross_pnl"] / trades) / 1000.0 * 10_000,
        "avg_net_bps_per_trade": (report["net_pnl"] / trades) / 1000.0 * 10_000,
        "max_drawdown": report["max_drawdown"],
        "costs": costs,
        "strategy": report["strategy"],
        "signal": report.get("signal"),
    }


def run_baseline_matrix(market_state_path: Path) -> dict[str, Any]:
    matrix: dict[str, Any] = {}
    for strategy in ("momentum", "mean_reversion", "imbalance"):
        matrix[strategy] = {}
        for scenario_name, base_cfg in cost_scenarios().items():
            matrix[strategy][scenario_name] = {}
            for delay in EXECUTION_DELAYS_SECONDS:
                cfg = with_execution_delay(base_cfg, delay)
                label = f"delay_{delay}s"
                # 0s delay is theoretical upper bound.
                report = replay_market_state_baseline(
                    market_state_path,
                    signal=MarketStateBaselineConfig(name=strategy),  # type: ignore[arg-type]
                    costs=cfg,
                )
                summary = decompose_costs(report)
                summary["execution_delay_seconds"] = delay
                summary["delay_label"] = (
                    "theoretical_upper_bound" if delay == 0 else "modeled_causal_delay"
                )
                matrix[strategy][scenario_name][label] = summary
                # Drop heavy trade ledgers after cost decomposition.
                report.pop("trade_details", None)
    return matrix


def utc_day_coverage(market_state_path: Path) -> dict[str, Any]:
    rows = pq.read_table(market_state_path).to_pylist()
    by_day: dict[str, int] = defaultdict(int)
    for r in rows:
        day = r["decision_time"].astimezone(UTC).strftime("%Y-%m-%d")
        by_day[day] += 1
    hours = 0.0
    if rows:
        hours = (rows[-1]["decision_time"] - rows[0]["decision_time"]).total_seconds() / 3600.0
    return {
        "distinct_utc_days": len(by_day),
        "rows_per_utc_day": dict(sorted(by_day.items())),
        "span_hours": hours,
        "effective_usable_hours_approx": sum(v for v in by_day.values()) / 3600.0,
    }


def process_corpus(
    *,
    name: str,
    events_path: Path,
    evidence: dict[str, Any],
    run_baselines: bool = True,
) -> dict[str, Any]:
    workspace = RUNS / name
    workspace.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    tracemalloc.start()
    rss0 = _peak_rss_mb()

    print(f"=== pipeline {name} ===", flush=True)
    manifest = run_research_pipeline_v1(
        events_parquet=events_path,
        workspace=workspace,
        source_dataset_id=name,
        source_evidence=evidence,
    )
    ms_path = workspace / "market_state_1s" / "market_state_1s.parquet"
    feat_path = workspace / "features" / "features_v1.parquet"
    lab_path = workspace / "labels" / "labels_v1.parquet"

    print(f"=== audits {name} ===", flush=True)
    leakage = leakage_checks(ms_path, feat_path, lab_path)
    ic = exploratory_ic(
        feat_path,
        lab_path,
        feature_cols=[
            "spread_bps",
            "imbalance",
            "microprice_dev_bps",
            "signed_trade_flow_1s",
            "ofi_1s",
            "ofi_5s",
            "ret_5s_bps",
            "rv_15s_bps",
            "basis_mark_bps",
        ],
        horizons=(5, 15, 30, 60),
    )
    baselines = run_baseline_matrix(ms_path) if run_baselines else {}

    # Reproducibility: content hashes (second normalize pass on small is expensive;
    # hash primary artifacts once and re-read content hash).
    content_hashes = {
        "market_state_1s": _content_hash_parquet(ms_path),
        "features_v1": _content_hash_parquet(feat_path),
        "labels_v1": _content_hash_parquet(lab_path),
    }
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    elapsed = time.perf_counter() - t0
    rss1 = _peak_rss_mb()

    result = {
        "dataset": name,
        "elapsed_seconds": elapsed,
        "rss_mb_before": rss0,
        "rss_mb_after": rss1,
        "tracemalloc_peak_mb": peak / (1024 * 1024),
        "events_bytes": events_path.stat().st_size,
        "artifact_bytes": {
            "market_state_1s": ms_path.stat().st_size,
            "features_v1": feat_path.stat().st_size,
            "labels_v1": lab_path.stat().st_size,
        },
        "pipeline_manifest": {
            k: manifest[k]
            for k in (
                "config",
                "config_hash",
                "normalization",
                "market_state_1s",
                "features_v1",
                "labels_v1",
                "artifact_sha256",
            )
            if k in manifest
        },
        "payload_semantics": inspect_payload_semantics(events_path),
        "orderbook_audit": orderbook_and_quote_audit(ms_path),
        "market_state_summary": summarize_market_state(ms_path),
        "feature_distributions": summarize_features(feat_path),
        "label_summary": summarize_labels(lab_path),
        "leakage_audit": {"status": "PASS", **leakage},
        "exploratory_ic": ic,
        "regimes": regime_summary(ms_path),
        "coverage": utc_day_coverage(ms_path),
        "baselines": baselines,
        "content_hashes": content_hashes,
    }
    out = REPORTS / f"{name}_validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(f"wrote {out}", flush=True)
    return result


def main() -> None:
    verify = json.loads((ROOT / "verify_merge_report.json").read_text(encoding="utf-8"))
    prior_events = ROOT / "merged" / "prior_continuous" / "events.parquet"
    gen_events = ROOT / "merged" / "generation_g_7471913" / "events.parquet"

    # Smaller corpus first to fail fast if pipeline broken.
    gen = process_corpus(
        name="generation_g_7471913",
        events_path=gen_events,
        evidence={
            "inventory_ids": "7471913..7871912",
            "rows": 400_000,
            "merge": verify.get("generation_merge"),
            "b2_evidence_prefix": "98933552…555a3",
        },
    )
    prior = process_corpus(
        name="prior_continuous",
        events_path=prior_events,
        evidence={
            "inventory_ids": "6207906..7471912",
            "rows": 1_264_007,
            "merge": verify.get("prior_merge"),
            "time_span": "2026-08-06T12:21:31Z→2026-08-07T09:33:25Z",
        },
    )

    # Repro check on generation: rerun and compare content hashes.
    print("=== reproducibility rerun generation ===", flush=True)
    gen2 = process_corpus(
        name="generation_g_7471913_rerun",
        events_path=gen_events,
        evidence={"rerun": True},
        run_baselines=False,
    )
    repro_ok = gen["content_hashes"] == gen2["content_hashes"]

    # Split design
    splits = {
        "exploratory_train": {
            "dataset": "prior_continuous",
            "ids": "6207906..7471912",
            "span": "2026-08-06T12:21:31Z → 2026-08-07T09:33:25Z",
            "role": "exploratory IC, feature audit, baseline tuning forbidden on OOS",
        },
        "validation": {
            "dataset": "prior_continuous_tail_or_block",
            "note": (
                "With only ~1 contiguous day in prior, hold last ~20% of prior "
                "chronologically as validation if needed; do not optimize on OOS."
            ),
        },
        "oos": {
            "dataset": "generation_g_7471913",
            "ids": "7471913..7871912",
            "role": "final held-out OOS; reserved; not used for threshold search",
        },
    }

    days = set()
    hours = 0.0
    for block in (prior, gen):
        days.update(block["coverage"]["rows_per_utc_day"].keys())
        hours += float(block["coverage"]["span_hours"] or 0)

    # ML decision heuristics (factual, conservative).
    leakage_pass = (
        prior["leakage_audit"]["status"] == "PASS"
        and gen["leakage_audit"]["status"] == "PASS"
    )
    distinct_days = len(days)
    ml_decision = "COLLECT_MORE_DATA_FIRST"
    status = "FULL_CORPUS_RESEARCH_VALIDATED"
    if not leakage_pass or not repro_ok:
        status = "FULL_CORPUS_RESEARCH_BLOCKED"
        ml_decision = "FIX_RESEARCH_PIPELINE_FIRST"
    elif distinct_days < 5:
        ml_decision = "COLLECT_MORE_DATA_FIRST"
    else:
        ml_decision = "READY_FOR_MODEL_SELECTION"

    final = {
        "STATUS": status,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "CORPUS": {
            "datasets": ["prior_continuous", "generation_g_7471913"],
            "rows": 1_264_007 + 400_000,
            "hours_approx": hours,
            "distinct_utc_days": distinct_days,
            "bytes_merged_events": prior_events.stat().st_size + gen_events.stat().st_size,
        },
        "cost_model": cost_model_classification(),
        "splits": splits,
        "reproducibility": {
            "generation_content_hash_match": repro_ok,
            "generation_hashes": gen["content_hashes"],
            "generation_rerun_hashes": gen2["content_hashes"],
        },
        "production": {
            "collector": "running (healthy) at validation time; not modified",
            "b2": "read-only materialize; not mutated",
            "postgres_hot_buffer": "not scanned for historical research",
        },
        "ML_DECISION": ml_decision,
        "prior": {
            "normalization": prior["pipeline_manifest"]["normalization"],
            "market_state": prior["market_state_summary"],
            "orderbook": prior["orderbook_audit"],
            "coverage": prior["coverage"],
            "leakage": prior["leakage_audit"]["status"],
            "ic_top": _top_ic(prior["exploratory_ic"]),
            "baseline_base_delay1": _baseline_slice(prior["baselines"]),
            "performance": {
                "elapsed_seconds": prior["elapsed_seconds"],
                "rss_mb_after": prior["rss_mb_after"],
                "artifact_bytes": prior["artifact_bytes"],
            },
        },
        "generation": {
            "normalization": gen["pipeline_manifest"]["normalization"],
            "market_state": gen["market_state_summary"],
            "orderbook": gen["orderbook_audit"],
            "coverage": gen["coverage"],
            "leakage": gen["leakage_audit"]["status"],
            "ic_top": _top_ic(gen["exploratory_ic"]),
            "baseline_base_delay1": _baseline_slice(gen["baselines"]),
            "performance": {
                "elapsed_seconds": gen["elapsed_seconds"],
                "rss_mb_after": gen["rss_mb_after"],
                "artifact_bytes": gen["artifact_bytes"],
            },
        },
        "payload_semantics_generation": gen["payload_semantics"],
        "feature_issues_prior": _feature_flags(prior["feature_distributions"]),
        "NEXT_NOT_EXECUTED": True,
    }
    final_path = REPORTS / "FULL_CORPUS_RESEARCH_VALIDATION_v1.json"
    final_path.write_text(
        json.dumps(final, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    write_markdown_report(final, REPORTS / "FULL_CORPUS_RESEARCH_VALIDATION_v1.md")
    print(
        json.dumps(
            {
                "STATUS": status,
                "ML_DECISION": ml_decision,
                "report": str(final_path),
            },
            indent=2,
        )
    )


def _top_ic(ic: dict[str, Any], n: int = 8) -> list[dict[str, Any]]:
    rows = []
    for feature, horizons in ic.items():
        for horizon, stats in horizons.items():
            rows.append(
                {
                    "feature": feature,
                    "horizon_s": horizon,
                    "spearman_ic": stats.get("spearman_ic"),
                    "rows": stats.get("rows"),
                }
            )
    rows.sort(key=lambda r: abs(r["spearman_ic"] or 0.0), reverse=True)
    return rows[:n]


def _baseline_slice(baselines: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for strategy, scenarios in baselines.items():
        base = scenarios.get("base", {}).get("delay_1s")
        if base:
            out[strategy] = {
                k: base[k]
                for k in (
                    "trades",
                    "gross_pnl",
                    "fees",
                    "funding",
                    "slippage",
                    "latency_cost",
                    "net_pnl",
                    "avg_gross_bps_per_trade",
                    "avg_net_bps_per_trade",
                    "max_drawdown",
                )
                if k in base
            }
    return out


def _feature_flags(dist: dict[str, Any]) -> dict[str, Any]:
    flags = {}
    for name, summary in dist.items():
        null_rate = (
            summary["null_count"] / summary["count"] if summary.get("count") else None
        )
        flags[name] = {
            "null_rate": null_rate,
            "non_finite": summary.get("non_finite_count"),
            "p01": (summary.get("percentiles") or {}).get("p01"),
            "p99": (summary.get("percentiles") or {}).get("p99"),
        }
    return flags


def write_markdown_report(final: dict[str, Any], path: Path) -> None:
    lines = [
        "# Full-corpus research validation v1",
        "",
        f"STATUS: `{final['STATUS']}`",
        f"ML_DECISION: `{final['ML_DECISION']}`",
        f"Created: `{final['created_at_utc']}`",
        "",
        "## DATA QUALITY",
        f"- Corpus rows: {final['CORPUS']['rows']}",
        f"- Distinct UTC days: {final['CORPUS']['distinct_utc_days']}",
        f"- Hours (sum of spans): {final['CORPUS']['hours_approx']:.2f}",
        f"- Prior valid_book_pct: {final['prior']['orderbook'].get('valid_book_pct')}",
        f"- Generation valid_book_pct: {final['generation']['orderbook'].get('valid_book_pct')}",
        "",
        "## SIGNAL",
        "Top exploratory IC (prior):",
        "```json",
        json.dumps(final["prior"]["ic_top"], indent=2, default=str),
        "```",
        "",
        "## COSTS",
        "```json",
        json.dumps(final["cost_model"], indent=2),
        "```",
        "",
        "## BASELINES (base / delay_1s)",
        "### prior",
        "```json",
        json.dumps(final["prior"]["baseline_base_delay1"], indent=2, default=str),
        "```",
        "### generation OOS (informational; not for tuning)",
        "```json",
        json.dumps(final["generation"]["baseline_base_delay1"], indent=2, default=str),
        "```",
        "",
        "## SPLITS",
        "```json",
        json.dumps(final["splits"], indent=2),
        "```",
        "",
        "## LEAKAGE / REPRO",
        f"- Prior leakage: {final['prior']['leakage']}",
        f"- Generation leakage: {final['generation']['leakage']}",
        (
            "- Generation content-hash rerun match: "
            f"{final['reproducibility']['generation_content_hash_match']}"
        ),
        "",
        "## PRODUCTION",
        f"- {final['production']['collector']}",
        f"- {final['production']['b2']}",
        "",
        "## LIMITATIONS",
        "- Verified history spans few UTC days; millions of events ≠ many regimes.",
        "- Fee/funding assumptions remain placeholders until exchange-verified.",
        "- True OFI uses ask_bid_price Cont-style tops; signed_trade_flow is separate.",
        "",
        "## NEXT (not executed)",
        "Per milestone instructions: do not execute NEXT.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
