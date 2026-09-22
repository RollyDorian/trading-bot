"""Interval-only visible v2 gate. Does not run mom/gap cells."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trading_bot.research.mexc_shadow.ui_capture.durable import (
    DEFAULT_CHUNK_SIZE,
    SessionMeta,
    session_end_record,
    session_start_record,
)
from trading_bot.research.mexc_shadow.ui_capture.extract import extract_html
from trading_bot.research.mexc_shadow.ui_capture.interval_only_visible_v2_gate import (
    EXTENSION_EXPECTED,
    MILESTONE_FAIL,
    MILESTONE_PASS,
    PRIOR_CONTENT_QUEUE_DEPTH_HIGH_WATER,
    PRIOR_CONTENT_WAIT_P95_MS,
    score_interval_only_visible_v2_gate,
    write_reports,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "mexc_ui_capture"
BASE = datetime(2026, 9, 7, 18, 0, tzinfo=UTC)
INTERVAL_MS = 500
DENSE_DURATION_MS = 5 * 60 * 1000
DENSE_N = DENSE_DURATION_MS // INTERVAL_MS + 1


def _html() -> str:
    return (FIXTURES / "tao_logged_in_ru_header_probe.html").read_text(encoding="utf-8")


def _dump(path: Path, objects: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for obj in objects:
            handle.write(
                json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                + "\n"
            )


def _diag(*, sequence: int, trigger: str, vis: str = "visible") -> dict[str, Any]:
    return {
        "diagnostic_format_version": 1,
        "extension_version": EXTENSION_EXPECTED,
        "request_ordinal": sequence,
        "trigger": trigger,
        "visibility_state": vis,
        "visibility_state_at_extract": vis,
        "stale_generation": False,
        "session_id_mismatch": False,
        "content_queue_wait_ms": 0.1,
        "content_queue_depth": 0,
        "extract_duration_ms": 70.0,
        "background_queue_wait_ms": 0.2,
        "background_queue_depth": 0,
        "callback_delay_ms": 2.0 if trigger == "interval" else None,
        "interval_callback_ordinal": sequence - 1 if trigger == "interval" else None,
        "mutation_callback_ordinal": 4 if trigger == "interval" else None,
        "dirty_since_last_interval": trigger == "interval",
    }


def _base_snapshot() -> dict[str, Any]:
    snap = extract_html(
        _html(),
        received_at_local=BASE.isoformat(),
        sequence=1,
        page_path="/ru-RU/futures/TAO_USDT",
        page_host="www.mexc.com",
        trigger="interval",
        sample_interval_ms=INTERVAL_MS,
        monotonic_ms=0.0,
        capture_id="visible-v2-gate-test",
    )
    return snap.as_dict()


def _clone(
    base: dict[str, Any],
    *,
    sequence: int,
    offset_ms: int,
    trigger: str = "interval",
    vis: str = "visible",
) -> dict[str, Any]:
    row: dict[str, Any] = json.loads(json.dumps(base))
    stamp = (BASE + timedelta(milliseconds=offset_ms)).isoformat()
    row["sequence"] = sequence
    row["received_at_local"] = stamp
    row["observed_at_local"] = stamp
    row["monotonic_ms"] = float(offset_ms)
    row["trigger"] = trigger
    row["sample_interval_ms"] = INTERVAL_MS
    row["capture_id"] = "visible-v2-gate-test"
    row["stage_diagnostics"] = _diag(sequence=sequence, trigger=trigger, vis=vis)
    return row


def _summary(*, n_snapshots: int, duration_ms: int, hidden: bool = False) -> dict[str, Any]:
    n_interval = max(0, n_snapshots - 1)
    lifecycle: list[dict[str, Any]] = [
        {"kind": "timer_registered", "mono": 0.0, "to_state": "visible"},
    ]
    if hidden:
        lifecycle.append(
            {
                "kind": "visibilitychange",
                "mono": 1000.0,
                "from_state": "visible",
                "to_state": "hidden",
            }
        )
    lifecycle.append(
        {
            "kind": "session_stop",
            "mono": float(duration_ms),
            "to_state": "visible" if not hidden else "hidden",
        }
    )
    return {
        "counters": {
            "expected_interval_opportunities": n_interval,
            "timer_callbacks": n_interval,
            "mutation_callbacks": 40,
            "mutation_dirty_sets": 1,
            "manual_callbacks": 1,
            "callbacks_enqueued": {
                "interval": n_interval,
                "mutation": 0,
                "manual": 1,
            },
            "snapshots_persisted": {
                "interval": n_interval,
                "mutation": 0,
                "manual": 1,
            },
            "stale_generation_detected": 0,
            "session_id_mismatch_detected": 0,
            "outstanding_at_stop": 0,
        },
        "high_water": {"content_queue_depth": 0, "background_queue_depth": 0},
        "lifecycle": lifecycle,
        "histograms": {},
        "mutation_details": [],
    }


def _session_pair(
    *, n_snapshots: int, duration_ms: int, hidden: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    meta = SessionMeta(
        session_id="visible-v2-gate-test",
        started_at=BASE.isoformat(),
        interval_ms=INTERVAL_MS,
        page_host="www.mexc.com",
        page_path="/ru-RU/futures/TAO_USDT",
        ended_at=(BASE + timedelta(milliseconds=duration_ms)).isoformat(),
        status="stopped",
        n_snapshots=n_snapshots,
        n_chunks=math.ceil(n_snapshots / DEFAULT_CHUNK_SIZE) if n_snapshots else 0,
        first_sequence=1 if n_snapshots else None,
        last_sequence=n_snapshots if n_snapshots else None,
        chunk_size=DEFAULT_CHUNK_SIZE,
        storage_error=None,
        extension_version=EXTENSION_EXPECTED,
        diagnostic_format_version=1,
        stage_diagnostic_summary=_summary(
            n_snapshots=n_snapshots, duration_ms=duration_ms, hidden=hidden
        ),
    )
    return session_start_record(meta), session_end_record(meta)


def _write_dense(path: Path) -> None:
    base = _base_snapshot()
    start, end = _session_pair(n_snapshots=DENSE_N, duration_ms=DENSE_DURATION_MS)
    rows: list[dict[str, Any]] = [start]
    for index in range(DENSE_N):
        trigger = "interval" if index else "manual"
        rows.append(
            _clone(
                base,
                sequence=index + 1,
                offset_ms=index * INTERVAL_MS,
                trigger=trigger,
            )
        )
    rows.append(end)
    _dump(path, rows)


def test_prior_fifo_constants_are_the_1_3_4_final_visible_evidence() -> None:
    assert PRIOR_CONTENT_WAIT_P95_MS == 1525.699999988079
    assert PRIOR_CONTENT_QUEUE_DEPTH_HIGH_WATER == 26


def test_dense_interval_only_visible_sample_passes_frozen_v2_bars(tmp_path: Path) -> None:
    raw = tmp_path / "dense.ndjson"
    _write_dense(raw)
    scored = score_interval_only_visible_v2_gate(raw)
    assert scored["gates"]["duration_5_to_15_min"] is True
    assert scored["gates"]["catalog_v1_2"] is True
    assert scored["gates"]["configured_interval_500_ms"] is True
    assert scored["gates"]["frozen_interarrival_p95"] is True
    assert scored["gates"]["frozen_interarrival_le_2000"] is True
    assert scored["gates"]["extension_1_3_5"] is True
    assert scored["gates"]["exactly_one_manual_start_raw_row"] is True
    assert scored["gates"]["remaining_raw_rows_interval"] is True
    assert scored["gates"]["zero_mutation_raw_rows"] is True
    assert scored["gates"]["mutation_callbacks_not_enqueued"] is True
    assert scored["gates"]["continuous_visibility_visible"] is True
    assert scored["gates"]["zero_visible_to_hidden"] is True
    assert scored["gates"]["heartbeat_expected_equals_timer_callbacks"] is True
    assert scored["gates"]["heartbeat_persisted_equals_timer_callbacks"] is True
    assert scored["gates"]["no_stale_generation"] is True
    assert scored["gates"]["no_session_id_mismatch"] is True
    assert scored["gates"]["screenshot_agreement"] is None
    assert scored["passed"] is True
    assert scored["failure_classes"] == []
    assert scored["mom_gap_inspected"] is False
    assert scored["bad_periods_cropped"] is False
    assert scored["fifo_before_after"]["improvement_percent_required"] is False


def test_mutation_raw_row_fails_zero_mutation_gate(tmp_path: Path) -> None:
    raw = tmp_path / "mutation.ndjson"
    base = _base_snapshot()
    start, end = _session_pair(n_snapshots=3, duration_ms=6 * 60_000)
    rows = [
        start,
        _clone(base, sequence=1, offset_ms=0, trigger="manual"),
        _clone(base, sequence=2, offset_ms=500, trigger="mutation"),
        _clone(base, sequence=3, offset_ms=6 * 60_000, trigger="interval"),
        end,
    ]
    _dump(raw, rows)
    scored = score_interval_only_visible_v2_gate(raw)
    assert scored["gates"]["zero_mutation_raw_rows"] is False
    assert scored["gates"]["remaining_raw_rows_interval"] is False
    assert "zero_mutation_raw_rows" in scored["failure_classes"]
    assert scored["passed"] is False


def test_visible_to_hidden_fails_visibility_gates(tmp_path: Path) -> None:
    raw = tmp_path / "hidden.ndjson"
    base = _base_snapshot()
    start, end = _session_pair(n_snapshots=2, duration_ms=6 * 60_000, hidden=True)
    rows = [
        start,
        _clone(base, sequence=1, offset_ms=0, trigger="manual", vis="visible"),
        _clone(base, sequence=2, offset_ms=6 * 60_000, trigger="interval", vis="hidden"),
        end,
    ]
    _dump(raw, rows)
    scored = score_interval_only_visible_v2_gate(raw)
    assert scored["gates"]["continuous_visibility_visible"] is False
    assert scored["gates"]["zero_visible_to_hidden"] is False
    assert "continuous_visibility_visible" in scored["failure_classes"]
    assert "zero_visible_to_hidden" in scored["failure_classes"]
    assert scored["passed"] is False


def test_report_writes_fail_status_on_sparse_sample(tmp_path: Path) -> None:
    raw = tmp_path / "sparse.ndjson"
    base = _base_snapshot()
    start, end = _session_pair(n_snapshots=2, duration_ms=6 * 60_000)
    _dump(
        raw,
        [
            start,
            _clone(base, sequence=1, offset_ms=0, trigger="manual"),
            _clone(base, sequence=2, offset_ms=6 * 60_000, trigger="interval"),
            end,
        ],
    )
    out_json = tmp_path / "gate.json"
    out_md = tmp_path / "gate.md"
    payload = write_reports(raw=raw, out_json=out_json, out_md=out_md)
    assert payload["status"] == MILESTONE_FAIL
    assert payload["decision"] == "STOP_FOR_LEAD_REVIEW"
    assert payload["mom_gap_inspected"] is False
    assert payload["long_capture_started"] is False
    text = out_md.read_text(encoding="utf-8")
    assert "VISIBLE_FINAL_V2_GATE_FAIL" in text
    assert "Do not start the 8–12h corpus" in text


def test_dense_report_can_pass_without_bound_screenshots(tmp_path: Path) -> None:
    raw = tmp_path / "dense.ndjson"
    _write_dense(raw)
    out_json = tmp_path / "gate.json"
    out_md = tmp_path / "gate.md"
    payload = write_reports(raw=raw, out_json=out_json, out_md=out_md)
    assert payload["status"] == MILESTONE_PASS
    assert payload["short_validation"]["gates"]["screenshot_agreement"] is None
    text = out_md.read_text(encoding="utf-8")
    assert "VISIBLE_FINAL_V2_GATE_PASS" in text
    assert "final-visible" in text
