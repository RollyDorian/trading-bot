"""ETH_FIRST_PASSAGE_FULL_CORPUS_EXPANSION_V1 runner.

Read-only vs B2 (GET/list only). No production PostgreSQL historical scan.
Does not overwrite docs/eth_first_passage_opportunity_v1.*.
Does not retune first-passage horizons or thresholds.
No ML, PAPER, or live trading.
"""

from __future__ import annotations

import json
import os
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from trading_bot.research.collection_gaps import load_collection_gaps
from trading_bot.research.pipeline.cost_evidence import (
    funding_contribution_bps,
    hibachi_public_fee_schedule,
    round_trip_friction_bps,
)
from trading_bot.research.pipeline.first_passage_corpus import (
    freeze_full_corpus_expansion,
    list_verified_eth_completed,
    load_operator_b2_environ,
    materialize_discovery_market_state,
)
from trading_bot.research.pipeline.first_passage_opportunity import (
    FIRST_PASSAGE_HORIZONS_SECONDS,
    FIRST_PASSAGE_THRESHOLDS_BPS,
    TAKER_RT_BPS_REFERENCE,
    _percentile,
    analyze_first_passage,
    commentary_cells,
    cost_overlay,
    day_block_stability,
    extract_exec_nonoverlap_snapshot,
    filter_known_gap_rows,
    load_executable_series_from_parquet,
    render_full_corpus_markdown,
    slice_series_by_time,
)

ROOT = Path("data/research/full_corpus")
DOCS = Path("docs")
LIVE_INDEX = ROOT / "b2_completed_index_live.json"
V1_JSON = DOCS / "eth_first_passage_opportunity_v1.json"
AUG6_START = datetime(2026, 8, 6, tzinfo=UTC)
AUG6_END = datetime(2026, 8, 7, tzinfo=UTC)


def _peak_rss_mib() -> float | None:
    try:
        import psutil  # type: ignore[import-untyped]

        return float(psutil.Process().memory_info().rss / (1024 * 1024))
    except Exception:
        pass
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        fn = ctypes.windll.psapi.GetProcessMemoryInfo
        fn.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD,
        ]
        fn.restype = wintypes.BOOL
        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ok = fn(handle, ctypes.byref(counters), counters.cb)
        if not ok:
            return None
        return float(counters.PeakWorkingSetSize) / (1024 * 1024)
    except Exception:
        return None


def _taker_rt_bps(spreads: list[float]) -> tuple[float, float | None]:
    ordered = sorted(spreads)
    median = _percentile(ordered, 0.50)
    fees = hibachi_public_fee_schedule()
    friction = round_trip_friction_bps(
        taker_fee_rate=float(fees["tier1_taker_fee_rate"]),
        slippage_bps_per_side=0.0,
        latency_bps_per_side=1.0,
        spread_bps_round_trip=float(median or 0.0),
        funding_bps=funding_contribution_bps(15.0),
    )
    return friction, median


def _live_b2_inventory() -> dict[str, Any]:
    """List COMPLETED ETH archives or reuse this session's live index. GET/list only."""

    env_meta = load_operator_b2_environ()
    if LIVE_INDEX.is_file():
        payload = cast(
            dict[str, Any], json.loads(LIVE_INDEX.read_text(encoding="utf-8"))
        )
        payload["credential_filenames"] = env_meta.get("loaded_filenames")
        payload["inventory_source"] = "session_live_index"
        return payload
    try:
        from trading_bot.archive.b2 import B2ArchiveConfig
        from trading_bot.archive.store import S3ArchiveStore
    except Exception as error:
        return {
            "status": "UNAVAILABLE",
            "error": type(error).__name__,
            "windows": [],
            "credential_filenames": env_meta.get("loaded_filenames"),
            "mutations": False,
        }
    try:
        store = S3ArchiveStore.for_b2(B2ArchiveConfig.from_environ())
        windows = list_verified_eth_completed(store)
        dates = sorted(
            {(row.get("start_utc") or "")[:10] for row in windows if row.get("start_utc")}
        )
        payload = {
            "status": "LIVE_COMPLETED_MARKERS",
            "window_count": len(windows),
            "utc_dates": dates,
            "quarantined_count": sum(1 for row in windows if row.get("quarantined")),
            "quality_pass_count": sum(
                1 for row in windows if row.get("research_quality_status") == "pass"
            ),
            "windows": windows,
            "mutations": False,
            "credential_filenames": env_meta.get("loaded_filenames"),
            "inventory_source": "live_list_verified_eth_completed",
        }
        LIVE_INDEX.parent.mkdir(parents=True, exist_ok=True)
        LIVE_INDEX.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        return payload
    except Exception as error:
        return {
            "status": "LIVE_LISTING_FAILED",
            "error": type(error).__name__,
            "windows": [],
            "credential_filenames": env_meta.get("loaded_filenames"),
            "mutations": False,
        }


def _open_b2_store() -> Any:
    from trading_bot.archive.b2 import B2ArchiveConfig
    from trading_bot.archive.store import S3ArchiveStore

    load_operator_b2_environ()
    return S3ArchiveStore.for_b2(B2ArchiveConfig.from_environ())


def _v1_snapshot() -> dict[str, Any]:
    if not V1_JSON.is_file():
        return {}
    payload = json.loads(V1_JSON.read_text(encoding="utf-8"))
    snap = extract_exec_nonoverlap_snapshot(payload)
    corpus = payload.get("corpus") or {}
    return {
        "cells": snap,
        "discovery_usable_hours": corpus.get("discovery_usable_hours"),
        "discovery_utc_dates": corpus.get("discovery_utc_dates"),
    }


def _findings(
    *,
    v1_cells: dict[str, Any],
    aug6_cells: dict[str, Any],
    expanded_cells: dict[str, Any],
    stability: dict[str, Any],
    v1_hours: float | None,
    expanded_hours: float,
    aug6_hours: float,
) -> tuple[str, list[str]]:
    keys = (
        "60s_20bps",
        "120s_20bps",
        "300s_20bps",
        "600s_20bps",
        "60s_10bps",
        "600s_30bps",
    )
    dist = stability.get("distribution_across_days") or {}
    day_rows = {row.get("utc_date"): row for row in stability.get("per_utc_day") or []}
    aug6_day = day_rows.get("2026-08-06") or {}
    notes: list[str] = []
    for key in keys:
        v1_frac = (v1_cells.get(key) or {}).get("either_side_hit_fraction_mean")
        exp_frac = (expanded_cells.get(key) or {}).get("either_side_hit_fraction_mean")
        a6_frac = (aug6_cells.get(key) or {}).get("either_side_hit_fraction_mean")
        spread = dist.get(key) or {}
        if v1_frac is not None and exp_frac is not None:
            denom = max(abs(float(v1_frac)), 1e-9)
            rel = abs(float(exp_frac) - float(v1_frac)) / denom
            if rel <= 0.25:
                notes.append(
                    f"{key}: expanded discovery hit fraction is within 25% relative "
                    f"of the published Aug-6-only v1 cell ({v1_frac:.4f} → {exp_frac:.4f}); "
                    "this piece looks stable."
                )
            else:
                notes.append(
                    f"{key}: expanded discovery hit fraction moved {rel:.0%} relative "
                    f"to v1 Aug-6-only ({v1_frac:.4f} → {exp_frac:.4f}). Treat the v1 "
                    "point estimate as a sample artifact until day-level min/median/max agree."
                )
        if a6_frac is not None and spread.get("min") is not None and spread.get("max") is not None:
            lo = float(spread["min"])
            hi = float(spread["max"])
            mid = spread.get("median")
            if hi - lo <= 1e-12:
                notes.append(
                    f"{key}: Aug 6 is the only informative day or all days match "
                    f"({a6_frac:.4f})."
                )
            elif float(a6_frac) >= hi - 1e-12:
                notes.append(
                    f"{key}: Aug 6 sits at the discovery maximum "
                    f"({a6_frac:.4f}); it is an active-regime day, not a quiet one."
                )
            elif float(a6_frac) <= lo + 1e-12:
                notes.append(
                    f"{key}: Aug 6 sits at the discovery minimum "
                    f"({a6_frac:.4f}); v1 may have understated typical activity."
                )
            elif mid is not None and abs(float(a6_frac) - float(mid)) <= 0.25 * (hi - lo):
                notes.append(
                    f"{key}: Aug 6 ({a6_frac:.4f}) is near the cross-day median "
                    f"({float(mid):.4f}); not an obvious anomaly."
                )
            else:
                notes.append(
                    f"{key}: Aug 6 ({a6_frac:.4f}) is off-median vs "
                    f"min={lo:.4f} median={mid} max={hi:.4f}."
                )
        v1_h24 = (v1_cells.get(key) or {}).get("nonoverlap_hits_per_24_usable_hours")
        exp_h24 = (expanded_cells.get(key) or {}).get("nonoverlap_hits_per_24_usable_hours")
        if (
            v1_h24 is not None
            and exp_h24 is not None
            and v1_frac is not None
            and exp_frac is not None
        ):
            frac_rel = abs(float(exp_frac) - float(v1_frac)) / max(abs(float(v1_frac)), 1e-9)
            rate_rel = abs(float(exp_h24) - float(v1_h24)) / max(abs(float(v1_h24)), 1e-9)
            if frac_rel <= 0.25 and rate_rel > 0.5:
                notes.append(
                    f"{key}: hits/24h moved more than the hit fraction; the v1 "
                    "frequency extrapolation was sensitive to the 11.64h window length."
                )
    if v1_hours is not None:
        notes.append(
            f"v1 discovery was {v1_hours:.2f}h on Aug 6 only; expanded Aug-6 subset "
            f"is {aug6_hours:.2f}h and expanded discovery is {expanded_hours:.2f}h. "
            "Larger n can change rare-TP counts even when the regime is similar."
        )
    if aug6_day:
        notes.append(
            "Aug 6 raw stability row is in day_block_stability.per_utc_day "
            "(marked in the Markdown table)."
        )
    narrative = (
        "Compare published v1 (Aug 6, 11.64h, OOS preserved) with the fuller Aug-6 "
        "restore and with all expanded-discovery days. Stable cells are those whose "
        "hit fraction stays near v1 and near the cross-day median. Sample artifacts "
        "are cells where v1's small n or a single-day regime drove hits/24h or a "
        "rare-TP percent. Grids stay frozen; none of this is a trading rule."
    )
    return narrative, notes


def main() -> None:
    started = time.perf_counter()
    tracemalloc.start()
    rss_start = _peak_rss_mib()
    blockers: list[str] = []

    b2 = _live_b2_inventory()
    if b2.get("status") != "LIVE_COMPLETED_MARKERS":
        blockers.append(
            "LIVE_B2_INVENTORY_UNAVAILABLE: "
            f"{b2.get('status')} ({b2.get('error')})."
        )
        raise SystemExit("live B2 COMPLETED inventory is required for this milestone")

    windows = list(b2.get("windows") or [])
    frozen = freeze_full_corpus_expansion(windows)
    coverage = frozen["inventory_utc_day_coverage"]

    restore_root = ROOT / "restored_events"
    runs_root = ROOT / "runs" / "first_passage_full_corpus"
    print(
        json.dumps(
            {
                "phase": "freeze",
                "discovery_window_count": frozen["discovery_window_count"],
                "discovery_dates": frozen["discovery_utc_dates"],
                "holdout_dates": frozen["new_holdout_utc_dates"],
                "holdout_applied": frozen["new_holdout_applied"],
                "lead_alerts": frozen["lead_alerts"],
            },
            sort_keys=True,
        ),
        flush=True,
    )

    store = _open_b2_store()
    materialize = materialize_discovery_market_state(
        store,
        frozen["discovery_windows"],
        restore_root=restore_root,
        runs_root=runs_root,
    )
    if materialize.get("errors"):
        blockers.append(
            "MATERIALIZE_ERRORS_"
            + ",".join(
                f"{item.get('dataset_id') or item.get('utc_date')}:{item.get('error_type')}"
                for item in materialize["errors"]
            )
        )
    paths = list(materialize.get("market_state_paths") or [])
    if not paths:
        raise SystemExit("no discovery market_state_1s materialized")

    times, bid, ask, mid = load_executable_series_from_parquet(paths)
    gaps = load_collection_gaps()
    epoch, f_bid, f_ask, f_mid, dropped = filter_known_gap_rows(
        times, bid, ask, mid, gaps
    )
    if dropped:
        blockers.append(
            f"DROPPED_{dropped}_ROWS_IN_DOCUMENTED_COLLECTION_GAPS_OR_INVALID_TOB"
        )
    times_f = [datetime.fromtimestamp(ts, tz=UTC) for ts in epoch]
    spreads = [
        (a - b) / m * 10_000.0
        for b, a, m in zip(f_bid, f_ask, f_mid, strict=True)
        if m > 0
    ]
    taker_rt, median_spread = _taker_rt_bps(spreads)
    usable_hours = len(epoch) / 3600.0

    print(
        json.dumps(
            {
                "phase": "scan",
                "discovery_rows": len(epoch),
                "usable_hours": usable_hours,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    stats = analyze_first_passage(
        epoch,
        f_bid,
        f_ask,
        f_mid,
        horizons=FIRST_PASSAGE_HORIZONS_SECONDS,
        thresholds=FIRST_PASSAGE_THRESHOLDS_BPS,
        usable_hours=usable_hours,
    )

    aug6_t, aug6_b, aug6_a, aug6_m = slice_series_by_time(
        times_f, f_bid, f_ask, f_mid, start=AUG6_START, end=AUG6_END
    )
    aug6_epoch = [int(ts.timestamp()) for ts in aug6_t]
    aug6_hours = len(aug6_epoch) / 3600.0
    aug6_stats: dict[str, Any] = {}
    if aug6_epoch:
        aug6_stats = analyze_first_passage(
            aug6_epoch,
            aug6_b,
            aug6_a,
            aug6_m,
            horizons=FIRST_PASSAGE_HORIZONS_SECONDS,
            thresholds=FIRST_PASSAGE_THRESHOLDS_BPS,
            usable_hours=aug6_hours,
        )
    else:
        blockers.append("EXPANDED_AUG6_SUBSET_EMPTY")

    print(
        json.dumps(
            {
                "phase": "day_stability",
                "days": sorted({t.date().isoformat() for t in times_f}),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    stability = day_block_stability(times_f, f_bid, f_ask, f_mid)

    cost = cost_overlay(taker_rt_bps=taker_rt, median_spread_bps=median_spread)
    comments = commentary_cells(stats, taker_rt_bps=TAKER_RT_BPS_REFERENCE)
    v1 = _v1_snapshot()
    expanded_cells = extract_exec_nonoverlap_snapshot(stats)
    aug6_cells = extract_exec_nonoverlap_snapshot(aug6_stats) if aug6_stats else {}
    v1_cells = v1.get("cells") or {}
    narrative, findings = _findings(
        v1_cells=v1_cells,
        aug6_cells=aug6_cells,
        expanded_cells=expanded_cells,
        stability=stability,
        v1_hours=v1.get("discovery_usable_hours"),
        expanded_hours=usable_hours,
        aug6_hours=aug6_hours,
    )

    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_end = _peak_rss_mib()

    inventory_public = {
        "status": b2.get("status"),
        "inventory_source": b2.get("inventory_source"),
        "window_count": b2.get("window_count"),
        "quarantined_count": b2.get("quarantined_count"),
        "quality_pass_count": b2.get("quality_pass_count"),
        "utc_dates": b2.get("utc_dates") or coverage.get("utc_dates"),
        "utc_day_coverage": coverage,
        "credential_filenames": b2.get("credential_filenames"),
        "mutations": False,
        "postgres_historical_scan": False,
    }
    split_public = {
        key: frozen[key]
        for key in frozen
        if key
        not in {
            "discovery_windows",
            "untouched_oos_windows",
            "new_holdout_windows",
            "inventory_utc_day_coverage",
        }
    }
    split_public["untouched_oos_dataset_ids"] = [
        row["dataset_id"] for row in frozen["untouched_oos_windows"]
    ]
    split_public["new_holdout_dataset_ids"] = [
        row["dataset_id"] for row in frozen["new_holdout_windows"]
    ]
    split_public["discovery_dataset_ids"] = [
        row["dataset_id"] for row in frozen["discovery_windows"]
    ]

    final: dict[str, Any] = {
        "STATUS": "ETH_FIRST_PASSAGE_FULL_CORPUS_EXPANSION_READY",
        "ML_STATUS": "NOT_STARTED",
        "DECISION": "STOP_FOR_LEAD_REVIEW",
        "protocol": "eth_first_passage_full_corpus_v1",
        "parent_protocol": stats.get("protocol"),
        "grids_frozen_from_v1": True,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "corpus": {
            "discovery_utc_dates": frozen["discovery_utc_dates"],
            "discovery_rows": len(epoch),
            "discovery_usable_hours": usable_hours,
            "discovery_usable_days": usable_hours / 24.0,
            "expanded_aug6_usable_hours": aug6_hours,
            "holdout_first_passage_materialized": False,
            "v1_oos_first_passage_materialized": False,
            "materialize_days": materialize.get("day_outputs"),
            "collection_gaps_applied": [gap.gap_id for gap in gaps],
            "known_gaps_not_bridged": True,
            "lead_alerts": frozen["lead_alerts"],
            "price_movement_inspected_before_split": False,
        },
        "corpus_split": split_public,
        "b2_inventory": inventory_public,
        "cost_layer": cost,
        "horizons_seconds": list(FIRST_PASSAGE_HORIZONS_SECONDS),
        "thresholds_bps": [int(t) for t in FIRST_PASSAGE_THRESHOLDS_BPS],
        "mid": stats["mid"],
        "executable_tob": stats["executable_tob"],
        "movement_episode_v1": stats["movement_episode_v1"],
        "day_block_stability": stability,
        "aug6_vs_expanded": {
            "v1_aug6": v1_cells,
            "v1_discovery_usable_hours": v1.get("discovery_usable_hours"),
            "expanded_aug6": aug6_cells,
            "expanded_aug6_usable_hours": aug6_hours,
            "expanded_discovery": expanded_cells,
            "expanded_discovery_usable_hours": usable_hours,
            "narrative": narrative,
            "findings": findings,
        },
        "previous_opportunity_definition": stats["previous_opportunity_definition"],
        "this_protocol": stats["this_protocol"],
        "executable_definition": stats["executable_definition"],
        "economic_frequency_commentary": comments,
        "n_contiguous_segments": stats["n_contiguous_segments"],
        "runtime": {
            "wall_seconds": round(time.perf_counter() - started, 3),
            "peak_rss_mib": rss_end,
            "rss_start_mib": rss_start,
            "tracemalloc_peak_mib": round(peak / (1024 * 1024), 3),
            "tracemalloc_current_mib": round(current / (1024 * 1024), 3),
        },
        "data_quality_blockers": blockers,
        "production": {
            "b2": "read-only",
            "no_pg_historical_scan": True,
            "no_ml": True,
            "no_paper_live": True,
            "no_tp_sl_grid": True,
        },
    }
    markdown = render_full_corpus_markdown(final)
    DOCS.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(final, indent=2, sort_keys=True, default=str)
    (DOCS / "eth_first_passage_full_corpus_v1.json").write_text(payload, encoding="utf-8")
    (DOCS / "eth_first_passage_full_corpus_v1.md").write_text(markdown, encoding="utf-8")
    report_dir = ROOT / "reports" / "first_passage"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "ETH_FIRST_PASSAGE_FULL_CORPUS_v1.json").write_text(
        payload, encoding="utf-8"
    )
    (report_dir / "ETH_FIRST_PASSAGE_FULL_CORPUS_v1.md").write_text(
        markdown, encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": final["STATUS"],
                "decision": final["DECISION"],
                "discovery_hours": usable_hours,
                "aug6_hours": aug6_hours,
                "holdout_dates": frozen["new_holdout_utc_dates"],
                "blockers": blockers,
                "wall_seconds": final["runtime"]["wall_seconds"],
                "docs": str(DOCS / "eth_first_passage_full_corpus_v1.md"),
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
