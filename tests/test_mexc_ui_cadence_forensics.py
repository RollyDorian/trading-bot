"""Cadence forensics on synthetic RAW. Does not use the gitignored capture."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trading_bot.research.mexc_shadow.ui_capture.cadence_forensics import (
    SCORED_CAPTURE_SHA256,
    ShaMismatchError,
    analyze_cadence,
)
from trading_bot.research.mexc_shadow.ui_capture.extract import extract_html

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "mexc_ui_capture"
BASE = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
INTERVAL_MS = 500


def _dump(path: Path, objects: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for obj in objects:
            handle.write(
                json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                + "\n"
            )


def _base() -> dict[str, Any]:
    snap = extract_html(
        (FIXTURES / "tao_live_wrappers.html").read_text(encoding="utf-8"),
        received_at_local=BASE.isoformat(),
        sequence=1,
        page_path="/futures/TAO_USDT",
        trigger="interval",
        sample_interval_ms=INTERVAL_MS,
        monotonic_ms=0.0,
        capture_id="forensics-test",
    )
    return snap.as_dict()


def _clone(
    base: dict[str, Any],
    *,
    sequence: int,
    wall_ms: int,
    mono_ms: float | None = None,
    trigger: str = "interval",
) -> dict[str, Any]:
    row: dict[str, Any] = json.loads(json.dumps(base))
    stamp = (BASE + timedelta(milliseconds=wall_ms)).isoformat()
    row["sequence"] = sequence
    row["received_at_local"] = stamp
    row["observed_at_local"] = stamp
    row["monotonic_ms"] = float(wall_ms if mono_ms is None else mono_ms)
    row["trigger"] = trigger
    row["sample_interval_ms"] = INTERVAL_MS
    row["capture_id"] = "forensics-test"
    return row


def test_sha_mismatch_is_fail_closed(tmp_path: Path) -> None:
    raw = tmp_path / "one.ndjson"
    _dump(raw, [_clone(_base(), sequence=1, wall_ms=0), _clone(_base(), sequence=2, wall_ms=500)])
    with pytest.raises(ShaMismatchError):
        analyze_cadence(raw, expected_sha256=SCORED_CAPTURE_SHA256)


def test_regular_500ms_interval_has_no_long_gaps(tmp_path: Path) -> None:
    raw = tmp_path / "regular.ndjson"
    base = _base()
    rows = [_clone(base, sequence=index + 1, wall_ms=index * INTERVAL_MS) for index in range(8)]
    _dump(raw, rows)
    scored = analyze_cadence(raw, expected_sha256=None)
    assert scored["n_snapshots"] == 8
    assert scored["n_gaps_gt_1000_ms"] == 0
    assert scored["interval_to_interval_wall"]["p50"] == 500.0
    assert scored["interval_to_interval_wall"]["max"] == 500.0
    assert scored["wall_vs_monotonic"]["n_abs_gt_50ms"] == 0
    assert scored["observed_interval_snapshots"] == 8


def test_3000ms_gap_is_listed_with_triggers(tmp_path: Path) -> None:
    raw = tmp_path / "gap.ndjson"
    base = _base()
    rows = [
        _clone(base, sequence=1, wall_ms=0, trigger="interval"),
        _clone(base, sequence=2, wall_ms=500, trigger="mutation"),
        _clone(base, sequence=3, wall_ms=3500, trigger="interval"),
        _clone(base, sequence=4, wall_ms=4000, trigger="interval"),
    ]
    _dump(raw, rows)
    scored = analyze_cadence(raw, expected_sha256=None)
    assert scored["n_gaps_gt_1000_ms"] == 1
    assert scored["n_gaps_gt_2000_ms"] == 1
    gap = scored["gaps_gt_2000_ms"][0]
    assert gap["seq_before"] == 2
    assert gap["seq_after"] == 3
    assert gap["trigger_before"] == "mutation"
    assert gap["trigger_after"] == "interval"
    assert gap["wall_delta_ms"] == 3000.0
    assert gap["clock_class"] == "elapsed_both"
    assert scored["clusters_gt_2000_ms"][0]["n_gaps"] == 1


def test_wall_jump_when_monotonic_stays_small(tmp_path: Path) -> None:
    raw = tmp_path / "jump.ndjson"
    base = _base()
    rows = [
        _clone(base, sequence=1, wall_ms=0, mono_ms=0.0, trigger="mutation"),
        _clone(base, sequence=2, wall_ms=5000, mono_ms=500.0, trigger="mutation"),
    ]
    _dump(raw, rows)
    scored = analyze_cadence(raw, expected_sha256=None)
    gap = scored["gaps_gt_2000_ms"][0]
    assert gap["clock_class"] == "wall_jump"
    assert scored["wall_vs_monotonic"]["n_abs_gt_1000ms"] == 1


def test_window_missing_interval_estimate(tmp_path: Path) -> None:
    raw = tmp_path / "windows.ndjson"
    base = _base()
    # 30 s span, only two interval ticks at 0 and 30000.
    rows = [
        _clone(base, sequence=1, wall_ms=0, trigger="interval"),
        _clone(base, sequence=2, wall_ms=30_000, trigger="interval"),
    ]
    _dump(raw, rows)
    scored = analyze_cadence(raw, expected_sha256=None)
    assert scored["windows_30s"][0]["n_interval"] == 2
    # One 30s window: 30000/500=60 scheduled, 2 observed, missing 58.
    assert scored["windows_30s"][0]["missing_interval_est"] == 58.0
    assert scored["missing_telemetry"] == [
        "scheduled_tick_monotonic",
        "queue_wait_ms",
        "queue_depth",
        "visibility_state",
        "background_message_receipt_time",
        "idb_append_start_ms",
        "idb_append_end_ms",
    ]
