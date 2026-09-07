"""Cadence forensics for one MEXC UI raw capture.

FORENSICS ONLY. Does not change the extension, does not amend protocol v2,
and does not inspect mom/gap, PnL, or the 21 cells.

All numbers come from persisted snapshots. The analyzer never invents
scheduled_tick_monotonic, queue wait, queue depth, visibility, background
message receipt time, or IndexedDB append timing.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_bot.research.mexc_shadow.ui_capture.durable import (
    DEFAULT_CHUNK_SIZE,
    is_session_record,
)
from trading_bot.research.mexc_shadow.ui_capture.normalize import snapshot_from_mapping
from trading_bot.research.mexc_shadow.ui_capture.schema import UiRawSnapshot
from trading_bot.research.mexc_shadow.ui_capture.store import iter_all_mappings

STRATEGY_FIELDS = ("bid", "ask", "last", "mark", "index")
CHUNK_SIZE = DEFAULT_CHUNK_SIZE
WINDOW_MS = 30_000
CONFIGURED_INTERVAL_MS = 500
GAP_1000_MS = 1000.0
GAP_2000_MS = 2000.0
MUTATION_WINDOWS_MS = (1_000, 2_000, 5_000, 10_000)
RESUME_BURST_MS = 2_500
RESUME_ISOLATED_MS = 600
BOUNDARY_NEAR = 5
# Operator 1.3.3 export scored by the v2 contract gate. Not rewritten.
SCORED_CAPTURE_SHA256 = (
    "1f83d307cc42324f803b26c01f6a6afde5eb1dc65b938909cf50eaeb09b56f2b"
)

MISSING_TELEMETRY = (
    "scheduled_tick_monotonic",
    "queue_wait_ms",
    "queue_depth",
    "visibility_state",
    "background_message_receipt_time",
    "idb_append_start_ms",
    "idb_append_end_ms",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _report_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _wall_ms(stamp: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() * 1000.0


def _percentile(sorted_vals: list[float], fraction: float) -> float | None:
    """Match quality.py rank estimator."""

    if not sorted_vals:
        return None
    index = min(len(sorted_vals) - 1, int(len(sorted_vals) * fraction))
    return sorted_vals[index]


def _delta_stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "n": 0,
            "min": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "max": None,
            "mean": None,
            "n_gt_1000": 0,
            "n_gt_2000": 0,
        }
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "min": ordered[0],
        "p50": _percentile(ordered, 0.50),
        "p90": _percentile(ordered, 0.90),
        "p95": _percentile(ordered, 0.95),
        "p99": _percentile(ordered, 0.99),
        "max": ordered[-1],
        "mean": sum(ordered) / len(ordered),
        "n_gt_1000": sum(1 for item in ordered if item > GAP_1000_MS),
        "n_gt_2000": sum(1 for item in ordered if item > GAP_2000_MS),
    }


def _five_key(snap: UiRawSnapshot) -> tuple[float | str | None, ...]:
    values: list[float | str | None] = []
    for name in STRATEGY_FIELDS:
        field = snap.fields.get(name)
        values.append(None if field is None else field.value)
    return tuple(values)


def _five_ages(snap: UiRawSnapshot) -> dict[str, float | None]:
    ages: dict[str, float | None] = {}
    for name in STRATEGY_FIELDS:
        field = snap.fields.get(name)
        ages[name] = None if field is None else field.age_ms
    return ages


def _chunk_index(sequence: int) -> int:
    return (sequence - 1) // CHUNK_SIZE


def _chunk_fill(sequence: int) -> int:
    return ((sequence - 1) % CHUNK_SIZE) + 1


def _near_chunk_boundary(sequence: int) -> bool:
    fill = _chunk_fill(sequence)
    return fill <= BOUNDARY_NEAR or fill > CHUNK_SIZE - BOUNDARY_NEAR


def _iso(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC).isoformat()


def _classify_clock(*, wall: float, mono: float | None) -> str:
    """Classify a long wall gap. Does not infer sleep from matching clocks."""

    if mono is None:
        return "missing_mono"
    # Matching large wall and mono is elapsed process time, not a wall-clock jump.
    if wall > GAP_2000_MS and mono > GAP_2000_MS and abs(wall - mono) <= 200.0:
        return "elapsed_both"
    if wall > GAP_2000_MS and mono <= GAP_2000_MS:
        return "wall_jump"
    if mono > GAP_2000_MS and wall <= GAP_2000_MS:
        return "mono_jump"
    if abs(wall - mono) > 200.0:
        return "wall_mono_disagree"
    return "elapsed_both"


class ShaMismatchError(ValueError):
    """Capture bytes do not match the SHA-256 this milestone locked."""


def analyze_cadence(
    path: Path, *, expected_sha256: str | None = SCORED_CAPTURE_SHA256
) -> dict[str, Any]:
    """Compute cadence forensics from persisted RAW only."""

    digest = sha256_file(path)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ShaMismatchError(
            f"capture sha256 {digest} != expected {expected_sha256}"
        )

    session_start: dict[str, Any] | None = None
    session_end: dict[str, Any] | None = None
    rows: list[dict[str, Any]] = []
    for payload in iter_all_mappings(path):
        if is_session_record(payload):
            if payload.get("record_type") == "session_start":
                session_start = dict(payload)
            elif payload.get("record_type") == "session_end":
                session_end = dict(payload)
            continue
        if payload.get("schema") != "mexc_ui_raw_snapshot":
            continue
        snap = snapshot_from_mapping(payload)
        wall = _wall_ms(snap.received_at_local)
        if wall is None:
            continue
        rows.append(
            {
                "sequence": int(snap.sequence),
                "trigger": str(snap.trigger),
                "received_at_local": snap.received_at_local,
                "wall_ms": wall,
                "monotonic_ms": snap.monotonic_ms,
                "changed_fields": list(snap.changed_fields),
                "five": _five_key(snap),
                "ages": _five_ages(snap),
                "chunk_index": _chunk_index(int(snap.sequence)),
                "chunk_fill": _chunk_fill(int(snap.sequence)),
            }
        )

    n = len(rows)
    triggers = Counter(str(row["trigger"]) for row in rows)
    first = rows[0] if rows else None
    last = rows[-1] if rows else None
    duration_ms = (
        None if first is None or last is None else float(last["wall_ms"]) - float(first["wall_ms"])
    )
    duration_mono_ms = None
    if (
        first is not None
        and last is not None
        and first["monotonic_ms"] is not None
        and last["monotonic_ms"] is not None
    ):
        duration_mono_ms = float(last["monotonic_ms"]) - float(first["monotonic_ms"])

    session_span_ms = None
    if session_start and session_end:
        start_ms = _wall_ms(str(session_start.get("started_at") or ""))
        end_ms = _wall_ms(str(session_end.get("ended_at") or ""))
        if start_ms is not None and end_ms is not None:
            session_span_ms = end_ms - start_ms

    all_wall: list[float] = []
    all_mono: list[float] = []
    abs_clock: list[float] = []
    clock_outliers: list[dict[str, Any]] = []
    n_missing_mono = 0
    n_nonpositive_mono = 0
    pairs: list[dict[str, Any]] = []
    for index in range(1, n):
        left = rows[index - 1]
        right = rows[index]
        wall = float(right["wall_ms"]) - float(left["wall_ms"])
        all_wall.append(wall)
        mono = None
        if left["monotonic_ms"] is None or right["monotonic_ms"] is None:
            n_missing_mono += 1
        else:
            mono = float(right["monotonic_ms"]) - float(left["monotonic_ms"])
            all_mono.append(mono)
            if mono <= 0:
                n_nonpositive_mono += 1
            abs_diff = abs(wall - mono)
            abs_clock.append(abs_diff)
            if abs_diff > 50.0:
                clock_outliers.append(
                    {
                        "seq_before": left["sequence"],
                        "seq_after": right["sequence"],
                        "wall_delta_ms": wall,
                        "mono_delta_ms": mono,
                        "abs_diff_ms": abs_diff,
                        "trigger_before": left["trigger"],
                        "trigger_after": right["trigger"],
                    }
                )
        pair = {
            "seq_before": left["sequence"],
            "seq_after": right["sequence"],
            "t_before": left["received_at_local"],
            "t_after": right["received_at_local"],
            "wall_before_ms": left["wall_ms"],
            "wall_after_ms": right["wall_ms"],
            "wall_delta_ms": wall,
            "mono_delta_ms": mono,
            "trigger_before": left["trigger"],
            "trigger_after": right["trigger"],
            "trigger_before_prev": None if index < 2 else rows[index - 2]["trigger"],
            "trigger_after_next": None if index + 1 >= n else rows[index + 1]["trigger"],
            "chunk_index_before": left["chunk_index"],
            "chunk_index_after": right["chunk_index"],
            "chunk_fill_before": left["chunk_fill"],
            "chunk_fill_after": right["chunk_fill"],
            "seq_mod_250_before": int(left["sequence"]) % CHUNK_SIZE,
            "seq_mod_250_after": int(right["sequence"]) % CHUNK_SIZE,
            "near_chunk_boundary": _near_chunk_boundary(int(left["sequence"]))
            or _near_chunk_boundary(int(right["sequence"])),
            "identical_five": left["five"] == right["five"],
            "changed_fields_before": list(left["changed_fields"]),
            "changed_fields_after": list(right["changed_fields"]),
            "ages_before": dict(left["ages"]),
            "ages_after": dict(right["ages"]),
            "five_before": list(left["five"]),
            "five_after": list(right["five"]),
            "clock_class": _classify_clock(wall=wall, mono=mono),
        }
        pairs.append(pair)

    gaps_1000 = [pair for pair in pairs if pair["wall_delta_ms"] > GAP_1000_MS]
    gaps_2000 = [pair for pair in pairs if pair["wall_delta_ms"] > GAP_2000_MS]

    def _cluster(gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        clusters: list[dict[str, Any]] = []
        current: list[dict[str, Any]] = []
        for gap in gaps:
            if current and int(gap["seq_before"]) != int(current[-1]["seq_after"]):
                clusters.append(_close_cluster(current))
                current = []
            current.append(gap)
        if current:
            clusters.append(_close_cluster(current))
        return clusters

    def _close_cluster(items: list[dict[str, Any]]) -> dict[str, Any]:
        start_seq = int(items[0]["seq_before"])
        end_seq = int(items[-1]["seq_after"])
        span = float(items[-1]["wall_after_ms"]) - float(items[0]["wall_before_ms"])
        return {
            "start_seq": start_seq,
            "end_seq": end_seq,
            "n_gaps": len(items),
            "span_ms": span,
            "n_snapshots_inside": max(0, end_seq - start_seq + 1),
            "sequences": [int(item["seq_before"]) for item in items] + [end_seq],
        }

    interval_rows = [row for row in rows if row["trigger"] == "interval"]
    interval_wall: list[float] = []
    interval_mono: list[float] = []
    interval_pairs: list[dict[str, Any]] = []
    for index in range(1, len(interval_rows)):
        left = interval_rows[index - 1]
        right = interval_rows[index]
        wall = float(right["wall_ms"]) - float(left["wall_ms"])
        interval_wall.append(wall)
        mono = None
        if left["monotonic_ms"] is not None and right["monotonic_ms"] is not None:
            mono = float(right["monotonic_ms"]) - float(left["monotonic_ms"])
            interval_mono.append(mono)
        n_mut_between = sum(
            1
            for row in rows
            if row["trigger"] == "mutation"
            and float(left["wall_ms"]) < float(row["wall_ms"]) < float(right["wall_ms"])
        )
        interval_pairs.append(
            {
                "seq_before": left["sequence"],
                "seq_after": right["sequence"],
                "wall_delta_ms": wall,
                "mono_delta_ms": mono,
                "n_mutation_between": n_mut_between,
            }
        )

    # Mutation density and resume pattern on each >1000 ms consecutive gap.
    for gap in gaps_1000:
        t0 = float(gap["wall_before_ms"])
        t1 = float(gap["wall_after_ms"])
        density: dict[str, Any] = {}
        for window in MUTATION_WINDOWS_MS:
            before = [
                row
                for row in rows
                if t0 - window <= float(row["wall_ms"]) < t0
            ]
            after = [
                row
                for row in rows
                if t1 < float(row["wall_ms"]) <= t1 + window
            ]
            n_mut_before = sum(1 for row in before if row["trigger"] == "mutation")
            n_mut_after = sum(1 for row in after if row["trigger"] == "mutation")
            density[str(window)] = {
                "n_all_before": len(before),
                "n_mutation_before": n_mut_before,
                "n_all_after": len(after),
                "n_mutation_after": n_mut_after,
                "mutation_per_s_before": n_mut_before / (window / 1000.0),
                "mutation_per_s_after": n_mut_after / (window / 1000.0),
            }
        gap["mutation_density"] = density
        n_mut_pm5 = (
            density["5000"]["n_mutation_before"] + density["5000"]["n_mutation_after"]
        )
        gap["n_mutation_pm5s"] = n_mut_pm5
        resume_triggers = [
            str(row["trigger"])
            for row in rows
            if int(row["sequence"]) >= int(gap["seq_after"])
        ][:6]
        gap["resume_next_6_triggers"] = resume_triggers
        interval_after = [
            row
            for row in interval_rows
            if t1 <= float(row["wall_ms"]) <= t1 + RESUME_BURST_MS
        ]
        gap["n_interval_in_2500ms_after"] = len(interval_after)
        first_interval_after = next(
            (
                row
                for row in interval_rows
                if float(row["wall_ms"]) >= t1
            ),
            None,
        )
        isolated = False
        burst = False
        if first_interval_after is not None:
            t_int = float(first_interval_after["wall_ms"])
            neighbors = [
                row
                for row in interval_rows
                if row["sequence"] != first_interval_after["sequence"]
                and abs(float(row["wall_ms"]) - t_int) <= RESUME_ISOLATED_MS
            ]
            isolated = len(neighbors) == 0
            burst = len(interval_after) >= 2
        gap["interval_resume_isolated"] = isolated
        gap["interval_resume_burst"] = burst
        # Tight mutation cluster immediately before the silent pair.
        tight = [
            row
            for row in rows
            if row["trigger"] == "mutation" and t0 - 150 <= float(row["wall_ms"]) < t0
        ]
        gap["n_mutation_150ms_before"] = len(tight)

    n_quiet = sum(
        1
        for gap in gaps_1000
        if gap["identical_five"]
        and int(gap["n_mutation_pm5s"]) == 0
        and gap["trigger_before"] != "interval"
        and gap["trigger_after"] != "interval"
    )
    n_serialized = sum(1 for gap in gaps_1000 if int(gap["n_mutation_150ms_before"]) >= 3)
    n_timer_loss_interval = sum(
        1
        for item in interval_pairs
        if item["wall_delta_ms"] > CONFIGURED_INTERVAL_MS
        and int(item["n_mutation_between"]) > 0
    )
    n_last_only = 0
    for gap in gaps_1000:
        if gap["identical_five"]:
            continue
        before = list(gap["five_before"])
        after = list(gap["five_after"])
        diffs = [
            name
            for name, left, right in zip(STRATEGY_FIELDS, before, after, strict=True)
            if left != right
        ]
        gap["five_fields_changed"] = diffs
        if diffs == ["last"]:
            n_last_only += 1
    for gap in gaps_1000:
        if gap["identical_five"]:
            gap["five_fields_changed"] = []
    n_resume_isolated = sum(1 for gap in gaps_1000 if gap["interval_resume_isolated"])
    n_resume_burst = sum(1 for gap in gaps_1000 if gap["interval_resume_burst"])

    windows: list[dict[str, Any]] = []
    if first is not None and last is not None and duration_ms is not None:
        t_origin = float(first["wall_ms"])
        t_end = float(last["wall_ms"])
        window_index = 0
        t0 = t_origin
        while t0 < t_end:
            t1 = min(t0 + WINDOW_MS, t_end)
            if t1 == t_end:
                members = [
                    row for row in rows if t0 <= float(row["wall_ms"]) <= t1
                ]
            else:
                members = [
                    row for row in rows if t0 <= float(row["wall_ms"]) < t1
                ]
            n_interval = sum(1 for row in members if row["trigger"] == "interval")
            n_mutation = sum(1 for row in members if row["trigger"] == "mutation")
            n_manual = sum(1 for row in members if row["trigger"] == "manual")
            span = t1 - t0
            scheduled = span / float(CONFIGURED_INTERVAL_MS)
            n_gt1000 = sum(
                1
                for pair in pairs
                if t0 <= float(pair["wall_before_ms"]) < t1
                and pair["wall_delta_ms"] > GAP_1000_MS
            )
            n_gt2000 = sum(
                1
                for pair in pairs
                if t0 <= float(pair["wall_before_ms"]) < t1
                and pair["wall_delta_ms"] > GAP_2000_MS
            )
            windows.append(
                {
                    "index": window_index,
                    "t0": _iso(t0),
                    "t1": _iso(t1),
                    "span_ms": span,
                    "n_all": len(members),
                    "n_interval": n_interval,
                    "n_mutation": n_mutation,
                    "n_manual": n_manual,
                    "n_wall_gaps_gt1000": n_gt1000,
                    "n_wall_gaps_gt2000": n_gt2000,
                    "estimated_scheduled_ticks": scheduled,
                    "missing_interval_est": max(0.0, scheduled - n_interval),
                }
            )
            window_index += 1
            t0 = t1

    chunk_sizes = Counter(int(row["chunk_index"]) for row in rows)
    left_1000 = Counter(int(gap["chunk_index_before"]) for gap in gaps_1000)
    left_2000 = Counter(int(gap["chunk_index_before"]) for gap in gaps_2000)
    n_boundary = sum(1 for gap in gaps_1000 if gap["near_chunk_boundary"])
    expected_left = {
        str(index): (chunk_sizes[index] / n if n else 0.0) * len(gaps_1000)
        for index in sorted(chunk_sizes)
    }

    n_interval = int(triggers.get("interval", 0))
    scheduled_span = None if duration_ms is None else duration_ms / float(CONFIGURED_INTERVAL_MS)
    scheduled_session = (
        None if session_span_ms is None else session_span_ms / float(CONFIGURED_INTERVAL_MS)
    )
    missing_span = (
        None if scheduled_span is None else max(0.0, scheduled_span - n_interval)
    )
    missing_session = (
        None if scheduled_session is None else max(0.0, scheduled_session - n_interval)
    )

    clock_counts = Counter(
        str(gap["clock_class"]) for gap in gaps_2000
    )
    ordered_clock = sorted(abs_clock)
    return {
        "sha256": digest,
        "path": _report_path(path),
        "n_snapshots": n,
        "first_received_at_local": None if first is None else first["received_at_local"],
        "last_received_at_local": None if last is None else last["received_at_local"],
        "duration_ms": duration_ms,
        "duration_mono_ms": duration_mono_ms,
        "session_started_at": None if session_start is None else session_start.get("started_at"),
        "session_ended_at": None if session_end is None else session_end.get("ended_at"),
        "session_span_ms": session_span_ms,
        "session_interval_ms": None
        if session_start is None
        else session_start.get("interval_ms"),
        "session_n_chunks": None if session_end is None else session_end.get("n_chunks"),
        "session_storage_error": None if session_end is None else session_end.get("storage_error"),
        "session_status": None if session_end is None else session_end.get("status"),
        "trigger_counts": dict(triggers),
        "configured_interval_ms": CONFIGURED_INTERVAL_MS,
        "expected_scheduled_ticks_snapshot_span": scheduled_span,
        "expected_scheduled_ticks_session_span": scheduled_session,
        "observed_interval_snapshots": n_interval,
        "missing_interval_est_snapshot_span": missing_span,
        "missing_interval_est_session_span": missing_session,
        "interval_to_interval_wall": _delta_stats(interval_wall),
        "interval_to_interval_mono": _delta_stats(interval_mono),
        "all_trigger_wall": _delta_stats(all_wall),
        "all_trigger_mono": _delta_stats(all_mono),
        "n_gaps_gt_1000_ms": len(gaps_1000),
        "n_gaps_gt_2000_ms": len(gaps_2000),
        "gaps_gt_1000_ms": gaps_1000,
        "gaps_gt_2000_ms": gaps_2000,
        "clusters_gt_1000_ms": _cluster(gaps_1000),
        "clusters_gt_2000_ms": _cluster(gaps_2000),
        "n_identical_five_among_gt_1000": sum(1 for gap in gaps_1000 if gap["identical_five"]),
        "n_changed_five_among_gt_1000": sum(
            1 for gap in gaps_1000 if not gap["identical_five"]
        ),
        "n_gt_1000_changed_last_only": n_last_only,
        "resume": {
            "n_gaps_gt_1000": len(gaps_1000),
            "n_interval_resume_isolated": n_resume_isolated,
            "n_interval_resume_burst": n_resume_burst,
        },
        "chunk": {
            "chunk_size": CHUNK_SIZE,
            "n_snapshots_by_chunk": {str(k): v for k, v in sorted(chunk_sizes.items())},
            "left_bound_gt_1000_by_chunk": {
                str(k): v for k, v in sorted(left_1000.items())
            },
            "left_bound_gt_2000_by_chunk": {
                str(k): v for k, v in sorted(left_2000.items())
            },
            "expected_left_gt_1000_proportional": expected_left,
            "n_gt_1000_near_boundary": n_boundary,
            "frac_gt_1000_near_boundary": (n_boundary / len(gaps_1000) if gaps_1000 else 0.0),
        },
        "wall_vs_monotonic": {
            "n_pairs": n - 1 if n else 0,
            "n_missing_mono": n_missing_mono,
            "monotonic_strictly_increasing": n_nonpositive_mono == 0 and n_missing_mono == 0,
            "n_nonpositive_mono_deltas": n_nonpositive_mono,
            "mean_abs_wall_minus_mono_ms": (
                None if not abs_clock else sum(abs_clock) / len(abs_clock)
            ),
            "median_abs_wall_minus_mono_ms": _percentile(ordered_clock, 0.50),
            "p95_abs_wall_minus_mono_ms": _percentile(ordered_clock, 0.95),
            "n_abs_gt_50ms": sum(1 for item in abs_clock if item > 50.0),
            "n_abs_gt_200ms": sum(1 for item in abs_clock if item > 200.0),
            "n_abs_gt_1000ms": sum(1 for item in abs_clock if item > 1000.0),
            "outlier_pairs_abs_gt_50ms": clock_outliers,
            "clock_class_counts_gt_2000_wall": dict(clock_counts),
        },
        "windows_30s": windows,
        "sum_missing_interval_est_windows": sum(
            float(row["missing_interval_est"]) for row in windows
        ),
        "observational_patterns": {
            "quiet_long_gap_identical_five_zero_mut_pm5s": n_quiet,
            "quiet_long_gap_identical_five_zero_mut_pm5s_any_bounds": sum(
                1
                for gap in gaps_1000
                if gap["identical_five"] and int(gap["n_mutation_pm5s"]) == 0
            ),
            "serialized_long_gap_after_3plus_mut_150ms": n_serialized,
            "timer_loss_interval_pairs_gt500ms_with_mutations_between": n_timer_loss_interval,
            "n_gt_1000_changed_last_only": n_last_only,
            "cannot_distinguish": (
                "scheduler vs Chrome timer clamping vs emitChain/IDB backlog: "
                "v1 raw lacks " + ", ".join(MISSING_TELEMETRY)
            ),
        },
        "missing_telemetry": list(MISSING_TELEMETRY),
        "notes": [
            "Mutations do not replace scheduled interval ticks under the v2 heartbeat contract.",
            "missing_interval_est is scheduled minus persisted interval rows, not a drop count.",
            "Matching large wall and monotonic gaps is elapsed time, not proof of sleep.",
        ],
    }


def build_milestone_payload(raw: Path) -> dict[str, Any]:
    analysis = analyze_cadence(raw, expected_sha256=SCORED_CAPTURE_SHA256)
    return {
        "milestone": "MEXC_UI_CAPTURE_CADENCE_FORENSICS_V1",
        "status": "MEXC_UI_CAPTURE_CADENCE_FORENSICS_READY",
        "decision": "STOP_FOR_LEAD_REVIEW",
        "ml_status": "NOT_STARTED",
        "paper": False,
        "live": False,
        "strategy_tuning": False,
        "mom_gap_inspected": False,
        "n_cells_executed": 0,
        "capture_implementation_changed": False,
        "protocol_v2_changed": False,
        "long_capture_started": False,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "capture_sha256": SCORED_CAPTURE_SHA256,
        "analysis": analysis,
        "notes": [
            "Forensics only on the failed v2-contract-gate 7.207 min capture.",
            "Did not change the extension or protocol v2.0.0.",
            "Did not inspect mom/gap formulas or execute any of the 21 cells.",
            "Did not infer scheduled ticks, queue wait, visibility, or IDB append timing.",
        ],
    }


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        if not math.isfinite(value):
            return "n/a"
        return f"{value:.{digits}f}"
    return str(value)


def _stats_line(stats: dict[str, Any]) -> str:
    return (
        f"n={stats.get('n')} min={_fmt(stats.get('min'))} "
        f"p50={_fmt(stats.get('p50'))} p90={_fmt(stats.get('p90'))} "
        f"p95={_fmt(stats.get('p95'))} p99={_fmt(stats.get('p99'))} "
        f"max={_fmt(stats.get('max'))} mean={_fmt(stats.get('mean'))} "
        f">1000={stats.get('n_gt_1000')} >2000={stats.get('n_gt_2000')}"
    )


def render_markdown(payload: dict[str, Any]) -> str:
    a = payload["analysis"]
    clock = a["wall_vs_monotonic"]
    chunk = a["chunk"]
    pat = a["observational_patterns"]
    resume = a["resume"]
    lines = [
        "# MEXC UI capture cadence forensics v1",
        "",
        f"STATUS: `{payload['status']}`",
        "",
        f"DECISION: `{payload['decision']}`",
        "",
        "ML_STATUS: `NOT_STARTED`",
        "",
        "PAPER: **false**",
        "",
        "LIVE: **false**",
        "",
        "STRATEGY_TUNING: **false**",
        "",
        "MOM/GAP: **not inspected** (0 of 21 cells)",
        "",
        "Capture implementation: **unchanged**",
        "",
        "Protocol v2.0.0: **unchanged**",
        "",
        "Long capture: **not started**",
        "",
        "## Purpose",
        "",
        "Explain, as far as persisted RAW allows, why the v2-contract-gate sample",
        "kept **297 interval** snapshots in a ~432.4 s session configured for 500 ms",
        "(about **865** unthrottled scheduled ticks). This is forensics only.",
        "",
        "## Capture lock",
        "",
        f"- sha256: `{a['sha256']}`",
        f"- path: `{a['path']}`",
        f"- snapshots: {a['n_snapshots']}",
        f"- first/last: `{a['first_received_at_local']}` / `{a['last_received_at_local']}`",
        f"- snapshot-span duration_ms / mono_ms: {a['duration_ms']} / {a['duration_mono_ms']}",
        f"- session: `{a['session_started_at']}` → `{a['session_ended_at']}` "
        f"(span_ms={a['session_span_ms']}, interval_ms={a['session_interval_ms']}, "
        f"chunks={a['session_n_chunks']}, status={a['session_status']}, "
        f"storage_error={a['session_storage_error']})",
        f"- trigger_counts: `{a['trigger_counts']}`",
        (
            "- expected scheduled ticks snapshot-span / session-span: "
            f"{_fmt(a['expected_scheduled_ticks_snapshot_span'])} / "
            f"{_fmt(a['expected_scheduled_ticks_session_span'])}"
        ),
        f"- observed interval snapshots: {a['observed_interval_snapshots']}",
        (
            "- missing interval est snapshot-span / session-span: "
            f"{_fmt(a['missing_interval_est_snapshot_span'])} / "
            f"{_fmt(a['missing_interval_est_session_span'])}"
        ),
        "",
        "## Interval-to-interval deltas",
        "",
        "Consecutive **interval** snapshots only (mutations between them are ignored).",
        "",
        f"- wall: {_stats_line(a['interval_to_interval_wall'])}",
        f"- monotonic: {_stats_line(a['interval_to_interval_mono'])}",
        "",
        "## All-trigger consecutive deltas",
        "",
        f"- wall: {_stats_line(a['all_trigger_wall'])}",
        f"- monotonic: {_stats_line(a['all_trigger_mono'])}",
        "",
        "## Long gaps (consecutive snapshots)",
        "",
        f"- n >1000 ms: {a['n_gaps_gt_1000_ms']}",
        f"- n >2000 ms: {a['n_gaps_gt_2000_ms']}",
        f"- identical five-field values among >1000 ms: {a['n_identical_five_among_gt_1000']}",
        f"- any five-field change among >1000 ms: {a['n_changed_five_among_gt_1000']}",
        f"- of those changes, last-only: {a['n_gt_1000_changed_last_only']}",
        f"- >1000 ms clusters: {len(a['clusters_gt_1000_ms'])}",
        f"- >2000 ms clusters: {len(a['clusters_gt_2000_ms'])}",
        "",
        "### Clusters >2000 ms",
        "",
        "| start_seq | end_seq | n_gaps | span_ms | snapshots |",
        "| --- | --- | --- | --- | --- |",
    ]
    for cluster in a["clusters_gt_2000_ms"]:
        lines.append(
            f"| {cluster['start_seq']} | {cluster['end_seq']} | {cluster['n_gaps']} | "
            f"{_fmt(cluster['span_ms'], 1)} | {cluster['n_snapshots_inside']} |"
        )
    lines.extend(
        [
            "",
            "### Every >2000 ms consecutive gap",
            "",
            (
                "| seq | wall_ms | mono_ms | trig_before | trig_after | "
                "chunk | fill | identical_five | clock |"
            ),
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for gap in a["gaps_gt_2000_ms"]:
        lines.append(
            f"| {gap['seq_before']}→{gap['seq_after']} | {_fmt(gap['wall_delta_ms'], 1)} | "
            f"{_fmt(gap['mono_delta_ms'], 1)} | {gap['trigger_before']} | {gap['trigger_after']} | "
            f"{gap['chunk_index_before']} | {gap['chunk_fill_before']} | "
            f"{gap['identical_five']} | {gap['clock_class']} |"
        )
    lines.extend(
        [
            "",
            "Full >1000 ms rows, mutation-density windows, and resume triggers are in the JSON.",
            "",
            "## Resume after long gaps",
            "",
            (
                f"- isolated next interval (±{RESUME_ISOLATED_MS} ms): "
                f"{resume['n_interval_resume_isolated']}"
            ),
            (
                f"- burst (≥2 interval rows within {RESUME_BURST_MS} ms): "
                f"{resume['n_interval_resume_burst']}"
            ),
            "",
            "## Chunk correlation",
            "",
            f"- snapshots by chunk: `{chunk['n_snapshots_by_chunk']}`",
            f"- left bound of >1000 ms gaps by chunk: `{chunk['left_bound_gt_1000_by_chunk']}`",
            f"- left bound of >2000 ms gaps by chunk: `{chunk['left_bound_gt_2000_by_chunk']}`",
            (
                "- expected >1000 left bounds if proportional to chunk size: "
                f"`{chunk['expected_left_gt_1000_proportional']}`"
            ),
            (
                f"- near chunk boundary (first/last {BOUNDARY_NEAR} of 250): "
                f"{chunk['n_gt_1000_near_boundary']} / {a['n_gaps_gt_1000_ms']} "
                f"({_fmt(100.0 * float(chunk['frac_gt_1000_near_boundary']), 1)}%)"
            ),
            "",
            "Random fill would put about 10/250 = 4% of sequences near those edges; "
            "a much higher fraction would suggest IDB chunk-roll correlation.",
            "",
            "## Wall clock vs monotonic",
            "",
            f"- pairs: {clock['n_pairs']}; missing monotonic: {clock['n_missing_mono']}",
            f"- strictly increasing monotonic: {clock['monotonic_strictly_increasing']}",
            f"- nonpositive mono deltas: {clock['n_nonpositive_mono_deltas']}",
            (
                f"- |wall−mono| mean/median/p95: {_fmt(clock['mean_abs_wall_minus_mono_ms'])} / "
                f"{_fmt(clock['median_abs_wall_minus_mono_ms'])} / "
                f"{_fmt(clock['p95_abs_wall_minus_mono_ms'])} ms"
            ),
            (
                f"- |wall−mono| >50 / >200 / >1000 ms: {clock['n_abs_gt_50ms']} / "
                f"{clock['n_abs_gt_200ms']} / {clock['n_abs_gt_1000ms']}"
            ),
            f"- >2000 ms wall clock class counts: `{clock['clock_class_counts_gt_2000_wall']}`",
            "",
            "A large wall gap with a matching monotonic gap is elapsed `performance.now()` time.",
            "Sleep/suspension is **not** inferred from matching clocks. A wall jump would need",
            "a large wall delta with a small monotonic delta. That class is counted above.",
            "",
            "## 30-second windows",
            "",
            (
                "| i | t0 | span_ms | n_all | interval | mutation | "
                "scheduled | missing_est | >1s | >2s |"
            ),
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in a["windows_30s"]:
        lines.append(
            f"| {row['index']} | {row['t0']} | {_fmt(row['span_ms'], 0)} | "
            f"{row['n_all']} | "
            f"{row['n_interval']} | {row['n_mutation']} | "
            f"{_fmt(row['estimated_scheduled_ticks'], 1)} | "
            f"{_fmt(row['missing_interval_est'], 1)} | {row['n_wall_gaps_gt1000']} | "
            f"{row['n_wall_gaps_gt2000']} |"
        )
    lines.extend(
        [
            "",
            "Sum of per-window missing_interval_est: "
            f"{_fmt(a['sum_missing_interval_est_windows'], 1)}",
            "",
            "## Observational patterns (not missing telemetry)",
            "",
            (
                "- quiet-like long gaps (identical five, 0 mutations ±5 s): "
                f"{pat['quiet_long_gap_identical_five_zero_mut_pm5s']}"
            ),
            (
                "- serialized-like (≥3 mutations in 150 ms before gap): "
                f"{pat['serialized_long_gap_after_3plus_mut_150ms']}"
            ),
            (
                "- timer-loss-like (interval-to-interval >500 ms **and** ≥1 mutation between those "
                f"interval rows): {pat['timer_loss_interval_pairs_gt500ms_with_mutations_between']}"
            ),
            "",
            "Quiet market cannot explain missing **interval** rows under the 1.3.3 heartbeat:",
            "unchanged bid/ask/last/mark/index still commit. Mutations in a window prove the",
            "content script was extracting DOM; sparse interval rows in the same window are",
            "therefore not \"nothing to emit\".",
            "",
            "## Findings",
            "",
            "The ~568 missing interval rows are **time-local**, not a session-wide 1000 ms clamp.",
            "Window 0 is near-complete (57/60). Window 11 is exact (60/60). Windows 2–9 persist",
            "only 1–4 interval rows per 30 s while MutationObserver still fires (7–11 mutations).",
            "Later windows do **not** overshoot 60 interval rows, so the missing ticks were not",
            "delivered late: a drained emitChain backlog would have produced a surplus of",
            "interval rows after the sparse period. RAW therefore favors scheduled interval",
            "callbacks that never became persisted snapshots over quiet-market skips or",
            "pure serialization delay.",
            "",
            "Of 68 consecutive gaps >1000 ms, 30 keep identical bid/ask/last/mark/index and",
            "38 change **last only**. Unchanged-field age_ms grows by about the gap length.",
            "",
            "Chunk fill is not a smoking gun. Chunk 2 (seq 501–750) has zero >1000 ms gaps",
            "while holding 250 snapshots. Most long gaps sit in chunk 1 because that is when",
            "the sparse wall-clock period occurred. 9/68 left bounds are near a 250 boundary",
            "(13.2% vs ~4% if uniform); that excess is confounded by the seq 249–252 storm.",
            "",
            "Wall vs monotonic agree to <1 ms p95. All 41 gaps >2000 ms are `elapsed_both`.",
            "RAW does not support a wall-clock jump. Sleep/suspension is not inferred.",
            "",
            "What remains indistinguishable without missing telemetry: Chrome timer clamping",
            "or background throttling, setInterval callbacks never scheduled, versus an",
            "un-telemetred drop before persist. Visibility state is not in the snapshot.",
            "",
            "If every setInterval callback ran, interval rows would still persist even when",
            "emitChain delayed them, unless capturing became false or sendMessage failed",
            "(both would stop the session). This session ended `stopped` "
            "with `storage_error=null`.",
            "",
            "## What v1 RAW cannot infer",
            "",
        ]
    )
    for name in a["missing_telemetry"]:
        lines.append(f"- `{name}`")
    lines.extend(
        [
            "",
            "Do not infer those fields. Queue wait, visibility, and IDB append duration are",
            "absent from schema `mexc_ui_raw_snapshot` v1.",
            "",
            "## Decision",
            "",
            "**STOP_FOR_LEAD_REVIEW.** Do not change capture implementation in this milestone.",
            "Do not amend protocol v2. Do not start the 8–12h corpus. Do not retune mom/gap.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def write_reports(*, raw: Path, out_json: Path, out_md: Path) -> dict[str, Any]:
    payload = build_milestone_payload(raw)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out_md.write_text(render_markdown(payload), encoding="utf-8")
    return payload
