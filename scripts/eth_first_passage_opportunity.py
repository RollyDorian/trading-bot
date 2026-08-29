"""ETH_FIRST_PASSAGE_OPPORTUNITY_REASSESSMENT_V1 runner.

Read-only vs B2. No production PostgreSQL historical scan. No ML. No trading.
Grids are frozen in first_passage_opportunity.py; this script must not retune them.
"""

from __future__ import annotations

import json
import os
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_bot.research.collection_gaps import load_collection_gaps
from trading_bot.research.pipeline.cost_evidence import (
    funding_contribution_bps,
    hibachi_public_fee_schedule,
    round_trip_friction_bps,
)
from trading_bot.research.pipeline.first_passage_corpus import (
    freeze_discovery_oos,
    freeze_series_oos,
    list_verified_eth_completed,
    load_optional_dotenv,
)
from trading_bot.research.pipeline.first_passage_opportunity import (
    FIRST_PASSAGE_HORIZONS_SECONDS,
    FIRST_PASSAGE_PROTOCOL_NAME,
    FIRST_PASSAGE_THRESHOLDS_BPS,
    OOS_RESERVED_UTC_DAYS,
    TAKER_RT_BPS_REFERENCE,
    _percentile,
    analyze_first_passage,
    commentary_cells,
    cost_overlay,
    filter_known_gap_rows,
    load_executable_series_from_parquet,
    render_first_passage_markdown,
    slice_series_by_time,
)

ROOT = Path("data/research/full_corpus")
DOCS = Path("docs")
LOCAL_MARKET_STATE = (
    ROOT / "runs" / "prior_continuous" / "market_state_1s" / "market_state_1s.parquet",
    ROOT / "runs" / "generation_g_7471913" / "market_state_1s" / "market_state_1s.parquet",
)


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

        # argtypes required on 64-bit Windows or the pointer is truncated.
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


def _try_live_b2_inventory() -> dict[str, Any]:
    """List COMPLETED ETH archives. Never writes to B2."""

    for candidate in (Path(".env"), Path.home() / ".env"):
        load_optional_dotenv(candidate)
    try:
        from trading_bot.archive.b2 import B2ArchiveConfig
        from trading_bot.archive.store import S3ArchiveStore
    except Exception as error:  # pragma: no cover - import/env failures
        return {"status": "UNAVAILABLE", "error": type(error).__name__, "windows": []}
    try:
        store = S3ArchiveStore.for_b2(B2ArchiveConfig.from_environ())
        windows = list_verified_eth_completed(store)
        return {
            "status": "LIVE_COMPLETED_MARKERS",
            "window_count": len(windows),
            "windows": windows,
            "mutations": False,
        }
    except Exception as error:
        return {
            "status": "LIVE_LISTING_FAILED",
            "error": type(error).__name__,
            "windows": [],
            "note": (
                "B2 credentials were not available in this process. "
                "Do not substitute stale docs counts as live inventory."
            ),
        }


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


def main() -> None:
    started = time.perf_counter()
    tracemalloc.start()
    rss_start = _peak_rss_mib()
    blockers: list[str] = []

    b2 = _try_live_b2_inventory()
    if b2.get("status") != "LIVE_COMPLETED_MARKERS":
        blockers.append(
            "LIVE_B2_INVENTORY_UNAVAILABLE: "
            f"{b2.get('status')} ({b2.get('error')}). "
            "Analysis uses locally restored verified market_state_1s from the "
            "existing offline pipeline, not a fresh B2 listing."
        )

    windows = list(b2.get("windows") or [])
    if windows:
        frozen_windows = freeze_discovery_oos(windows, oos_utc_days=OOS_RESERVED_UTC_DAYS)
    else:
        frozen_windows = None

    existing = [path for path in LOCAL_MARKET_STATE if path.is_file()]
    if not existing:
        raise SystemExit("no local market_state_1s parquet found under data/research")

    times, bid, ask, mid = load_executable_series_from_parquet(existing)
    # Freeze from timestamps only, before excursion stats.
    frozen_series = freeze_series_oos(times, oos_utc_days=OOS_RESERVED_UTC_DAYS)
    oos_start = frozen_series.get("oos_start_utc")
    oos_start_dt = datetime.fromisoformat(str(oos_start)) if oos_start else None
    disc_t, disc_b, disc_a, disc_m = slice_series_by_time(
        times, bid, ask, mid, start=None, end=oos_start_dt
    )
    gaps = load_collection_gaps()
    epoch, f_bid, f_ask, f_mid, dropped = filter_known_gap_rows(
        disc_t, disc_b, disc_a, disc_m, gaps
    )
    if dropped:
        blockers.append(
            f"DROPPED_{dropped}_ROWS_IN_DOCUMENTED_COLLECTION_GAPS_OR_INVALID_TOB"
        )

    spreads = [
        (a - b) / m * 10_000.0
        for b, a, m in zip(f_bid, f_ask, f_mid, strict=True)
        if m > 0
    ]
    taker_rt, median_spread = _taker_rt_bps(spreads)
    usable_hours = len(epoch) / 3600.0

    stats = analyze_first_passage(
        epoch,
        f_bid,
        f_ask,
        f_mid,
        horizons=FIRST_PASSAGE_HORIZONS_SECONDS,
        thresholds=FIRST_PASSAGE_THRESHOLDS_BPS,
        usable_hours=usable_hours,
    )
    cost = cost_overlay(taker_rt_bps=taker_rt, median_spread_bps=median_spread)
    comments = commentary_cells(stats, taker_rt_bps=TAKER_RT_BPS_REFERENCE)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_end = _peak_rss_mib()

    corpus = {
        "inventory_source": b2.get("status"),
        "live_b2_window_count": b2.get("window_count"),
        "local_market_state_paths": [str(path) for path in existing],
        "materialization": (
            "Reused existing offline pipeline outputs "
            "(verified RAW events.parquet → normalize_events_parquet → "
            "build_market_state_1s). No second parser."
        ),
        "discovery_utc_dates": frozen_series.get("discovery_utc_dates"),
        "oos_utc_dates": frozen_series.get("oos_utc_dates"),
        "discovery_window_count": (
            frozen_windows["discovery_window_count"] if frozen_windows else None
        ),
        "oos_window_count": frozen_windows["oos_window_count"] if frozen_windows else None,
        "discovery_rows": len(epoch),
        "discovery_usable_hours": usable_hours,
        "discovery_usable_days": usable_hours / 24.0,
        "oos_rows_untouched": frozen_series.get("oos_row_count"),
        "oos_usable_hours_untouched": frozen_series.get("oos_usable_hours"),
        "lead_alerts": list(frozen_series.get("lead_alerts") or [])
        + list((frozen_windows or {}).get("lead_alerts") or []),
        "price_movement_inspected_before_split": False,
        "g_7471913_note": (
            "Previously inspected for a different endpoint-return question; "
            "held in OOS here if it falls in the last reserved UTC dates."
        ),
        "collection_gaps_applied": [gap.gap_id for gap in gaps],
        "known_gaps_not_bridged": True,
    }
    final: dict[str, Any] = {
        "STATUS": "ETH_FIRST_PASSAGE_OPPORTUNITY_REASSESSMENT_READY",
        "ML_STATUS": "NOT_STARTED",
        "DECISION": "STOP_FOR_LEAD_REVIEW",
        "protocol": FIRST_PASSAGE_PROTOCOL_NAME,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "corpus": corpus,
        "b2_inventory": {
            "status": b2.get("status"),
            "error": b2.get("error"),
            "window_count": b2.get("window_count"),
            "mutations": False,
            "postgres_historical_scan": False,
        },
        "cost_layer": cost,
        "horizons_seconds": list(FIRST_PASSAGE_HORIZONS_SECONDS),
        "thresholds_bps": [int(t) for t in FIRST_PASSAGE_THRESHOLDS_BPS],
        "mid": stats["mid"],
        "executable_tob": stats["executable_tob"],
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
        },
    }
    markdown = render_first_passage_markdown(final)
    DOCS.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(final, indent=2, sort_keys=True, default=str)
    (DOCS / "eth_first_passage_opportunity_v1.json").write_text(payload, encoding="utf-8")
    (DOCS / "eth_first_passage_opportunity_v1.md").write_text(markdown, encoding="utf-8")
    report_dir = ROOT / "reports" / "first_passage"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "ETH_FIRST_PASSAGE_OPPORTUNITY_v1.json").write_text(
        payload, encoding="utf-8"
    )
    (report_dir / "ETH_FIRST_PASSAGE_OPPORTUNITY_v1.md").write_text(
        markdown, encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": final["STATUS"],
                "discovery_hours": usable_hours,
                "oos_dates": frozen_series.get("oos_utc_dates"),
                "blockers": blockers,
                "wall_seconds": final["runtime"]["wall_seconds"],
                "docs": str(DOCS / "eth_first_passage_opportunity_v1.md"),
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
