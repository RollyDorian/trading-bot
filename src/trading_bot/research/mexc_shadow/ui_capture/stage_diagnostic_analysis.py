"""Analyze one extension 1.3.4 stage-diagnostic visibility experiment.

Does not change the capture implementation or protocol v2. Does not inspect
mom/gap, PnL, ML, PAPER, or LIVE. Phase boundaries come only from recorded
visibilitychange / lifecycle diagnostics, never from the intended 2/6/4 plan.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from trading_bot.research.mexc_shadow.ui_capture.durable import is_session_record
from trading_bot.research.mexc_shadow.ui_capture.stage_diagnostics import (
    DIAGNOSTIC_FORMAT_VERSION,
    EXTENSION_VERSION,
    expected_interval_opportunities,
    is_stage_diagnostic_record,
)
from trading_bot.research.mexc_shadow.ui_capture.store import iter_all_mappings

MILESTONE = "MEXC_UI_CAPTURE_STAGE_DIAGNOSTIC_ANALYSIS_V1"
INTERVAL_MS = 500
GAP_1000_MS = 1000.0
GAP_2000_MS = 2000.0
OPERATOR_CHROME = "152.0.7977.82"
DEFAULT_CAPTURE = (
    Path("data")
    / "mexc_ui_capture"
    / "mexc_ui_capture_f73988c1-590c-4a92-a98c-2890b0749061_2026-09-07T16-15-40-639Z.ndjson"
)
SCORED_CAPTURE_SHA256 = "c3699bb2261b153dda093e36e801e9f352ffb02891345c7aea1b1ff1ba6c81c2"

PHASES = ("initial_visible", "hidden", "final_visible")
APPEND_SUBSTAGE_KEYS = (
    "meta_lookup_ms",
    "db_open_ms",
    "session_read_ms",
    "chunk_read_ms",
    "stringify_put_ms",
    "tx_wait_ms",
    "close_ms",
)

ROOT_TIMER = "TIMER_RENDERER_SCHEDULING_DOMINANT"
ROOT_FIFO = "CONTENT_FIFO_BACKPRESSURE_DOMINANT"
ROOT_STORAGE = "STORAGE_BACKPRESSURE_DOMINANT"
ROOT_EXTRACT = "DOM_EXTRACTION_DOMINANT"
ROOT_IPC = "SERVICE_WORKER_IPC_DOMINANT"
ROOT_MIXED = "MIXED_CAUSE"
ROOT_INCONCLUSIVE = "DIAGNOSTICS_INCONCLUSIVE"
# Phase-only: cadence matches the 500 ms grid and queues stay small. Not a tree root.
PHASE_NEAR_NOMINAL = "NEAR_NOMINAL"

SNAPSHOT_DIAG_KEYS = (
    "request_ordinal",
    "interval_callback_ordinal",
    "elapsed_ideal_slot_ordinal",
    "expected_deadline_mono",
    "callback_mono",
    "callback_delay_ms",
    "content_enqueue_mono",
    "content_queue_wait_ms",
    "content_queue_depth",
    "extract_start_mono",
    "extract_end_mono",
    "extract_duration_ms",
    "send_start_mono",
    "background_receive_mono",
    "background_queue_wait_ms",
    "background_queue_depth",
    "append_start_mono",
    "append_duration_ms",
    "total_ack_latency_ms",
    "ack_end_mono",
    "append_end_mono",
    "visibility_state",
    "visibility_state_at_extract",
    "producer_epoch",
    "session_generation",
    "stale_generation",
    "session_id_mismatch",
    "worker_boot_id",
    "payload_bytes",
    "append_timings",
)


class ShaMismatchError(ValueError):
    """Export bytes do not match the locked diagnostic-capture digest."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _percentile(sorted_vals: list[float], fraction: float) -> float | None:
    """Match quality.py / cadence_forensics rank estimator."""

    if not sorted_vals:
        return None
    index = min(len(sorted_vals) - 1, int(len(sorted_vals) * fraction))
    return sorted_vals[index]


def _stats(values: Sequence[float | None]) -> dict[str, float | int | None]:
    ordered = sorted(float(item) for item in values if item is not None)
    if not ordered:
        return {
            "n": 0,
            "min": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "max": None,
            "mean": None,
        }
    return {
        "n": len(ordered),
        "min": ordered[0],
        "p50": _percentile(ordered, 0.50),
        "p90": _percentile(ordered, 0.90),
        "p95": _percentile(ordered, 0.95),
        "p99": _percentile(ordered, 0.99),
        "max": ordered[-1],
        "mean": sum(ordered) / len(ordered),
    }


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number == number and abs(number) != float("inf"):
            return number
    return None


def _merge_by_ordinal(items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Last non-null field wins. Completions may emit append_end then ack_end separately."""

    merged: dict[int, dict[str, Any]] = {}
    for item in items:
        ordinal = item.get("request_ordinal")
        if ordinal is None:
            continue
        bucket = merged.setdefault(int(ordinal), {})
        for key, value in item.items():
            if value is not None:
                bucket[key] = value
    return merged


def _timings_sum(timings: Any) -> float | None:
    if not isinstance(timings, dict):
        return None
    present = [_finite(timings.get(key)) for key in APPEND_SUBSTAGE_KEYS]
    values = [item for item in present if item is not None]
    return sum(values) if values else None


def histogram_stats(hist: dict[str, Any] | None) -> dict[str, Any]:
    """Percentiles from cumulative histogram buckets (upper bound of the rank bucket)."""

    empty = {**_stats([]), "percentile_method": "histogram_bucket_upper"}
    if not hist:
        return empty
    count = int(hist.get("n") or 0)
    if count <= 0:
        return empty
    bounds = [float(item) for item in (hist.get("bounds_ms") or [])]
    counts = [int(item) for item in (hist.get("counts") or [])]
    max_ms = _finite(hist.get("max_ms"))
    min_ms = _finite(hist.get("min_ms"))
    sum_ms = _finite(hist.get("sum_ms")) or 0.0

    def rank_upper(fraction: float) -> float | None:
        index = min(count - 1, int(count * fraction))
        running = 0
        for bucket_index, bucket_count in enumerate(counts):
            running += bucket_count
            if running > index:
                if bucket_index < len(bounds):
                    return bounds[bucket_index]
                return max_ms
        return max_ms

    return {
        "n": count,
        "min": min_ms,
        "p50": rank_upper(0.50),
        "p90": rank_upper(0.90),
        "p95": rank_upper(0.95),
        "p99": rank_upper(0.99),
        "max": max_ms,
        "mean": (sum_ms / count) if count else None,
        "percentile_method": "histogram_bucket_upper",
    }


def _wall_ms(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() * 1000.0


def reconstruct_phases(
    lifecycle: list[dict[str, Any]],
    *,
    session_stop_mono: float | None = None,
) -> dict[str, Any]:
    """Build visible→hidden→visible phases from lifecycle only.

    Intended 2/6/4 minute marks are never used as boundaries.
    """

    timer = next((event for event in lifecycle if event.get("kind") == "timer_registered"), None)
    vis_events = [event for event in lifecycle if event.get("kind") == "visibilitychange"]
    stop = next((event for event in lifecycle if event.get("kind") == "session_stop"), None)
    freeze = [event.get("kind") for event in lifecycle]
    hidden_at = next(
        (
            event
            for event in vis_events
            if event.get("from_state") == "visible" and event.get("to_state") == "hidden"
        ),
        None,
    )
    shown_at = next(
        (
            event
            for event in vis_events
            if event.get("from_state") == "hidden" and event.get("to_state") == "visible"
        ),
        None,
    )
    inconclusive_reasons: list[str] = []
    if timer is None or _finite(timer.get("mono")) is None:
        inconclusive_reasons.append("missing_timer_registered")
    if hidden_at is None or _finite(hidden_at.get("mono")) is None:
        inconclusive_reasons.append("missing_visible_to_hidden")
    if shown_at is None or _finite(shown_at.get("mono")) is None:
        inconclusive_reasons.append("missing_hidden_to_visible")
    extra_vis = [
        event
        for event in vis_events
        if event is not hidden_at and event is not shown_at
    ]
    if extra_vis:
        inconclusive_reasons.append("extra_visibilitychange_events")
    if hidden_at and shown_at:
        hidden_mono = _finite(hidden_at.get("mono"))
        shown_mono = _finite(shown_at.get("mono"))
        if hidden_mono is not None and shown_mono is not None and shown_mono <= hidden_mono:
            inconclusive_reasons.append("hidden_visible_order_invalid")
    stop_mono = _finite(stop.get("mono") if stop else None)
    if stop_mono is None:
        stop_mono = session_stop_mono
    if stop_mono is None:
        inconclusive_reasons.append("missing_session_stop")
    if stop and stop.get("to_state") not in {None, "visible"}:
        inconclusive_reasons.append("session_stop_not_visible")
    if inconclusive_reasons:
        return {
            "reconstructed": False,
            "status": ROOT_INCONCLUSIVE,
            "reasons": inconclusive_reasons,
            "lifecycle_kinds": freeze,
            "phases": None,
        }

    assert timer is not None and hidden_at is not None and shown_at is not None
    timer_mono = float(_finite(timer.get("mono")) or 0.0)
    hidden_mono = float(_finite(hidden_at.get("mono")) or 0.0)
    shown_mono = float(_finite(shown_at.get("mono")) or 0.0)
    assert stop_mono is not None
    phases = {
        "initial_visible": {
            "name": "initial_visible",
            "visibility": "visible",
            "start_mono": timer_mono,
            "end_mono": hidden_mono,
            "duration_ms": hidden_mono - timer_mono,
            "start_event": "timer_registered",
            "end_event": "visibilitychange:visible->hidden",
        },
        "hidden": {
            "name": "hidden",
            "visibility": "hidden",
            "start_mono": hidden_mono,
            "end_mono": shown_mono,
            "duration_ms": shown_mono - hidden_mono,
            "start_event": "visibilitychange:visible->hidden",
            "end_event": "visibilitychange:hidden->visible",
        },
        "final_visible": {
            "name": "final_visible",
            "visibility": "visible",
            "start_mono": shown_mono,
            "end_mono": float(stop_mono),
            "duration_ms": float(stop_mono) - shown_mono,
            "start_event": "visibilitychange:hidden->visible",
            "end_event": "session_stop",
        },
    }
    return {
        "reconstructed": True,
        "status": "ok",
        "reasons": [],
        "lifecycle_kinds": freeze,
        "timer_registered_mono": timer_mono,
        "session_stop_mono": float(stop_mono),
        "phases": phases,
    }


def assign_phase(callback_mono: float | None, phases: dict[str, dict[str, Any]]) -> str | None:
    if callback_mono is None:
        return None
    hidden = phases["hidden"]
    final = phases["final_visible"]
    if callback_mono < hidden["start_mono"]:
        return "initial_visible"
    if callback_mono < final["start_mono"]:
        return "hidden"
    return "final_visible"


def _interarrivals(values: list[float]) -> list[float]:
    ordered = list(values)
    return [ordered[index] - ordered[index - 1] for index in range(1, len(ordered))]


def _high_water(values: list[float | None]) -> int:
    present = [int(item) for item in values if item is not None]
    return max(present) if present else 0


def classify_gap(
    row: dict[str, Any],
    gap_ms: float,
    prev: dict[str, Any] | None,
    prev_interval: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attribute one raw-observation gap using this request's own stage timings.

    Percentiles from different stages are never summed. Timer evidence uses the
    previous *interval* callback, not a mixed-trigger neighbor.
    """

    wait = float(row.get("content_queue_wait_ms") or 0.0)
    extract = float(row.get("extract_duration_ms") or 0.0)
    bg_wait = float(row.get("background_queue_wait_ms") or 0.0)
    append = _finite(row.get("append_duration_ms"))
    if append is None:
        append = _timings_sum(row.get("append_timings"))
    append_ms = float(append or 0.0)
    ack = _finite(row.get("total_ack_latency_ms"))
    trigger = str(row.get("trigger") or "")
    causes: list[str] = []

    neighbor_callback_gap = None
    interval_callback_gap = None
    slot_jump = None
    if (
        prev is not None
        and row.get("callback_mono") is not None
        and prev.get("callback_mono") is not None
    ):
        neighbor_callback_gap = float(row["callback_mono"]) - float(prev["callback_mono"])
    if (
        trigger == "interval"
        and prev_interval is not None
        and row.get("callback_mono") is not None
        and prev_interval.get("callback_mono") is not None
    ):
        interval_callback_gap = float(row["callback_mono"]) - float(
            prev_interval["callback_mono"]
        )
    if (
        trigger == "interval"
        and prev_interval is not None
        and row.get("elapsed_ideal_slot_ordinal") is not None
        and prev_interval.get("elapsed_ideal_slot_ordinal") is not None
    ):
        slot_jump = int(row["elapsed_ideal_slot_ordinal"]) - int(
            prev_interval["elapsed_ideal_slot_ordinal"]
        )

    timer = False
    if trigger == "interval" and wait < 0.4 * gap_ms and (
        (slot_jump is not None and slot_jump > 1)
        or (interval_callback_gap is not None and interval_callback_gap >= 800)
        or (interval_callback_gap is None and gap_ms >= 800 and wait < 200 and extract < 250)
    ):
        timer = True
    if timer:
        causes.append("delayed_or_missing_timer_callback")
    if wait >= 500 or (gap_ms > 0 and wait >= 0.4 * gap_ms):
        causes.append("content_fifo_waiting")
    if extract >= 200 and extract >= 0.4 * gap_ms:
        causes.append("long_extraction")
    if bg_wait >= 20:
        causes.append("background_queue")
    if append_ms >= 50 and append_ms >= 0.3 * gap_ms:
        causes.append("indexeddb_append")
    if ack is not None:
        residual = ack - append_ms - bg_wait
        if residual >= 50 and residual >= 0.3 * gap_ms:
            causes.append("ipc_service_worker_delay")
    if not causes:
        causes.append("unknown")
    unique = list(dict.fromkeys(causes))
    primary = unique[0] if len(unique) == 1 else "overlapping_multiple_causes"
    return {
        "primary": primary,
        "causes": unique,
        "gap_ms": gap_ms,
        "content_queue_wait_ms": wait,
        "extract_duration_ms": extract,
        "background_queue_wait_ms": bg_wait,
        "append_duration_ms": append,
        "total_ack_latency_ms": ack,
        "neighbor_callback_gap_ms": neighbor_callback_gap,
        "interval_callback_gap_ms": interval_callback_gap,
        "elapsed_slot_jump": slot_jump,
        "trigger": trigger,
    }


def _phase_interval_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    interval = [row for row in rows if row.get("trigger") == "interval"]
    callbacks = [_finite(row.get("callback_mono")) for row in interval]
    callback_times = [item for item in callbacks if item is not None]
    ordinals = [
        int(row["interval_callback_ordinal"])
        for row in interval
        if row.get("interval_callback_ordinal") is not None
    ]
    slots = [
        int(row["elapsed_ideal_slot_ordinal"])
        for row in interval
        if row.get("elapsed_ideal_slot_ordinal") is not None
    ]
    return {
        "n_snapshots": len(rows),
        "n_interval": len(interval),
        "n_mutation": sum(1 for row in rows if row.get("trigger") == "mutation"),
        "n_manual": sum(1 for row in rows if row.get("trigger") == "manual"),
        "interval_callback_ordinal_min": min(ordinals) if ordinals else None,
        "interval_callback_ordinal_max": max(ordinals) if ordinals else None,
        "elapsed_ideal_slot_min": min(slots) if slots else None,
        "elapsed_ideal_slot_max": max(slots) if slots else None,
        "callback_delay_ms": _stats([_finite(row.get("callback_delay_ms")) for row in interval]),
        "callback_interarrival_ms": _stats(_interarrivals(callback_times)),
        "content_queue_wait_ms": _stats(
            [_finite(row.get("content_queue_wait_ms")) for row in rows]
        ),
        "content_queue_wait_interval_ms": _stats(
            [_finite(row.get("content_queue_wait_ms")) for row in interval]
        ),
        "content_queue_depth_high_water": _high_water(
            [_finite(row.get("content_queue_depth")) for row in rows]
        ),
        "extract_duration_ms": _stats([_finite(row.get("extract_duration_ms")) for row in rows]),
        "background_queue_wait_ms": _stats(
            [_finite(row.get("background_queue_wait_ms")) for row in rows]
        ),
        "background_queue_depth_high_water": _high_water(
            [_finite(row.get("background_queue_depth")) for row in rows]
        ),
        "append_duration_joined_ms": _stats(
            [_finite(row.get("append_duration_ms")) for row in rows]
        ),
        "append_timings_sum_ms": _stats([_timings_sum(row.get("append_timings")) for row in rows]),
        "total_ack_latency_joined_ms": _stats(
            [_finite(row.get("total_ack_latency_ms")) for row in rows]
        ),
        "payload_bytes": _stats([_finite(row.get("payload_bytes")) for row in rows]),
        "n_append_duration_joined": sum(
            1 for row in rows if _finite(row.get("append_duration_ms")) is not None
        ),
        "n_ack_joined": sum(
            1 for row in rows if _finite(row.get("total_ack_latency_ms")) is not None
        ),
        "append_substages": {
            key: _stats(
                [_finite((row.get("append_timings") or {}).get(key)) for row in rows]
            )
            for key in APPEND_SUBSTAGE_KEYS
        },
    }


def _classify_phase(stats: dict[str, Any]) -> str:
    """Map one reconstructed phase onto the milestone decision tree."""

    expected = float(stats.get("expected_heartbeat_opportunities") or 0)
    actual = float(stats["n_interval"])
    conservation = actual / expected if expected else None
    wait = stats["content_queue_wait_ms"]
    inter = stats["callback_interarrival_ms"]
    extract = stats["extract_duration_ms"]
    append = stats["append_duration_joined_ms"]
    timings = stats["append_timings_sum_ms"]
    ack = stats["total_ack_latency_joined_ms"]
    bg_wait = stats["background_queue_wait_ms"]
    wait_p95 = float(wait["p95"] or 0)
    extract_p95 = float(extract["p95"] or 0)
    append_max = float(append["max"] or timings["max"] or 0)
    ack_p95 = float(ack["p95"] or 0)
    bg_p95 = float(bg_wait["p95"] or 0)
    inter_p50 = inter["p50"]
    depth = int(stats["content_queue_depth_high_water"] or 0)

    storage = append_max >= 200 or (ack_p95 >= 400 and bg_p95 >= 50)
    extraction_dom = extract_p95 >= 400 and wait_p95 < 400
    ipc = ack_p95 >= 200 and append_max < 80 and bg_p95 < 20 and wait_p95 < 400
    fifo = depth >= 8 or wait_p95 >= 800
    timer = False
    if conservation is not None and conservation < 0.75:
        timer = True
    if inter_p50 is not None and float(inter_p50) >= 800 and wait_p95 < 400:
        timer = True

    votes: list[str] = []
    if timer:
        votes.append(ROOT_TIMER)
    # Hidden 1 s clamp can coexist with a tiny FIFO; scheduling still dominates.
    if fifo and not (timer and wait_p95 < 400):
        votes.append(ROOT_FIFO)
    if storage:
        votes.append(ROOT_STORAGE)
    if extraction_dom:
        votes.append(ROOT_EXTRACT)
    if ipc:
        votes.append(ROOT_IPC)
    unique = list(dict.fromkeys(votes))
    if not unique:
        return PHASE_NEAR_NOMINAL
    if len(unique) > 1:
        return ROOT_MIXED
    return unique[0]


def _overlay_join(row: dict[str, Any], extra: dict[str, Any]) -> None:
    for key, value in extra.items():
        if value is None:
            continue
        if row.get(key) is None:
            row[key] = value


def analyze_stage_diagnostics(
    path: Path, *, expected_sha256: str | None = SCORED_CAPTURE_SHA256
) -> dict[str, Any]:
    digest = sha256_file(path)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ShaMismatchError(f"capture sha256 {digest} != expected {expected_sha256}")

    session_start: dict[str, Any] | None = None
    session_end: dict[str, Any] | None = None
    summary: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    for payload in iter_all_mappings(path):
        if is_session_record(payload):
            if payload.get("record_type") == "session_start":
                session_start = dict(payload)
            elif payload.get("record_type") == "session_end":
                session_end = dict(payload)
                raw_summary = payload.get("stage_diagnostic_summary")
                if isinstance(raw_summary, dict):
                    summary = raw_summary
            continue
        if is_stage_diagnostic_record(payload):
            continue
        if payload.get("schema") != "mexc_ui_raw_snapshot":
            continue
        diag = dict(payload.get("stage_diagnostics") or {})
        rows.append(
            {
                "sequence": int(payload.get("sequence") or 0),
                "trigger": str(payload.get("trigger") or ""),
                "received_at_local": payload.get("received_at_local"),
                "monotonic_ms": _finite(payload.get("monotonic_ms")),
                **{key: diag.get(key) for key in SNAPSHOT_DIAG_KEYS},
            }
        )

    rows.sort(key=lambda item: int(item.get("sequence") or 0))
    lifecycle = list(summary.get("lifecycle") or [])
    detail_join = _merge_by_ordinal(
        list(summary.get("interval_details") or [])
        + list(summary.get("mutation_details") or [])
    )
    completion_join = _merge_by_ordinal(list(summary.get("completions") or []))
    for row in rows:
        ordinal = int(row["request_ordinal"] or 0)
        extra = detail_join.get(ordinal)
        if extra:
            _overlay_join(row, extra)
        extra = completion_join.get(ordinal)
        if extra:
            _overlay_join(row, extra)

    reconstructed = reconstruct_phases(lifecycle)
    if not reconstructed["reconstructed"]:
        return {
            "milestone": MILESTONE,
            "status": ROOT_INCONCLUSIVE,
            "decision": "STOP_FOR_LEAD_REVIEW",
            "root_cause": ROOT_INCONCLUSIVE,
            "inconclusive_reasons": reconstructed["reasons"],
            "capture_sha256": digest,
            "extension_version": (session_start or {}).get("extension_version"),
            "lifecycle": lifecycle,
            "protocol_v2_can_remain_unchanged": True,
            "capture_implementation_changed": False,
            "mom_gap_inspected": False,
        }

    phases = reconstructed["phases"]
    assert phases is not None
    for row in rows:
        row["phase"] = assign_phase(_finite(row.get("callback_mono")), phases)

    # Wall-clock phase stamps from nearby snapshots, not from the 2/6/4 plan.
    for name, phase in phases.items():
        matching = [row for row in rows if row.get("phase") == name]
        phase["n_snapshots"] = len(matching)
        if matching:
            phase["first_received_at_local"] = matching[0]["received_at_local"]
            phase["last_received_at_local"] = matching[-1]["received_at_local"]
            phase["first_received_wall_ms"] = _wall_ms(str(matching[0]["received_at_local"]))
            phase["last_received_wall_ms"] = _wall_ms(str(matching[-1]["received_at_local"]))
        phase["expected_heartbeat_opportunities"] = expected_interval_opportunities(
            phase["start_mono"], phase["end_mono"], INTERVAL_MS
        )

    counters = dict(summary.get("counters") or {})
    high_water = dict(summary.get("high_water") or {})
    histograms = dict(summary.get("histograms") or {})

    phase_stats: dict[str, Any] = {}
    phase_class: dict[str, str] = {}
    for name in PHASES:
        subset = [row for row in rows if row.get("phase") == name]
        stats = _phase_interval_stats(subset)
        stats["expected_heartbeat_opportunities"] = phases[name]["expected_heartbeat_opportunities"]
        actual = stats["n_interval"]
        expected = stats["expected_heartbeat_opportunities"]
        stats["callback_conservation"] = {
            "expected_opportunities": expected,
            "persisted_interval": actual,
            "missing_est": expected - actual,
            "ratio": (actual / expected) if expected else None,
        }
        phase_stats[name] = stats
        phase_class[name] = _classify_phase(stats)

    overall_stats = _phase_interval_stats(rows)
    overall_stats["expected_heartbeat_opportunities"] = expected_interval_opportunities(
        reconstructed["timer_registered_mono"],
        reconstructed["session_stop_mono"],
        INTERVAL_MS,
    )
    overall_stats["callback_conservation"] = {
        "expected_opportunities": overall_stats["expected_heartbeat_opportunities"],
        "timer_callbacks_counter": counters.get("timer_callbacks"),
        "persisted_interval": overall_stats["n_interval"],
        "missing_est": overall_stats["expected_heartbeat_opportunities"]
        - overall_stats["n_interval"],
        "ratio": (
            overall_stats["n_interval"] / overall_stats["expected_heartbeat_opportunities"]
            if overall_stats["expected_heartbeat_opportunities"]
            else None
        ),
    }

    append_hist = histograms.get("append_duration_ms")
    overall_stats["histogram_append_duration_ms"] = histogram_stats(
        append_hist if isinstance(append_hist, dict) else None
    )
    overall_stats["histogram_total_ack_latency_ms"] = histogram_stats(
        histograms.get("total_ack_latency_ms")
        if isinstance(histograms.get("total_ack_latency_ms"), dict)
        else None
    )
    overall_stats["histogram_background_queue_wait_ms"] = histogram_stats(
        histograms.get("background_queue_wait_ms")
        if isinstance(histograms.get("background_queue_wait_ms"), dict)
        else None
    )

    substages: dict[str, Any] = {}
    detail_rows = list(summary.get("interval_details") or []) + list(
        summary.get("mutation_details") or []
    )
    for key in APPEND_SUBSTAGE_KEYS:
        substages[key] = _stats(
            [
                _finite((row.get("append_timings") or {}).get(key))
                for row in detail_rows
            ]
        )

    gaps: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    prev_interval: dict[str, Any] | None = None
    for row in rows:
        mono = _finite(row.get("monotonic_ms"))
        prev_mono = _finite(previous.get("monotonic_ms") if previous else None)
        if previous is not None and mono is not None and prev_mono is not None:
            gap_ms = mono - prev_mono
            if gap_ms > GAP_1000_MS:
                attr = classify_gap(row, gap_ms, previous, prev_interval)
                gaps.append(
                    {
                        "seq_before": previous.get("sequence"),
                        "seq_after": row.get("sequence"),
                        "received_after": row.get("received_at_local"),
                        "phase": row.get("phase"),
                        "visibility_state": row.get("visibility_state"),
                        "gt_2000": gap_ms > GAP_2000_MS,
                        **attr,
                    }
                )
        if row.get("trigger") == "interval":
            prev_interval = row
        previous = row

    gap_primary = Counter(str(item["primary"]) for item in gaps)
    gap_primary_2000 = Counter(str(item["primary"]) for item in gaps if item["gt_2000"])

    producer_epochs = sorted(
        {str(item) for item in (summary.get("producer_epochs") or []) if item}
        | {str(row["producer_epoch"]) for row in rows if row.get("producer_epoch")}
    )
    worker_boots = sorted(
        {str(item) for item in (summary.get("worker_boot_ids") or []) if item}
        | {str(row["worker_boot_id"]) for row in rows if row.get("worker_boot_id")}
    )
    stale = sum(1 for row in rows if row.get("stale_generation"))
    mismatch = sum(1 for row in rows if row.get("session_id_mismatch"))

    lifecycle_kinds = Counter(str(event.get("kind")) for event in lifecycle)

    tree_labels = {
        phase_class[name]
        for name in PHASES
        if phase_class[name]
        in {
            ROOT_TIMER,
            ROOT_FIFO,
            ROOT_STORAGE,
            ROOT_EXTRACT,
            ROOT_IPC,
            ROOT_MIXED,
            ROOT_INCONCLUSIVE,
        }
    }
    if ROOT_INCONCLUSIVE in tree_labels and len(tree_labels) == 1:
        root = ROOT_INCONCLUSIVE
    elif tree_labels == {ROOT_TIMER}:
        root = ROOT_TIMER
    elif tree_labels == {ROOT_FIFO}:
        root = ROOT_FIFO
    elif len(tree_labels) > 1:
        root = ROOT_MIXED
    elif tree_labels:
        root = next(iter(tree_labels))
    else:
        root = ROOT_INCONCLUSIVE

    hidden_stats = phase_stats["hidden"]
    final_stats = phase_stats["final_visible"]
    initial_stats = phase_stats["initial_visible"]
    remediation: dict[str, Any]
    if root == ROOT_INCONCLUSIVE:
        remediation = {
            "choice": [],
            "rationale": (
                "Lifecycle or stage evidence is insufficient; "
                "do not pick a storage/scheduler change."
            ),
        }
    else:
        choice: list[str] = []
        rationale: list[str] = []
        hidden_ratio = hidden_stats["callback_conservation"]["ratio"]
        if hidden_ratio is not None and hidden_ratio < 0.75:
            rationale.append(
                "Hidden-phase interval delivery is ~1 Hz (Chrome timer clamp). "
                "Heartbeat-priority, mutation coalescing, persistence decoupling, "
                "and IDB batching cannot create timer callbacks a hidden renderer does not fire."
            )
        if (
            final_stats["content_queue_depth_high_water"] >= 8
            or float(final_stats["content_queue_wait_ms"]["p95"] or 0) >= 800
        ):
            choice.append("interval-only raw observations / mutations as dirty notifications")
            choice.append("heartbeat-priority + mutation coalescing")
            rationale.append(
                "Final visible phase restores ~500 ms callback interarrival while the "
                "content FIFO depth and wait grow with mutation traffic. Smallest "
                "justified change is to stop optional mutation snapshots from sharing "
                "the ACK-bound FIFO with the heartbeat (candidate D, or A if mutation "
                "rows must remain)."
            )
        hist_append_max = float(overall_stats["histogram_append_duration_ms"]["max"] or 0)
        joined_append_max = float(overall_stats["append_duration_joined_ms"]["max"] or 0)
        append_max = max(hist_append_max, joined_append_max)
        bg_hw = int(overall_stats["background_queue_depth_high_water"] or 0)
        if append_max < 150 and bg_hw == 0:
            rationale.append(
                "IndexedDB append stays tens of milliseconds with background depth 0, "
                "so observation/persistence decoupling and batched IDB are not the "
                "measured cause and are not recommended from this run."
            )
        if not choice and hidden_ratio is not None and hidden_ratio < 0.75:
            rationale.append(
                "No queue/storage remediation is justified until a visible-tab recapture "
                "is the operating condition for protocol v2 cadence bars."
            )
        remediation = {"choice": choice, "rationale": rationale}

    enqueued = counters.get("callbacks_enqueued") or {}
    started = counters.get("tasks_started") or {}
    persisted = counters.get("snapshots_persisted") or {}
    acked = counters.get("acked") or {}
    abandoned = counters.get("abandoned") or {}
    outstanding = int(counters.get("outstanding_at_stop") or 0)
    triggers = ("interval", "mutation", "manual")
    recon = {
        "enqueued_total": sum(int(enqueued.get(key) or 0) for key in triggers),
        "started_total": sum(int(started.get(key) or 0) for key in triggers),
        "persisted_total": sum(int(persisted.get(key) or 0) for key in triggers),
        "acked_total": sum(int(acked.get(key) or 0) for key in triggers),
        "abandoned_total": sum(int(abandoned.get(key) or 0) for key in triggers),
        "outstanding_at_stop": outstanding,
        "persisted_equals_started": persisted == started,
        "abandoned_zero_with_outstanding": outstanding > 0
        and sum(int(abandoned.get(key) or 0) for key in triggers) == 0,
        "note": (
            "Stop does not drain emitChain. outstanding_at_stop counts waiting+active "
            "at the Stop flag; abandoned stays 0 if those tasks had not yet settled "
            "when the content diagnostic delta was flushed."
        ),
    }

    return {
        "milestone": MILESTONE,
        "status": "MEXC_UI_CAPTURE_STAGE_DIAGNOSTIC_ANALYSIS_READY",
        "decision": "STOP_FOR_LEAD_REVIEW",
        "root_cause": root,
        "phase_root_cause": phase_class,
        "ml_status": "NOT_STARTED",
        "paper": False,
        "live": False,
        "strategy_tuning": False,
        "mom_gap_inspected": False,
        "protocol_version": "2.0.0",
        "protocol_v2_can_remain_unchanged": True,
        "capture_implementation_changed": False,
        "extension_version": (session_start or {}).get("extension_version") or EXTENSION_VERSION,
        "diagnostic_format_version": (session_start or {}).get("diagnostic_format_version")
        or DIAGNOSTIC_FORMAT_VERSION,
        "operator_chrome": OPERATOR_CHROME,
        "capture_path": str(path).replace("\\", "/"),
        "capture_sha256": digest,
        "session": {
            "session_id": (session_start or {}).get("session_id"),
            "started_at": (session_start or {}).get("started_at"),
            "ended_at": (session_end or {}).get("ended_at"),
            "interval_ms": (session_start or {}).get("interval_ms") or INTERVAL_MS,
            "page_path": (session_start or {}).get("page_path"),
            "n_snapshots": (session_end or {}).get("n_snapshots") or len(rows),
            "status": (session_end or {}).get("status"),
        },
        "intended_plan_not_authoritative": {
            "visible_s": 120,
            "hidden_s": 360,
            "visible2_s": 240,
            "note": "Manual switching may be off by several seconds; phases ignore this plan.",
        },
        "lifecycle": lifecycle,
        "lifecycle_kind_counts": dict(lifecycle_kinds),
        "freeze_resume_pagehide_pageshow": {
            "freeze": lifecycle_kinds.get("freeze", 0),
            "resume": lifecycle_kinds.get("resume", 0),
            "pagehide": lifecycle_kinds.get("pagehide", 0),
            "pageshow": lifecycle_kinds.get("pageshow", 0),
            "visibilitychange": lifecycle_kinds.get("visibilitychange", 0),
            "worker_boot": lifecycle_kinds.get("worker_boot", 0),
        },
        "phases": phases,
        "phase_stats": phase_stats,
        "overall_stats": overall_stats,
        "append_substages": substages,
        "counters": counters,
        "high_water": high_water,
        "histograms": histograms,
        "reconciliation": recon,
        "producer_epochs": producer_epochs,
        "worker_boot_ids": worker_boots,
        "stale_generation_rows": stale,
        "session_id_mismatch_rows": mismatch,
        "diagnostic_truncation": {
            "diagnostic_truncated": bool(counters.get("diagnostic_truncated")),
            "suppressed_interval_details": counters.get("suppressed_interval_details"),
            "suppressed_mutation_details": counters.get("suppressed_mutation_details"),
            "suppressed_lifecycle": counters.get("suppressed_lifecycle"),
            "suppressed_slow_examples": counters.get("suppressed_slow_examples"),
            "suppressed_completions": counters.get("suppressed_completions"),
            "n_interval_details": len(summary.get("interval_details") or []),
            "n_mutation_details": len(summary.get("mutation_details") or []),
            "n_slow_examples": len(summary.get("slow_examples") or []),
            "n_completions": len(summary.get("completions") or []),
            "n_unique_completion_ordinals": len(completion_join),
            "completion_ordinal_max": max(completion_join) if completion_join else None,
            "completion_coverage_note": (
                "Completions are a capped ring (append_end then ack_end per ordinal). "
                "Final-visible request ordinals are typically missing; session append/ACK "
                "percentiles come from sidecar histograms, not from adding stage p95s."
            ),
        },
        "content_vs_worker_monotonic_subtracted": False,
        "gaps_gt_1000_ms": {
            "n": len(gaps),
            "n_gt_2000": sum(1 for item in gaps if item["gt_2000"]),
            "max_ms": max((float(item["gap_ms"]) for item in gaps), default=None),
            "primary_counts": dict(gap_primary),
            "primary_counts_gt_2000": dict(gap_primary_2000),
            "rows": gaps,
        },
        "visible_vs_hidden": {
            "hidden_interval_interarrival_p50_ms": hidden_stats["callback_interarrival_ms"][
                "p50"
            ],
            "initial_visible_interval_interarrival_p50_ms": initial_stats[
                "callback_interarrival_ms"
            ]["p50"],
            "final_visible_interval_interarrival_p50_ms": final_stats[
                "callback_interarrival_ms"
            ]["p50"],
            "hidden_conservation_ratio": hidden_stats["callback_conservation"]["ratio"],
            "initial_visible_conservation_ratio": initial_stats["callback_conservation"]["ratio"],
            "final_visible_conservation_ratio": final_stats["callback_conservation"]["ratio"],
            "hidden_content_wait_p95_ms": hidden_stats["content_queue_wait_ms"]["p95"],
            "final_visible_content_wait_p95_ms": final_stats["content_queue_wait_ms"]["p95"],
            "hidden_queue_depth_high_water": hidden_stats["content_queue_depth_high_water"],
            "final_visible_queue_depth_high_water": final_stats["content_queue_depth_high_water"],
            "callback_delay_is_cumulative_ordinal_offset": True,
            "callback_delay_note": (
                "callback_delay_ms = callback_mono - (timer_registered + ordinal*500). "
                "Hidden ~1 s delivery makes ordinal lag the 500 ms grid, so delay stays "
                "~minutes in the later visible phase even though interarrival returns to ~500 ms. "
                "Do not treat that leftover offset as continued timer failure after unhide."
            ),
        },
        "remediation": remediation,
        "protocol_note": (
            "Protocol v2.0.0 can remain unchanged. received_at_local stays extract-time. "
            "Do not backdate to timer deadlines, exclude the hidden phase, or relax p95/p99 "
            "bars to manufacture a pass."
        ),
    }


def _fmt(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int) and digits == 0:
        return str(value)
    return f"{float(value):.{digits}f}"


def _fmt_stats(block: dict[str, Any], digits: int = 1) -> str:
    return (
        f"n={block.get('n', 0)} min={_fmt(block.get('min'), digits)} "
        f"p50={_fmt(block.get('p50'), digits)} p90={_fmt(block.get('p90'), digits)} "
        f"p95={_fmt(block.get('p95'), digits)} p99={_fmt(block.get('p99'), digits)} "
        f"max={_fmt(block.get('max'), digits)}"
    )


def render_markdown(report: dict[str, Any]) -> str:
    if report.get("root_cause") == ROOT_INCONCLUSIVE and report.get("status") == ROOT_INCONCLUSIVE:
        reasons = ", ".join(report.get("inconclusive_reasons") or [])
        return (
            f"# MEXC UI capture stage diagnostic analysis v1\n\n"
            f"MILESTONE: `{MILESTONE}`\n\n"
            f"STATUS: `{ROOT_INCONCLUSIVE}`\n\n"
            f"DECISION: `STOP_FOR_LEAD_REVIEW`\n\n"
            f"The expected visible → hidden → visible lifecycle could not be reconstructed "
            f"from recorded diagnostics ({reasons}). Intended 2/6/4 minute marks were not "
            f"used as a substitute. No capture or protocol change. No mom/gap.\n"
        )

    phases = report["phases"]
    overall = report["overall_stats"]
    phase_stats = report["phase_stats"]
    gaps = report["gaps_gt_1000_ms"]
    recon = report["reconciliation"]
    trunc = report["diagnostic_truncation"]
    vis = report["visible_vs_hidden"]
    rem = report["remediation"]
    substages = report["append_substages"]
    freeze = report["freeze_resume_pagehide_pageshow"]
    lines = [
        "# MEXC UI capture stage diagnostic analysis v1",
        "",
        f"MILESTONE: `{report['milestone']}`",
        "",
        f"STATUS: `{report['status']}`",
        "",
        f"DECISION: `{report['decision']}`",
        "",
        f"ROOT_CAUSE: `{report['root_cause']}`",
        "",
        (
            "Phase causes: "
            + ", ".join(
                f"{name}=`{report['phase_root_cause'][name]}`" for name in PHASES
            )
        ),
        "",
        f"ML_STATUS: `{report['ml_status']}`",
        "",
        "PAPER: **false**",
        "",
        "LIVE: **false**",
        "",
        "STRATEGY_TUNING: **false**",
        "",
        "MOM/GAP: **not inspected**",
        "",
        "Capture implementation: **unchanged**",
        "",
        "Protocol v2.0.0: **unchanged** (can remain unchanged)",
        "",
        "## Capture lock",
        "",
        f"- sha256: `{report['capture_sha256']}`",
        f"- path: `{report['capture_path']}`",
        f"- extension: `{report['extension_version']}`",
        f"- diagnostic_format_version: `{report['diagnostic_format_version']}`",
        f"- operator Chrome (attested, not in RAW): `{report['operator_chrome']}`",
        (
            f"- session `{report['session']['session_id']}` "
            f"{report['session']['started_at']} → {report['session']['ended_at']} "
            f"status `{report['session']['status']}` interval_ms="
            f"{report['session']['interval_ms']} n_snapshots="
            f"{report['session']['n_snapshots']}"
        ),
        f"- page_path: `{report['session']['page_path']}`",
        "",
        "## Phase reconstruction",
        "",
        "Phases are taken only from `timer_registered`, `visibilitychange`, and "
        "`session_stop`. The intended ~2 / ~6 / ~4 minute plan is operator context "
        "and is **not** a timestamp source.",
        "",
        "| phase | vis | start | end | ms | min | expected 500ms |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in PHASES:
        phase = phases[name]
        lines.append(
            f"| {name} | {phase['visibility']} | {phase['start_event']} | "
            f"{phase['end_event']} | {_fmt(phase['duration_ms'], 1)} | "
            f"{_fmt(phase['duration_ms'] / 60000.0, 2)} | "
            f"{phase['expected_heartbeat_opportunities']} |"
        )
    lines.extend(
        [
            "",
            (
                f"Content monotonic bounds: timer_registered="
                f"{_fmt(phases['initial_visible']['start_mono'], 1)} → hidden="
                f"{_fmt(phases['hidden']['start_mono'], 1)} → visible="
                f"{_fmt(phases['final_visible']['start_mono'], 1)} → stop="
                f"{_fmt(phases['final_visible']['end_mono'], 1)}."
            ),
            "",
            "## Whole-session path",
            "",
            (
                f"- expected heartbeat opportunities: "
                f"{overall['callback_conservation']['expected_opportunities']}"
            ),
            (
                f"- timer_callbacks counter / persisted interval: "
                f"{overall['callback_conservation']['timer_callbacks_counter']} / "
                f"{overall['callback_conservation']['persisted_interval']} "
                f"(missing_est {overall['callback_conservation']['missing_est']}, "
                f"ratio {_fmt(overall['callback_conservation']['ratio'], 3)})"
            ),
            f"- interval callback_delay_ms: {_fmt_stats(overall['callback_delay_ms'])}",
            (
                f"- interval callback interarrival_ms: "
                f"{_fmt_stats(overall['callback_interarrival_ms'])}"
            ),
            f"- content queue wait_ms: {_fmt_stats(overall['content_queue_wait_ms'])}",
            (
                f"- content queue depth high-water: "
                f"{overall['content_queue_depth_high_water']} "
                f"(session summary {report['high_water'].get('content_queue_depth')})"
            ),
            f"- extract_duration_ms: {_fmt_stats(overall['extract_duration_ms'])}",
            f"- background queue wait_ms: {_fmt_stats(overall['background_queue_wait_ms'])}",
            (
                f"- background queue depth high-water: "
                f"{overall['background_queue_depth_high_water']} "
                f"(session summary {report['high_water'].get('background_queue_depth')})"
            ),
            (
                f"- append_duration_ms histogram (full session): "
                f"{_fmt_stats(overall['histogram_append_duration_ms'])}"
            ),
            (
                f"- ack_latency_ms histogram (full session): "
                f"{_fmt_stats(overall['histogram_total_ack_latency_ms'])}"
            ),
            (
                f"- append_duration joined from completion ring: "
                f"{_fmt_stats(overall['append_duration_joined_ms'])} "
                f"(n_joined={overall['n_append_duration_joined']})"
            ),
            (
                f"- ack_latency joined from completion ring: "
                f"{_fmt_stats(overall['total_ack_latency_joined_ms'])} "
                f"(n_joined={overall['n_ack_joined']})"
            ),
            (
                "- Content `performance.now()` and worker `performance.now()` are never "
                "subtracted; IPC residual uses ACK minus append minus background wait "
                "on the same joined request when those fields exist."
            ),
            "",
            "Append substages (interval details + mutation samples; not every row):",
            "",
        ]
    )
    for key in APPEND_SUBSTAGE_KEYS:
        lines.append(f"- `{key}`: {_fmt_stats(substages[key], 2)}")
    lines.extend(
        [
            "",
            "## Per phase",
            "",
        ]
    )
    for name in PHASES:
        stats = phase_stats[name]
        cons = stats["callback_conservation"]
        lines.extend(
            [
                f"### {name} (`{report['phase_root_cause'][name]}`)",
                "",
                (
                    f"- snapshots {stats['n_snapshots']} "
                    f"(interval {stats['n_interval']}, mutation {stats['n_mutation']}, "
                    f"manual {stats['n_manual']})"
                ),
                (
                    f"- expected slots {cons['expected_opportunities']}, persisted interval "
                    f"{cons['persisted_interval']}, missing_est {cons['missing_est']}, "
                    f"ratio {_fmt(cons['ratio'], 3)}"
                ),
                (
                    f"- interval ordinal {stats['interval_callback_ordinal_min']}–"
                    f"{stats['interval_callback_ordinal_max']}; ideal slot "
                    f"{stats['elapsed_ideal_slot_min']}–{stats['elapsed_ideal_slot_max']}"
                ),
                f"- callback_delay_ms: {_fmt_stats(stats['callback_delay_ms'])}",
                f"- callback interarrival_ms: {_fmt_stats(stats['callback_interarrival_ms'])}",
                f"- content wait_ms: {_fmt_stats(stats['content_queue_wait_ms'])}",
                (
                    f"- interval-only content wait_ms: "
                    f"{_fmt_stats(stats['content_queue_wait_interval_ms'])}"
                ),
                f"- content depth high-water: {stats['content_queue_depth_high_water']}",
                f"- extract_duration_ms: {_fmt_stats(stats['extract_duration_ms'])}",
                f"- background wait_ms: {_fmt_stats(stats['background_queue_wait_ms'])}",
                f"- background depth high-water: {stats['background_queue_depth_high_water']}",
                (
                    f"- append_duration joined: "
                    f"{_fmt_stats(stats['append_duration_joined_ms'])} "
                    f"(n={stats['n_append_duration_joined']})"
                ),
                (
                    f"- append_timings_sum_ms: {_fmt_stats(stats['append_timings_sum_ms'])}"
                ),
                (
                    f"- ack_latency joined: "
                    f"{_fmt_stats(stats['total_ack_latency_joined_ms'])} "
                    f"(n={stats['n_ack_joined']})"
                ),
                "",
            ]
        )
    lines.extend(
        [
            "## Visible vs hidden",
            "",
            vis["callback_delay_note"],
            "",
            (
                f"- interval interarrival p50 ms: initial_visible "
                f"{_fmt(vis['initial_visible_interval_interarrival_p50_ms'])}, hidden "
                f"{_fmt(vis['hidden_interval_interarrival_p50_ms'])}, final_visible "
                f"{_fmt(vis['final_visible_interval_interarrival_p50_ms'])}"
            ),
            (
                f"- conservation ratio: initial_visible "
                f"{_fmt(vis['initial_visible_conservation_ratio'], 3)}, hidden "
                f"{_fmt(vis['hidden_conservation_ratio'], 3)}, final_visible "
                f"{_fmt(vis['final_visible_conservation_ratio'], 3)}"
            ),
            (
                f"- content wait p95 ms / depth HW: hidden "
                f"{_fmt(vis['hidden_content_wait_p95_ms'])} / "
                f"{vis['hidden_queue_depth_high_water']}; final_visible "
                f"{_fmt(vis['final_visible_content_wait_p95_ms'])} / "
                f"{vis['final_visible_queue_depth_high_water']}"
            ),
            "",
            "Hidden delivery is a ~1 s background timer clamp when conservation drops "
            "and queue wait stays small. After unhide, ~500 ms callbacks can return "
            "while mutation traffic lengthens content wait.",
            "",
            "## Reconciliation, fencing, lifecycle, truncation",
            "",
            (
                f"- enqueued {recon['enqueued_total']}, started {recon['started_total']}, "
                f"persisted {recon['persisted_total']}, acked {recon['acked_total']}, "
                f"abandoned {recon['abandoned_total']}, outstanding_at_stop "
                f"{recon['outstanding_at_stop']}"
            ),
            f"- {recon['note']}",
            (
                f"- producer_epochs ({len(report['producer_epochs'])}): "
                f"`{report['producer_epochs']}`"
            ),
            (
                f"- worker_boot_ids ({len(report['worker_boot_ids'])}): "
                f"`{report['worker_boot_ids']}`"
            ),
            (
                f"- stale_generation rows: {report['stale_generation_rows']}; "
                f"session_id_mismatch rows: {report['session_id_mismatch_rows']}"
            ),
            (
                f"- lifecycle: visibilitychange={freeze['visibilitychange']}, "
                f"pageshow={freeze['pageshow']}, pagehide={freeze['pagehide']}, "
                f"freeze={freeze['freeze']}, resume={freeze['resume']}, "
                f"worker_boot={freeze['worker_boot']}"
            ),
            "- No `freeze` / `resume` / `pagehide` during the running session. "
            "`pageshow` is the content-script load (session_generation 0).",
            (
                f"- diagnostic_truncated={trunc['diagnostic_truncated']}: "
                f"suppressed mutation details {trunc['suppressed_mutation_details']}, "
                f"slow examples {trunc['suppressed_slow_examples']}, "
                f"completions {trunc['suppressed_completions']} "
                f"(unique ordinals {trunc.get('n_unique_completion_ordinals')}, "
                f"max {trunc.get('completion_ordinal_max')}); "
                f"interval details {trunc['n_interval_details']}, "
                f"lifecycle suppressed {trunc['suppressed_lifecycle']}"
            ),
            "",
            "Counters and histograms still cover the full session after those caps. "
            "Do not treat the completion ring as a complete ACK join.",
            "",
            "## Raw-observation gaps",
            "",
            (
                f"- n >1000 ms: {gaps['n']}; n >2000 ms: {gaps['n_gt_2000']}; "
                f"max {_fmt(gaps['max_ms'])} ms"
            ),
            f"- primary attribution >1000 ms: `{gaps['primary_counts']}`",
            f"- primary attribution >2000 ms: `{gaps['primary_counts_gt_2000']}`",
            "",
            "Attribution uses the closing request's own wait/extract/callback gap/slot jump. "
            "Percentiles from different stages are never added together.",
            "",
            "| seq_b | seq_a | phase | gap_ms | primary | wait | extract | slot |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for item in gaps.get("rows") or []:
        lines.append(
            f"| {item.get('seq_before')} | {item.get('seq_after')} | {item.get('phase')} | "
            f"{_fmt(item.get('gap_ms'))} | {item.get('primary')} | "
            f"{_fmt(item.get('content_queue_wait_ms'))} | "
            f"{_fmt(item.get('extract_duration_ms'))} | {item.get('elapsed_slot_jump')} |"
        )
    lines.extend(
        [
            "",
            "## Root cause",
            "",
            (
                f"**{report['root_cause']}.** Hidden phase is "
                f"`{report['phase_root_cause']['hidden']}` "
                f"(conservation {_fmt(vis['hidden_conservation_ratio'], 3)}, "
                f"interval interarrival p50 {_fmt(vis['hidden_interval_interarrival_p50_ms'])} ms, "
                f"wait p95 {_fmt(vis['hidden_content_wait_p95_ms'])} ms, depth "
                f"{vis['hidden_queue_depth_high_water']}). Final visible is "
                f"`{report['phase_root_cause']['final_visible']}` "
                f"(conservation {_fmt(vis['final_visible_conservation_ratio'], 3)}, "
                f"interarrival p50 {_fmt(vis['final_visible_interval_interarrival_p50_ms'])} ms, "
                f"wait p95 {_fmt(vis['final_visible_content_wait_p95_ms'])} ms, depth "
                f"{vis['final_visible_queue_depth_high_water']}). "
                f"Initial visible is `{report['phase_root_cause']['initial_visible']}`."
            ),
            "",
            "Storage, extraction, and service-worker restart are not dominant when "
            "background depth stays 0, extract remains well under 250 ms, and append "
            "histogram max stays tens of milliseconds.",
            "",
            "## Remediation recommendation (not implemented)",
            "",
        ]
    )
    if rem.get("choice"):
        lines.append("Supported next implementation, smallest first:")
        lines.append("")
        for item in rem["choice"]:
            lines.append(f"- {item}")
        lines.append("")
    for note in rem.get("rationale") or []:
        lines.append(f"- {note}")
    lines.extend(
        [
            "",
            "## Protocol v2",
            "",
            report["protocol_note"],
            "",
            "## Decision",
            "",
            "**STOP_FOR_LEAD_REVIEW.** Do not change capture code in this milestone. "
            "Do not start the 8–12 h corpus. Do not retune mom/gap. Do not start ML, "
            "PAPER, or LIVE.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def write_reports(*, raw: Path, out_json: Path, out_md: Path) -> dict[str, Any]:
    payload = analyze_stage_diagnostics(raw, expected_sha256=SCORED_CAPTURE_SHA256)
    # Gap row list is complete in JSON; keep markdown from duplicating 60 rows.
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    out_md.write_text(render_markdown(payload), encoding="utf-8")
    return payload
