"""Corrected long TAOUSDT corpus admissibility. Does not run mom/gap cells."""

from __future__ import annotations

import ast
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
from trading_bot.research.mexc_shadow.ui_capture.long_corpus_admissibility import (
    EXTENSION_EXPECTED,
    STATUS_INADEQUATE,
    CompactRow,
    hash_grid_rows,
    materialize_causal_grid,
    score_long_corpus_admissibility,
    split_usable_segments,
    usable_hours_from_segments,
    write_reports,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "mexc_ui_capture"
MODULE = (
    REPO
    / "src"
    / "trading_bot"
    / "research"
    / "mexc_shadow"
    / "ui_capture"
    / "long_corpus_admissibility.py"
)
BASE = datetime(2026, 9, 21, 6, 0, tzinfo=UTC)
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
        "producer_epoch": "test-epoch",
        "worker_boot_id": "test-worker",
        "content_queue_wait_ms": 0.1,
        "content_queue_depth": 0,
        "extract_duration_ms": 70.0,
        "background_queue_wait_ms": 0.2,
        "background_queue_depth": 0,
        "callback_delay_ms": 2.0 if trigger == "interval" else None,
        "interval_callback_ordinal": sequence - 1 if trigger == "interval" else None,
        "dirty_since_last_interval": trigger == "interval",
    }


def _summary(*, n_snapshots: int, duration_ms: int, hidden: bool = False) -> dict[str, Any]:
    n_interval = max(0, n_snapshots - 1)
    lifecycle: list[dict[str, Any]] = [
        {"kind": "timer_registered", "mono": 0.0, "to_state": "visible"},
        {"kind": "session_start", "mono": 0.0},
        {"kind": "session_stop", "mono": float(duration_ms)},
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
    return {
        "schema": "mexc_ui_stage_diagnostics_summary",
        "diagnostic_format_version": 1,
        "extension_version": EXTENSION_EXPECTED,
        "counters": {
            "expected_interval_opportunities": n_interval,
            "timer_callbacks": n_interval,
            "mutation_callbacks": 0,
            "mutation_dirty_sets": 0,
            "manual_callbacks": 1 if n_snapshots else 0,
            "callbacks_enqueued": {
                "manual": 1 if n_snapshots else 0,
                "interval": n_interval,
                "mutation": 0,
            },
            "snapshots_persisted": {
                "manual": 1 if n_snapshots else 0,
                "interval": n_interval,
                "mutation": 0,
            },
            "stale_generation_detected": 0,
            "session_id_mismatch_detected": 0,
            "outstanding_at_stop": 0,
        },
        "high_water": {"content_queue_depth": 0},
        "histograms": {},
        "lifecycle": lifecycle,
        "producer_epochs": ["test-epoch"],
        "worker_boot_ids": ["test-worker"],
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
        capture_id="long-admiss-test",
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
    row["capture_id"] = "long-admiss-test"
    row["stage_diagnostics"] = _diag(sequence=sequence, trigger=trigger, vis=vis)
    return row


def _session_pair(
    *, n_snapshots: int, duration_ms: int, hidden: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    meta = SessionMeta(
        session_id="long-admiss-test",
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
        producer_epoch="test-epoch",
        session_generation=1,
        worker_boot_id="test-worker",
        extension_version=EXTENSION_EXPECTED,
        stage_diagnostic_summary=_summary(
            n_snapshots=n_snapshots, duration_ms=duration_ms, hidden=hidden
        ),
    )
    return session_start_record(meta), session_end_record(meta)


def _write_dense(
    path: Path,
    *,
    n: int,
    extra_mutation: bool = False,
    hidden: bool = False,
) -> None:
    base = _base_snapshot()
    duration_ms = (n - 1) * INTERVAL_MS
    start, end = _session_pair(n_snapshots=n, duration_ms=duration_ms, hidden=hidden)
    rows: list[dict[str, Any]] = [start]
    for index in range(n):
        trigger = "interval" if index else "manual"
        vis = "hidden" if hidden and index == n // 2 else "visible"
        rows.append(
            _clone(
                base,
                sequence=index + 1,
                offset_ms=index * INTERVAL_MS,
                trigger=trigger,
                vis=vis,
            )
        )
    if extra_mutation:
        rows.append(
            _clone(base, sequence=n + 1, offset_ms=n * INTERVAL_MS, trigger="mutation")
        )
        end = _session_pair(n_snapshots=n + 1, duration_ms=n * INTERVAL_MS)[1]
    rows.append(end)
    _dump(path, rows)


def _compact(
    *,
    received_ms: float,
    sequence: int,
    session_id: str = "s",
    five_ok: bool = True,
    as_of_eligible: bool = True,
) -> CompactRow:
    return CompactRow(
        received_ms=received_ms,
        received_at="2026-09-21T00:00:00Z",
        sequence=sequence,
        session_id=session_id,
        trigger="interval",
        as_of_eligible=as_of_eligible,
        five_ok=five_ok,
        bid=100.0 if five_ok else None,
        ask=100.1 if five_ok else None,
        last=100.05 if five_ok else None,
        mark=100.05 if five_ok else None,
        index=100.04 if five_ok else None,
    )


def test_module_does_not_import_mom_gap_or_replay() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "signal" not in alias.name
                assert "features" not in alias.name
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "signal" not in node.module
            assert ".features" not in node.module
            assert node.module.endswith("replay") is False
            assert "hypothesis" not in node.module


def test_usable_hours_count_contiguous_time_after_gaps() -> None:
    start = 1_000_000.0
    step_ms = 1000.0
    n_continuous = int(8.1 * 3600) + 1
    continuous = [
        _compact(received_ms=start + index * step_ms, sequence=index + 1)
        for index in range(n_continuous)
    ]
    hours = usable_hours_from_segments(split_usable_segments(continuous))
    assert hours >= 8.0
    n_left = int(4.0 * 3600) + 1
    gap_ms = 400_000.0
    n_right = int(3.5 * 3600) + 1
    gapped = [
        _compact(received_ms=start + index * step_ms, sequence=index + 1)
        for index in range(n_left)
    ]
    right_start = start + (n_left - 1) * step_ms + gap_ms
    gapped.extend(
        _compact(
            received_ms=right_start + index * step_ms,
            sequence=n_left + index + 1,
        )
        for index in range(n_right)
    )
    gapped_hours = usable_hours_from_segments(split_usable_segments(gapped))
    assert gapped_hours < 8.0
    assert gapped_hours < hours


def test_causal_grid_as_of_and_lag_bound() -> None:
    rows = [
        _compact(received_ms=0.0, sequence=1),
        _compact(received_ms=500.0, sequence=2),
    ]
    grid = materialize_causal_grid(rows)
    assert grid[0]["t_ms"] == 0
    assert grid[0]["seq"] == 1
    assert grid[0]["five_ok"] is True
    late = [
        _compact(received_ms=0.0, sequence=1),
        _compact(received_ms=500.0, sequence=2),
        _compact(received_ms=3000.0, sequence=3),
    ]
    split = split_usable_segments(late)
    assert len(split) == 2
    grid_late = materialize_causal_grid(late)
    # Tick 2000 sits in the excluded gap, so it is not emitted.
    ticks = [row["t_ms"] for row in grid_late]
    assert 2000 not in ticks
    assert 3000 in ticks
    invalid = [
        _compact(received_ms=0.0, sequence=1, as_of_eligible=False, five_ok=False),
        _compact(received_ms=500.0, sequence=2),
    ]
    grid_invalid = materialize_causal_grid(invalid)
    assert grid_invalid[0]["five_ok"] is False
    assert grid_invalid[0]["seq"] is None
    assert grid_invalid[1]["seq"] == 2


def test_grid_hash_is_deterministic() -> None:
    rows = [_compact(received_ms=float(i * 500), sequence=i + 1) for i in range(4)]
    grid = materialize_causal_grid(rows)
    assert hash_grid_rows(grid) == hash_grid_rows(grid)


def test_short_capture_is_data_inadequate(tmp_path: Path) -> None:
    raw = tmp_path / "short.ndjson"
    _write_dense(raw, n=DENSE_N)
    scored = score_long_corpus_admissibility(raw)
    assert scored["passed"] is False
    assert scored["gates"]["usable_hours_ge_8"] is False
    assert "usable_hours_ge_8" in scored["failure_classes"]
    assert scored["mom_gap_inspected"] is False
    assert scored["n_cells_executed"] == 0
    assert scored["grid_sha256"] is None
    assert scored["bad_periods_cropped"] is False
    assert scored["gates"]["exactly_one_manual_start_raw_row"] is True
    assert scored["gates"]["remaining_raw_rows_interval"] is True
    assert scored["gates"]["zero_mutation_raw_rows"] is True
    assert scored["gates"]["catalog_v1_2"] is True
    assert scored["gates"]["frozen_interarrival_p95"] is True
    assert scored["gates"]["frozen_interarrival_le_2000"] is True
    assert scored["gates"]["heartbeat_persisted_equals_timer_callbacks"] is True
    payload = write_reports(
        raw=raw,
        out_json=tmp_path / "out.json",
        out_md=tmp_path / "out.md",
    )
    assert payload["status"] == STATUS_INADEQUATE
    assert payload["decision"] == "STOP_FOR_LEAD_REVIEW"
    assert payload["n_cells_executed"] == 0
    assert "DATA_INADEQUATE" in (tmp_path / "out.md").read_text(encoding="utf-8")


def test_mutation_raw_row_fails_interval_only_gate(tmp_path: Path) -> None:
    raw = tmp_path / "mutation.ndjson"
    _write_dense(raw, n=21, extra_mutation=True)
    scored = score_long_corpus_admissibility(raw)
    assert scored["gates"]["zero_mutation_raw_rows"] is False
    assert "zero_mutation_raw_rows" in scored["failure_classes"]
    assert scored["passed"] is False
    assert scored["grid_sha256"] is None


def test_hidden_visibility_fails_operator_condition(tmp_path: Path) -> None:
    raw = tmp_path / "hidden.ndjson"
    _write_dense(raw, n=21, hidden=True)
    scored = score_long_corpus_admissibility(raw)
    assert scored["gates"]["operator_intended_continuously_visible"] is False
    assert scored["visible_to_hidden_transitions"] == 1
    assert scored["passed"] is False
