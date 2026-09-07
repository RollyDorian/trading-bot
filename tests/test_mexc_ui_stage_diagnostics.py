"""Stage diagnostics: correlation, counters, visibility, fencing, bounds.

Does not retune mom/gap, change protocol v2, or require a live Chrome capture.
"""

from __future__ import annotations

import json
from pathlib import Path

from trading_bot.research.mexc_shadow.ui_capture.durable import DurableCaptureStore
from trading_bot.research.mexc_shadow.ui_capture.normalize import snapshot_from_mapping
from trading_bot.research.mexc_shadow.ui_capture.stage_diagnostics import (
    DIAGNOSTIC_FORMAT_VERSION,
    EXTENSION_VERSION,
    HISTOGRAM_BOUNDS_MS,
    INTERVAL_DETAIL_CAP,
    LIFECYCLE_CAP,
    MUTATION_DETAIL_CAP,
    BoundedSidecar,
    CaptureStagePipeline,
    empty_histogram,
    expected_deadline_mono,
    expected_interval_opportunities,
    milestone_report,
    observe_histogram,
    queue_wait_ms,
    reconcile_counters,
    sanitize_stage_diagnostics,
    should_sample_mutation,
    write_reports,
)

REPO = Path(__file__).resolve().parents[1]
CONTENT = (REPO / "extensions" / "mexc_ui_capture" / "content.js").read_text(
    encoding="utf-8"
)
BACKGROUND = (REPO / "extensions" / "mexc_ui_capture" / "background.js").read_text(
    encoding="utf-8"
)
DIAG_JS = (REPO / "extensions" / "mexc_ui_capture" / "stage_diagnostics.js").read_text(
    encoding="utf-8"
)
MANIFEST = json.loads(
    (REPO / "extensions" / "mexc_ui_capture" / "manifest.json").read_text(encoding="utf-8")
)


def _min_snapshot(*, sequence: int, received: str, monotonic_ms: float) -> dict:
    return {
        "schema": "mexc_ui_raw_snapshot",
        "schema_version": 1,
        "sequence": sequence,
        "received_at_local": received,
        "observed_at_local": received,
        "monotonic_ms": monotonic_ms,
        "trigger": "interval",
        "selector_catalog_version": "v1.2",
        "page_host": "www.mexc.com",
        "page_path": "/futures/TAO_USDT",
        "observation_valid": True,
        "invalid_reasons": [],
        "changed_fields": [],
        "fields": {},
        "stage_diagnostics": {
            "request_ordinal": sequence,
            "trigger": "interval",
            "producer_epoch": "producer-a",
            "session_generation": 1,
            "interval_callback_ordinal": sequence,
            "expected_deadline_mono": float(sequence * 500),
            "callback_mono": float(sequence * 500 + 2),
            "extract_end_mono": monotonic_ms,
            "html": "<account>secret</account>",
            "raw_text": "do-not-keep",
        },
    }


def test_manifest_and_scripts_are_1_3_5_interval_only() -> None:
    assert MANIFEST["version"] == "1.3.5"
    assert MANIFEST["content_scripts"][0]["js"] == ["stage_diagnostics.js", "content.js"]
    assert "stage_diagnostics.js" in BACKGROUND
    assert "emitChain = emitChain.then" in CONTENT
    assert "setInterval(emitInterval, intervalMs)" in CONTENT
    assert "Never copy expected_deadline_mono" in CONTENT
    assert "received_at_local: received" in CONTENT
    assert "expected_deadline_mono" in CONTENT
    assert "visibilitychange" in CONTENT
    assert "producerEpoch" in CONTENT
    assert "sessionGeneration" in CONTENT
    assert "workerBootId" in BACKGROUND
    assert "appendChain.then(task, task)" in BACKGROUND
    assert "lastEmitKey" not in CONTENT
    assert "emitMutation" not in CONTENT
    assert 'emit("mutation"' not in CONTENT
    assert "noteMutation" in CONTENT
    assert "dirtySinceLastInterval" in CONTENT
    assert "INTERVAL_DETAIL_CAP = 2048" in DIAG_JS
    assert 'EXTENSION_VERSION = "1.3.5"' in DIAG_JS


def test_expected_deadline_is_from_registration_not_previous_callback() -> None:
    late_previous = 1800.0
    deadline = expected_deadline_mono(
        timer_registered_mono=100.0, interval_callback_ordinal=2, interval_ms=500
    )
    assert deadline == 1100.0
    assert deadline != late_previous + 500


def test_queue_wait_is_extract_start_minus_callback() -> None:
    assert queue_wait_ms(125.0, 100.0) == 25.0


def test_expected_interval_opportunities_use_timer_registration() -> None:
    assert expected_interval_opportunities(0.0, 10100.0, 500) == 20
    assert expected_interval_opportunities(None, 10100.0, 500) == 0


def test_sanitize_strips_private_and_unlisted_keys() -> None:
    clean = sanitize_stage_diagnostics(
        {
            "request_ordinal": 3,
            "trigger": "interval",
            "producer_epoch": "p" * 80,
            "html": "<div>nope</div>",
            "raw_text": "265.1",
            "account_balance": 1,
            "callback_mono": 12.5,
            "extract_end_mono": 40.0,
        }
    )
    assert clean is not None
    assert clean["request_ordinal"] == 3
    assert len(clean["producer_epoch"]) == 64
    assert "html" not in clean
    assert "raw_text" not in clean
    assert "account_balance" not in clean
    assert clean["diagnostic_format_version"] == DIAGNOSTIC_FORMAT_VERSION


def test_observation_timestamps_are_not_backdated_to_deadline() -> None:
    pipe = CaptureStagePipeline(interval_ms=500, extract_ms=7.0, append_ms=11.0, ipc_ms=1.0)
    pipe.start_session(0.0)
    pipe.interval_callback(500.0)
    pipe.settle(600.0)
    interval = next(trace for trace in pipe.traces if trace.request.trigger == "interval")
    assert interval.monotonic_ms == interval.extract_end_mono
    assert interval.monotonic_ms != interval.request.expected_deadline_mono
    assert interval.received_at_local is not None
    assert "deadline" not in interval.received_at_local.lower()
    snap = _min_snapshot(
        sequence=1,
        received=interval.received_at_local,
        monotonic_ms=float(interval.monotonic_ms or 0),
    )
    snap["stage_diagnostics"]["expected_deadline_mono"] = interval.request.expected_deadline_mono
    snap["stage_diagnostics"]["extract_end_mono"] = interval.extract_end_mono
    parsed = snapshot_from_mapping(snap)
    assert parsed.received_at_local == interval.received_at_local
    assert parsed.monotonic_ms == interval.monotonic_ms
    assert parsed.stage_diagnostics is not None
    assert parsed.stage_diagnostics["expected_deadline_mono"] == (
        interval.request.expected_deadline_mono
    )
    assert "html" not in parsed.stage_diagnostics


def test_diagnostic_correlation_and_queue_wait() -> None:
    pipe = CaptureStagePipeline(interval_ms=500, extract_ms=5.0, append_ms=20.0, ipc_ms=1.0)
    pipe.start_session(0.0)
    # Manual occupies the FIFO until ACK at 5+1+20+1 = 27 ms.
    for index in range(12):
        assert pipe.mutation_callback(1.0 + index) is None
    pipe.interval_callback(10.0)
    pipe.settle(200.0)
    by_ordinal = {trace.request.request_ordinal: trace for trace in pipe.traces}
    assert len(by_ordinal) == 2
    assert all(trace.request.trigger != "mutation" for trace in pipe.traces)
    interval = next(trace for trace in pipe.traces if trace.request.trigger == "interval")
    # Mutations no longer sit on emitChain, so interval wait is still manual ACK only.
    assert interval.request.content_queue_depth == 0
    wait = interval.content_queue_wait_ms()
    assert wait == interval.extract_start_mono - interval.request.callback_mono
    assert wait == 17.0
    assert interval.request.dirty_since_last_interval is True
    assert interval.request.mutation_callback_ordinal == 12
    assert pipe.sidecar.counters["mutation_callbacks"] == 12
    assert pipe.sidecar.counters["callbacks_enqueued"]["mutation"] == 0
    assert interval.worker_boot_id == "worker-a"
    assert interval.request.producer_epoch == "producer-a"
    assert interval.request.session_generation == 1
    assert interval.background_receive_mono is not None
    assert interval.append_start_mono is not None
    assert interval.append_end_mono is not None
    assert interval.ack_end_mono is not None
    diag = interval.as_stage_diagnostics()
    assert diag["request_ordinal"] == interval.request.request_ordinal
    assert diag["interval_callback_ordinal"] == 1
    assert diag["expected_deadline_mono"] == 500.0
    assert diag["dirty_since_last_interval"] is True


def test_counters_and_abandoned_at_stop() -> None:
    pipe = CaptureStagePipeline(interval_ms=500, extract_ms=5.0, append_ms=50.0, ipc_ms=1.0)
    pipe.start_session(0.0)
    pipe.interval_callback(10.0)
    pipe.interval_callback(11.0)
    pipe.interval_callback(12.0)
    pipe.stop_session(20.0)
    pipe.settle(5000.0)
    counters = pipe.sidecar.counters
    assert counters["timer_callbacks"] == 3
    assert counters["expected_interval_opportunities"] == expected_interval_opportunities(
        0.0, 20.0, 500
    )
    assert counters["callbacks_enqueued"]["interval"] == 3
    assert counters["abandoned"]["interval"] == 3
    assert counters["outstanding_at_stop"] == 3
    assert counters["acked"]["interval"] == 0
    checks = reconcile_counters(counters, clean_stop=True)
    assert checks["interval_started_abandoned"] is True
    assert counters["snapshots_persisted"]["interval"] == counters["acked"]["interval"]


def test_visibility_transitions_are_bounded() -> None:
    pipe = CaptureStagePipeline()
    pipe.start_session(0.0)
    for index in range(200):
        pipe.set_visibility(float(index + 1), "hidden" if index % 2 else "visible")
    assert len(pipe.sidecar.lifecycle) <= LIFECYCLE_CAP
    kinds = [event["kind"] for event in pipe.sidecar.lifecycle]
    assert "visibilitychange" in kinds
    assert pipe.sidecar.counters["suppressed_lifecycle"] > 0
    assert pipe.sidecar.counters["diagnostic_truncated"] is True


def test_session_fencing_is_detect_only() -> None:
    pipe = CaptureStagePipeline(extract_ms=5.0, append_ms=30.0, ipc_ms=1.0)
    pipe.start_session(0.0)
    first_generation = pipe.session_generation
    pipe.interval_callback(10.0)
    pipe.start_session(11.0)
    pipe.settle(400.0)
    interval = next(trace for trace in pipe.traces if trace.request.trigger == "interval")
    assert interval.request.session_generation == first_generation
    assert interval.stale_generation is True
    assert interval.abandoned is False
    assert interval.persisted_sequence is not None
    assert pipe.sidecar.counters["stale_generation_detected"] >= 1


def test_detail_caps_keep_updating_counters() -> None:
    sidecar = BoundedSidecar()
    pipe = CaptureStagePipeline()
    pipe.sidecar = sidecar
    pipe.capturing = True
    pipe.timer_registered_mono = 0.0
    pipe.session_generation = 1
    for index in range(INTERVAL_DETAIL_CAP + 5):
        pipe.interval_callback(float((index + 1) * 500))
    pipe.settle(float((INTERVAL_DETAIL_CAP + 10) * 500))
    assert len(sidecar.interval_details) == INTERVAL_DETAIL_CAP
    assert sidecar.counters["suppressed_interval_details"] == 5
    assert sidecar.counters["timer_callbacks"] == INTERVAL_DETAIL_CAP + 5
    assert sidecar.counters["diagnostic_truncated"] is True
    assert should_sample_mutation(1) is True
    assert should_sample_mutation(128) is True
    assert should_sample_mutation(129) is False
    assert should_sample_mutation(144) is True
    assert MUTATION_DETAIL_CAP == 512


def test_histogram_overflow_bucket() -> None:
    hist = empty_histogram()
    observe_histogram(hist, 0.5)
    observe_histogram(hist, 500.0)
    observe_histogram(hist, 99_000.0)
    assert hist["n"] == 3
    assert hist["counts"][0] == 1
    assert hist["counts"][HISTOGRAM_BOUNDS_MS.index(500)] == 1
    assert hist["counts"][-1] == 1


def test_durable_roundtrip_keeps_stage_diagnostics_and_summary() -> None:
    store = DurableCaptureStore(chunk_size=10)
    store.start_session(
        started_at="2026-09-07T00:00:00+00:00",
        interval_ms=500,
        producer_epoch="producer-a",
        session_generation=1,
        worker_boot_id="worker-a",
        extension_version=EXTENSION_VERSION,
        diagnostic_format_version=DIAGNOSTIC_FORMAT_VERSION,
        session_id="diag-1",
    )
    snap = _min_snapshot(
        sequence=0, received="2026-09-07T00:00:00.500+00:00", monotonic_ms=507.0
    )
    committed = store.append_snapshot(snap)
    assert committed["received_at_local"] == snap["received_at_local"]
    assert committed["monotonic_ms"] == 507.0
    store.stop_session(
        ended_at="2026-09-07T00:01:00+00:00",
        stage_diagnostic_summary={
            "counters": {"timer_callbacks": 1, "abandoned": {"interval": 0}}
        },
    )
    lines = [json.loads(line) for line in store.export_lines("diag-1")]
    assert lines[0]["producer_epoch"] == "producer-a"
    assert lines[-1]["stage_diagnostic_summary"]["counters"]["timer_callbacks"] == 1
    parsed = snapshot_from_mapping(lines[1])
    assert parsed.stage_diagnostics is not None
    assert parsed.received_at_local == snap["received_at_local"]


def test_milestone_report_is_instrumentation_only(tmp_path: Path) -> None:
    out_json = tmp_path / "stage.json"
    out_md = tmp_path / "stage.md"
    report = write_reports(out_json=out_json, out_md=out_md)
    assert report["status"] == "MEXC_UI_CAPTURE_STAGE_DIAGNOSTICS_READY"
    assert report["decision"] == "STOP_FOR_LEAD_REVIEW"
    assert report["protocol_changed"] is False
    assert report["scheduler_changed"] is False
    assert report["live_diagnostic_capture"] == "NOT_RUN"
    assert report["extension_version"] == "1.3.5"
    text = out_md.read_text(encoding="utf-8")
    assert "Never backdate" in text
    assert milestone_report()["mom_gap_inspected"] is False
