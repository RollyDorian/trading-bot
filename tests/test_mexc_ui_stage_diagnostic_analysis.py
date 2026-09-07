"""Stage-diagnostic analysis on synthetic RAW. Does not use the gitignored capture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trading_bot.research.mexc_shadow.ui_capture.stage_diagnostic_analysis import (
    PHASE_NEAR_NOMINAL,
    ROOT_FIFO,
    ROOT_INCONCLUSIVE,
    ROOT_MIXED,
    ROOT_TIMER,
    SCORED_CAPTURE_SHA256,
    ShaMismatchError,
    analyze_stage_diagnostics,
    classify_gap,
    reconstruct_phases,
)


def _dump(path: Path, objects: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for obj in objects:
            handle.write(
                json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                + "\n"
            )


def _lifecycle(
    *,
    timer: float = 1000.0,
    hidden: float = 4000.0,
    shown: float = 16000.0,
    stop: float = 22000.0,
    omit_hidden: bool = False,
    extra_visibility: bool = False,
) -> list[dict[str, Any]]:
    events = [
        {"kind": "worker_boot", "mono": 50.0, "worker_boot_id": "worker-a"},
        {
            "kind": "timer_registered",
            "mono": timer,
            "to_state": "visible",
            "session_generation": 1,
        },
    ]
    if not omit_hidden:
        events.append(
            {
                "kind": "visibilitychange",
                "mono": hidden,
                "from_state": "visible",
                "to_state": "hidden",
            }
        )
        events.append(
            {
                "kind": "visibilitychange",
                "mono": shown,
                "from_state": "hidden",
                "to_state": "visible",
            }
        )
    if extra_visibility:
        events.append(
            {
                "kind": "visibilitychange",
                "mono": shown + 10.0,
                "from_state": "visible",
                "to_state": "hidden",
            }
        )
    events.append(
        {"kind": "session_stop", "mono": stop, "to_state": "visible", "session_generation": 1}
    )
    return events


def _snap(
    sequence: int,
    *,
    trigger: str,
    callback_mono: float,
    wait_ms: float = 1.0,
    extract_ms: float = 20.0,
    depth: int = 0,
    vis: str = "visible",
    interval_ordinal: int | None = None,
    slot: int | None = None,
    delay_ms: float | None = None,
) -> dict[str, Any]:
    extract_end = callback_mono + wait_ms + extract_ms
    diag: dict[str, Any] = {
        "request_ordinal": sequence,
        "trigger": trigger,
        "callback_mono": callback_mono,
        "content_queue_wait_ms": wait_ms,
        "content_queue_depth": depth,
        "extract_duration_ms": extract_ms,
        "extract_end_mono": extract_end,
        "background_queue_wait_ms": 0.2,
        "background_queue_depth": 0,
        "visibility_state": vis,
        "producer_epoch": "producer-a",
        "session_generation": 1,
        "stale_generation": False,
        "session_id_mismatch": False,
        "worker_boot_id": "worker-a",
        "append_timings": {
            "meta_lookup_ms": 0.5,
            "db_open_ms": 0.2,
            "session_read_ms": 0.1,
            "chunk_read_ms": 1.0,
            "stringify_put_ms": 2.0,
            "tx_wait_ms": 3.0,
        },
    }
    if interval_ordinal is not None:
        diag["interval_callback_ordinal"] = interval_ordinal
        diag["elapsed_ideal_slot_ordinal"] = slot
        diag["callback_delay_ms"] = delay_ms
        diag["expected_deadline_mono"] = 1000.0 + interval_ordinal * 500.0
    return {
        "schema": "mexc_ui_raw_snapshot",
        "schema_version": 1,
        "sequence": sequence,
        "received_at_local": f"2026-09-07T16:00:00.{sequence:03d}Z",
        "observed_at_local": f"2026-09-07T16:00:00.{sequence:03d}Z",
        "monotonic_ms": extract_end,
        "trigger": trigger,
        "selector_catalog_version": "v1.2",
        "page_host": "www.mexc.com",
        "page_path": "/futures/TAO_USDT",
        "observation_valid": True,
        "invalid_reasons": [],
        "changed_fields": [],
        "fields": {},
        "stage_diagnostics": diag,
    }


def _session(lifecycle: list[dict[str, Any]], n_snapshots: int) -> list[dict[str, Any]]:
    start = {
        "record_type": "session_start",
        "schema": "mexc_ui_capture_session",
        "schema_version": 1,
        "session_id": "synthetic-stage-analysis",
        "started_at": "2026-09-07T16:00:00.000Z",
        "interval_ms": 500,
        "page_path": "/futures/TAO_USDT",
        "extension_version": "1.3.4",
        "diagnostic_format_version": 1,
        "status": "running",
    }
    end = {
        "record_type": "session_end",
        "schema": "mexc_ui_capture_session",
        "schema_version": 1,
        "session_id": "synthetic-stage-analysis",
        "started_at": "2026-09-07T16:00:00.000Z",
        "ended_at": "2026-09-07T16:00:22.000Z",
        "interval_ms": 500,
        "page_path": "/futures/TAO_USDT",
        "extension_version": "1.3.4",
        "diagnostic_format_version": 1,
        "status": "stopped",
        "n_snapshots": n_snapshots,
        "stage_diagnostic_summary": {
            "lifecycle": lifecycle,
            "counters": {
                "timer_callbacks": 0,
                "outstanding_at_stop": 0,
                "abandoned": {"interval": 0, "mutation": 0, "manual": 0},
                "acked": {"interval": 0, "mutation": 0, "manual": 0},
                "callbacks_enqueued": {"interval": 0, "mutation": 0, "manual": 0},
                "tasks_started": {"interval": 0, "mutation": 0, "manual": 0},
                "snapshots_persisted": {"interval": 0, "mutation": 0, "manual": 0},
                "diagnostic_truncated": False,
            },
            "high_water": {
                "content_queue_depth": 0,
                "background_queue_depth": 0,
                "content_oldest_wait_ms": 0,
            },
            "histograms": {},
            "producer_epochs": ["producer-a"],
            "worker_boot_ids": ["worker-a"],
            "interval_details": [],
            "mutation_details": [],
            "completions": [],
        },
    }
    return [start, end]


def test_sha_mismatch_is_fail_closed(tmp_path: Path) -> None:
    raw = tmp_path / "one.ndjson"
    lifecycle = _lifecycle()
    snaps = [_snap(1, trigger="interval", callback_mono=1500.0, interval_ordinal=1, slot=1)]
    start, end = _session(lifecycle, 1)
    _dump(raw, [start, *snaps, end])
    with pytest.raises(ShaMismatchError):
        analyze_stage_diagnostics(raw, expected_sha256=SCORED_CAPTURE_SHA256)


def test_reconstruct_uses_lifecycle_not_intended_2_6_4() -> None:
    # 150 ms / 150 ms / 100 ms — not 2/6/4 minutes.
    scored = reconstruct_phases(
        [
            {"kind": "timer_registered", "mono": 100.0, "to_state": "visible"},
            {
                "kind": "visibilitychange",
                "mono": 250.0,
                "from_state": "visible",
                "to_state": "hidden",
            },
            {
                "kind": "visibilitychange",
                "mono": 400.0,
                "from_state": "hidden",
                "to_state": "visible",
            },
            {"kind": "session_stop", "mono": 500.0, "to_state": "visible"},
        ]
    )
    assert scored["reconstructed"] is True
    phases = scored["phases"]
    assert phases is not None
    assert phases["initial_visible"]["duration_ms"] == 150.0
    assert phases["hidden"]["duration_ms"] == 150.0
    assert phases["final_visible"]["duration_ms"] == 100.0
    assert phases["initial_visible"]["duration_ms"] != 120_000.0


def test_missing_hidden_is_inconclusive() -> None:
    scored = reconstruct_phases(_lifecycle(omit_hidden=True))
    assert scored["reconstructed"] is False
    assert scored["status"] == ROOT_INCONCLUSIVE
    assert "missing_visible_to_hidden" in scored["reasons"]


def test_extra_visibilitychange_is_inconclusive() -> None:
    scored = reconstruct_phases(_lifecycle(extra_visibility=True))
    assert scored["reconstructed"] is False
    assert "extra_visibilitychange_events" in scored["reasons"]


def test_timer_gap_not_fifo() -> None:
    prev = {
        "trigger": "interval",
        "callback_mono": 5000.0,
        "elapsed_ideal_slot_ordinal": 8,
        "content_queue_wait_ms": 1.0,
        "extract_duration_ms": 20.0,
    }
    row = {
        "trigger": "interval",
        "callback_mono": 6050.0,
        "elapsed_ideal_slot_ordinal": 10,
        "content_queue_wait_ms": 1.0,
        "extract_duration_ms": 20.0,
        "background_queue_wait_ms": 0.2,
        "append_duration_ms": 5.0,
        "total_ack_latency_ms": 8.0,
    }
    attr = classify_gap(row, 1050.0, prev, prev)
    assert attr["primary"] == "delayed_or_missing_timer_callback"
    assert "content_fifo_waiting" not in attr["causes"]


def test_fifo_gap_not_timer() -> None:
    prev = {
        "trigger": "interval",
        "callback_mono": 17000.0,
        "elapsed_ideal_slot_ordinal": 32,
        "content_queue_wait_ms": 10.0,
    }
    row = {
        "trigger": "interval",
        "callback_mono": 17510.0,
        "elapsed_ideal_slot_ordinal": 33,
        "content_queue_wait_ms": 1400.0,
        "extract_duration_ms": 30.0,
        "background_queue_wait_ms": 0.2,
        "append_duration_ms": 6.0,
    }
    attr = classify_gap(row, 1450.0, prev, prev)
    assert attr["primary"] == "content_fifo_waiting"
    assert "delayed_or_missing_timer_callback" not in attr["causes"]


def _mixed_capture(tmp_path: Path) -> Path:
    """Visible 500 ms ticks, hidden 1000 ms ticks, final visible FIFO wait."""

    lifecycle = _lifecycle()
    rows: list[dict[str, Any]] = []
    seq = 1
    interval_ordinal = 0
    # initial visible: 500 ms callbacks, small wait
    for callback in range(1500, 4000, 500):
        interval_ordinal += 1
        rows.append(
            _snap(
                seq,
                trigger="interval",
                callback_mono=float(callback),
                wait_ms=5.0,
                depth=1,
                vis="visible",
                interval_ordinal=interval_ordinal,
                slot=interval_ordinal,
                delay_ms=20.0,
            )
        )
        seq += 1
    # hidden: ~1 Hz, small wait
    for callback in range(4500, 16000, 1000):
        interval_ordinal += 1
        slot = (callback - 1000) // 500
        rows.append(
            _snap(
                seq,
                trigger="interval",
                callback_mono=float(callback),
                wait_ms=2.0,
                depth=1,
                vis="hidden",
                interval_ordinal=interval_ordinal,
                slot=slot,
                delay_ms=float(callback - (1000 + interval_ordinal * 500)),
            )
        )
        seq += 1
    # final visible: 500 ms callbacks, content FIFO
    for callback in range(16500, 22000, 500):
        interval_ordinal += 1
        slot = (callback - 1000) // 500
        rows.append(
            _snap(
                seq,
                trigger="interval",
                callback_mono=float(callback),
                wait_ms=1200.0,
                depth=12,
                vis="visible",
                interval_ordinal=interval_ordinal,
                slot=slot,
                delay_ms=8000.0,
            )
        )
        seq += 1
        rows.append(
            _snap(
                seq,
                trigger="mutation",
                callback_mono=float(callback) + 80.0,
                wait_ms=1100.0,
                depth=12,
                vis="visible",
            )
        )
        seq += 1
    start, end = _session(lifecycle, len(rows))
    path = tmp_path / "mixed.ndjson"
    _dump(path, [start, *rows, end])
    return path


def test_mixed_cause_from_hidden_timer_and_visible_fifo(tmp_path: Path) -> None:
    scored = analyze_stage_diagnostics(_mixed_capture(tmp_path), expected_sha256=None)
    assert scored["root_cause"] == ROOT_MIXED
    assert scored["phase_root_cause"]["hidden"] == ROOT_TIMER
    assert scored["phase_root_cause"]["final_visible"] == ROOT_FIFO
    assert scored["phase_root_cause"]["initial_visible"] == PHASE_NEAR_NOMINAL
    assert scored["protocol_v2_can_remain_unchanged"] is True
    assert scored["capture_implementation_changed"] is False
    assert scored["mom_gap_inspected"] is False
    assert scored["content_vs_worker_monotonic_subtracted"] is False
    durations = {
        name: scored["phases"][name]["duration_ms"] for name in scored["phases"]
    }
    assert durations["initial_visible"] == 3000.0
    assert durations["hidden"] == 12000.0
    assert durations["final_visible"] == 6000.0
    assert durations["hidden"] != 360_000.0


def test_analyze_inconclusive_without_hide(tmp_path: Path) -> None:
    lifecycle = _lifecycle(omit_hidden=True)
    rows = [
        _snap(1, trigger="interval", callback_mono=1500.0, interval_ordinal=1, slot=1)
    ]
    start, end = _session(lifecycle, 1)
    raw = tmp_path / "nohide.ndjson"
    _dump(raw, [start, *rows, end])
    scored = analyze_stage_diagnostics(raw, expected_sha256=None)
    assert scored["root_cause"] == ROOT_INCONCLUSIVE
    assert scored["status"] == ROOT_INCONCLUSIVE
    assert "missing_visible_to_hidden" in scored["inconclusive_reasons"]
