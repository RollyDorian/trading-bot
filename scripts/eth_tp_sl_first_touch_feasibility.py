"""ETH_TP_SL_FIRST_TOUCH_FEASIBILITY_V1 runner.

Uses the accepted full-corpus v1 discovery dates and local market_state_1s.
Does not inspect untouched OOS dates. No ML, PAPER, or live trading.
"""

from __future__ import annotations

import json
import os
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trading_bot.research.collection_gaps import load_collection_gaps
from trading_bot.research.pipeline.first_passage_corpus import V1_UNTOUCHED_OOS_UTC_DATES
from trading_bot.research.pipeline.tp_sl_first_touch import (
    ALL_HORIZONS_SECONDS,
    CURRENT_MODELED_LATENCY_BPS_PER_SIDE,
    EXECUTION_DELAYS_SECONDS,
    LATENCY_BPS_PER_SIDE_GRID,
    SL_THRESHOLDS_BPS,
    TP_SL_PROTOCOL_NAME,
    TP_THRESHOLDS_BPS,
    analyze_tp_sl_first_touch,
    assess_primary_contamination,
    audit_cost_decomposition,
    discovery_dates_from_full_corpus_doc,
    filter_tp_sl_series,
    index_restored_raw_windows,
    load_tp_sl_series_from_parquet,
    render_tp_sl_markdown,
    scan_forensic_excursions,
    spot_check_restored_raw_events,
)

ROOT = Path("data/research/full_corpus/runs/first_passage_full_corpus")
RESTORE_ROOT = Path("data/research/full_corpus/restored_events")
FULL_CORPUS_DOC = Path("docs/eth_first_passage_full_corpus_v1.json")
DOCS = Path("docs")
MD_PATH = DOCS / "eth_tp_sl_first_touch_feasibility_v1.md"
JSON_PATH = DOCS / "eth_tp_sl_first_touch_feasibility_v1.json"


def _peak_rss_mib() -> float | None:
    try:
        import psutil  # type: ignore[import-untyped]

        return float(psutil.Process().memory_info().rss / (1024 * 1024))
    except Exception:
        return None


def _discovery_market_state_paths(dates: list[str]) -> list[Path]:
    banned = set(V1_UNTOUCHED_OOS_UTC_DATES)
    paths: list[Path] = []
    for day in dates:
        if day in banned:
            raise SystemExit(f"refusing to load untouched OOS date {day}")
        path = ROOT / f"utc_{day}" / "market_state_1s" / "market_state_1s.parquet"
        if not path.is_file():
            raise SystemExit(f"missing discovery market_state_1s: {path}")
        paths.append(path)
    return paths


def _spot_check_normalized(utc_date: str, epoch_s: int) -> dict[str, Any]:
    """Read public quotes/marks ±2s around a spike. No secrets."""

    folder = ROOT / f"utc_{utc_date}" / "normalized_events"
    target = datetime.fromtimestamp(int(epoch_s), UTC)
    window_lo = target - timedelta(seconds=2)
    window_hi = target + timedelta(seconds=2)
    notes: dict[str, Any] = {
        "utc_date": utc_date,
        "target_utc": target.isoformat(),
        "quote_rows": 0,
        "mark_rows": 0,
        "status": "OK",
    }
    if not folder.is_dir():
        notes["status"] = "NO_NORMALIZED_EVENTS"
        return notes

    def _nearby(path: Path, time_key: str = "available_at") -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        handle = pq.ParquetFile(path)
        keep = {
            "available_at",
            "bid_price",
            "ask_price",
            "price",
            "data_quality",
        }
        wanted = [name for name in handle.schema_arrow.names if name in keep]
        found: list[dict[str, Any]] = []
        for batch in handle.iter_batches(batch_size=8_000, columns=wanted or None):
            for row in batch.to_pylist():
                raw = row.get(time_key)
                if raw is None:
                    continue
                if isinstance(raw, datetime):
                    ts = raw
                else:
                    ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                ts = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
                if window_lo <= ts <= window_hi:
                    found.append(row)
                    if len(found) >= 12:
                        return found
        return found

    quotes = _nearby(folder / "ask_bid_price.parquet")
    marks = _nearby(folder / "mark_price.parquet")
    notes["quote_rows"] = len(quotes)
    notes["mark_rows"] = len(marks)
    if quotes:
        q0 = quotes[0]
        notes["sample_quote"] = {
            "bid_price": q0.get("bid_price"),
            "ask_price": q0.get("ask_price"),
            "data_quality": q0.get("data_quality"),
        }
    if marks:
        notes["sample_mark"] = {
            "price": marks[0].get("price"),
            "data_quality": marks[0].get("data_quality"),
        }
    if not quotes and not marks:
        notes["status"] = "NO_EVENTS_IN_PM2S_WINDOW"
    return notes


def _raw_event_checks(forensic: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    restore_index = index_restored_raw_windows(RESTORE_ROOT)
    cases = list(forensic.get("largest_excursions") or [])[:8]
    cases += list(forensic.get("mae_tails") or [])[:4]
    seen: set[int] = set()
    for case in cases:
        epoch = int(case.get("start_epoch_s") or 0)
        if epoch in seen or epoch <= 0:
            continue
        seen.add(epoch)
        start_utc = str(case.get("start_utc") or "")
        day = start_utc[:10]
        raw = spot_check_restored_raw_events(
            epoch_s=epoch, restore_index=restore_index
        )
        norm = _spot_check_normalized(day, epoch)
        tags = ",".join(case.get("quality_tags") or [])
        lines.append(
            f"{start_utc} tags={tags} raw={raw.get('status')} "
            f"raw_events={raw.get('n_events')} types={raw.get('event_type_counts')} "
            f"max_abs_latency_ms={raw.get('max_abs_latency_ms')} "
            f"raw_quote={raw.get('sample_public_quote')} "
            f"raw_mark={raw.get('sample_public_mark')} "
            f"normalized={norm.get('status')} "
            f"norm_quotes={norm.get('quote_rows')} norm_marks={norm.get('mark_rows')}"
        )
    if not restore_index:
        lines.insert(0, "restored RAW index empty; market_state quality tags still apply")
    return lines


def main() -> None:
    started = time.perf_counter()
    tracemalloc.start()
    rss0 = _peak_rss_mib()
    dates = discovery_dates_from_full_corpus_doc(FULL_CORPUS_DOC)
    print(
        json.dumps(
            {
                "phase": "freeze",
                "protocol": TP_SL_PROTOCOL_NAME,
                "discovery_dates": dates,
                "oos_untouched": list(V1_UNTOUCHED_OOS_UTC_DATES),
                "horizons": list(ALL_HORIZONS_SECONDS),
                "tp_bps": list(TP_THRESHOLDS_BPS),
                "sl_bps": list(SL_THRESHOLDS_BPS),
                "delays_seconds": list(EXECUTION_DELAYS_SECONDS),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    paths = _discovery_market_state_paths(dates)
    series = load_tp_sl_series_from_parquet(paths)
    gaps = load_collection_gaps()
    filtered, dropped = filter_tp_sl_series(series, gaps)
    print(
        json.dumps(
            {
                "phase": "loaded",
                "rows": len(filtered["epoch_s"]),
                "dropped_gap_or_invalid": dropped,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    spreads = [
        float(value)
        for value in filtered["spread_bps"]
        if value is not None
    ]
    median_spread = None
    if spreads:
        ordered = sorted(spreads)
        median_spread = ordered[(len(ordered) - 1) // 2]
    cost = audit_cost_decomposition(
        median_spread_bps=median_spread,
        holding_seconds=300.0,
        latency_bps_per_side=CURRENT_MODELED_LATENCY_BPS_PER_SIDE,
    )
    print(json.dumps({"phase": "scan_start"}, sort_keys=True), flush=True)
    stats = analyze_tp_sl_first_touch(
        filtered["epoch_s"],
        filtered["bid"],
        filtered["ask"],
        filtered["mid"],
        horizons=ALL_HORIZONS_SECONDS,
        tp_grid=TP_THRESHOLDS_BPS,
        sl_grid=SL_THRESHOLDS_BPS,
        delays_seconds=EXECUTION_DELAYS_SECONDS,
        rolling_delays=(0,),
        latency_bps_per_side_grid=LATENCY_BPS_PER_SIDE_GRID,
        median_spread_bps=median_spread,
        valid_book=filtered["valid_book"],
        book_age_seconds=filtered["book_age_seconds"],
    )
    print(json.dumps({"phase": "forensic"}, sort_keys=True), flush=True)
    forensic = scan_forensic_excursions(
        filtered["epoch_s"],
        filtered["bid"],
        filtered["ask"],
        filtered["mid"],
        valid_book=filtered["valid_book"],
        book_age_seconds=filtered["book_age_seconds"],
        quote_age_seconds=filtered["quote_age_seconds"],
        mark_price=filtered["mark_price"],
    )
    contamination = assess_primary_contamination(stats=stats, forensic=forensic)
    forensic["contamination"] = contamination
    forensic["raw_event_checks"] = _raw_event_checks(forensic)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    status = (
        "ETH_TP_SL_FIRST_TOUCH_FEASIBILITY_BLOCKED_BAD_DATA"
        if contamination.get("escalate")
        else "ETH_TP_SL_FIRST_TOUCH_FEASIBILITY_READY"
    )
    payload: dict[str, Any] = {
        **stats,
        "STATUS": status,
        "ML_STATUS": "NOT_STARTED",
        "DECISION": "STOP_FOR_LEAD_REVIEW",
        "cost_audit": cost,
        "forensic_qa": forensic,
        "corpus": {
            "discovery_utc_dates": dates,
            "discovery_usable_hours": stats.get("usable_hours"),
            "discovery_rows": stats.get("n_rows"),
            "dropped_gap_or_invalid": dropped,
            "oos_untouched": list(V1_UNTOUCHED_OOS_UTC_DATES),
            "price_path_on_oos": False,
        },
        "runtime": {
            "wall_seconds": time.perf_counter() - started,
            "tracemalloc_peak_mib": peak / (1024 * 1024),
            "rss_start_mib": rss0,
            "rss_end_mib": _peak_rss_mib(),
        },
        "blockers": (
            [f"DROPPED_{dropped}_ROWS_IN_DOCUMENTED_COLLECTION_GAPS_OR_INVALID_TOB"]
            if dropped
            else []
        )
        + list(contamination.get("alerts") or []),
    }
    MD_PATH.write_text(render_tp_sl_markdown(payload), encoding="utf-8")
    JSON_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "phase": "done",
                "status": status,
                "decision": "STOP_FOR_LEAD_REVIEW",
                "contamination": contamination.get("contamination_status"),
                "docs": str(MD_PATH),
                "wall_seconds": payload["runtime"]["wall_seconds"],
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    main()
