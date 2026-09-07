"""Interval-only raw capture. Mutations stay diagnostic. No mom/gap retune."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_bot.research.mexc_shadow.ui_capture.durable import DurableCaptureStore
from trading_bot.research.mexc_shadow.ui_capture.extract import extract_html
from trading_bot.research.mexc_shadow.ui_capture.interval_only_capture import (
    MILESTONE_DECISION,
    MILESTONE_STATUS,
    write_reports,
)
from trading_bot.research.mexc_shadow.ui_capture.stage_diagnostics import (
    EXTENSION_VERSION,
    CaptureStagePipeline,
    sanitize_stage_diagnostics,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "mexc_ui_capture"
CONTENT = (REPO / "extensions" / "mexc_ui_capture" / "content.js").read_text(
    encoding="utf-8"
)
DIAG_JS = (REPO / "extensions" / "mexc_ui_capture" / "stage_diagnostics.js").read_text(
    encoding="utf-8"
)
MANIFEST = json.loads(
    (REPO / "extensions" / "mexc_ui_capture" / "manifest.json").read_text(encoding="utf-8")
)
BASE = datetime(2026, 9, 7, 18, 0, tzinfo=UTC)


def _html() -> str:
    return (FIXTURES / "tao_live_wrappers.html").read_text(encoding="utf-8")


def _stamp(offset_ms: int) -> str:
    return (BASE + timedelta(milliseconds=offset_ms)).isoformat()


def test_extension_is_interval_only_raw_rows() -> None:
    assert MANIFEST["version"] == "1.3.5"
    assert EXTENSION_VERSION == "1.3.5"
    assert 'EXTENSION_VERSION = "1.3.5"' in DIAG_JS
    assert "noteMutation" in CONTENT
    assert "emitMutation" not in CONTENT
    assert 'emit("mutation"' not in CONTENT
    assert "dirtySinceLastInterval" in CONTENT
    assert "dirtySinceLastInterval never skips this extract" in CONTENT
    assert "Always reread every catalog field from the live DOM" in CONTENT
    assert "lastValue tracks age only" in CONTENT
    assert "fields[name] = lastValue" not in CONTENT
    assert "rec.value = lastValue" not in CONTENT
    assert "MutationObserver" in CONTENT
    assert "setInterval(emitInterval, intervalMs)" in CONTENT
    assert "visibilitychange" in CONTENT


def test_many_mutations_create_zero_raw_rows_and_do_not_block_interval() -> None:
    pipe = CaptureStagePipeline(interval_ms=500, extract_ms=5.0, append_ms=40.0, ipc_ms=1.0)
    pipe.start_session(0.0)
    pipe.settle(50.0)
    for index in range(40):
        assert pipe.mutation_callback(60.0 + index) is None
    interval = pipe.interval_callback(500.0)
    assert interval is not None
    pipe.settle(800.0)
    traces = [trace for trace in pipe.traces if not trace.abandoned]
    triggers = [trace.request.trigger for trace in traces]
    assert triggers == ["manual", "interval"]
    assert pipe.sidecar.counters["mutation_callbacks"] == 40
    assert pipe.sidecar.counters["mutation_dirty_sets"] == 1
    assert pipe.sidecar.counters["callbacks_enqueued"]["mutation"] == 0
    assert pipe.sidecar.counters["snapshots_persisted"]["mutation"] == 0
    assert pipe.sidecar.mutation_details == []
    interval_trace = next(trace for trace in traces if trace.request.trigger == "interval")
    # Manual ACK is long finished by t=500; mutations added no FIFO work.
    assert interval_trace.extract_start_mono == 500.0
    assert interval_trace.request.dirty_since_last_interval is True
    assert interval_trace.request.content_queue_depth == 0
    diag = interval_trace.as_stage_diagnostics()
    assert diag["dirty_since_last_interval"] is True
    assert "html" not in diag
    assert "raw_text" not in diag


def test_clean_interval_still_extracts_when_dirty_is_false() -> None:
    pipe = CaptureStagePipeline(interval_ms=500, extract_ms=5.0, append_ms=10.0, ipc_ms=1.0)
    pipe.start_session(0.0)
    pipe.settle(50.0)
    first = pipe.interval_callback(500.0)
    second = pipe.interval_callback(1000.0)
    pipe.settle(1200.0)
    assert first is not None and second is not None
    assert first.dirty_since_last_interval is False
    assert second.dirty_since_last_interval is False
    persisted = [trace.request.trigger for trace in pipe.traces if not trace.abandoned]
    assert persisted == ["manual", "interval", "interval"]


def test_unchanged_market_values_still_produce_interval_observations() -> None:
    html = _html()
    first = extract_html(
        html,
        received_at_local=_stamp(0),
        sequence=1,
        trigger="interval",
        sample_interval_ms=500,
        monotonic_ms=0.0,
        capture_id="interval-only",
        page_path="/futures/TAO_USDT",
        page_host="www.mexc.com",
    )
    second = extract_html(
        html,
        received_at_local=_stamp(500),
        sequence=2,
        trigger="interval",
        sample_interval_ms=500,
        previous=first,
        monotonic_ms=500.0,
        capture_id="interval-only",
        page_path="/futures/TAO_USDT",
        page_host="www.mexc.com",
    )
    assert first.trigger == "interval"
    assert second.trigger == "interval"
    assert first.fields["bid"].value == second.fields["bid"].value
    assert first.fields["ask"].value == second.fields["ask"].value
    assert first.fields["last"].value == second.fields["last"].value
    assert first.received_at_local != second.received_at_local
    store = DurableCaptureStore(chunk_size=10)
    store.start_session(
        started_at=_stamp(0),
        interval_ms=500,
        session_id="interval-only",
        extension_version=EXTENSION_VERSION,
    )
    store.append_snapshot(first.as_dict())
    store.append_snapshot(second.as_dict())
    store.stop_session(ended_at=_stamp(1000))
    lines = [json.loads(line) for line in store.export_lines("interval-only")]
    snaps = [row for row in lines if row.get("schema") == "mexc_ui_raw_snapshot"]
    assert [row["trigger"] for row in snaps] == ["interval", "interval"]
    assert [row["sequence"] for row in snaps] == [1, 2]


def test_next_interval_rereads_changed_dom() -> None:
    html = _html()
    first = extract_html(
        html,
        received_at_local=_stamp(0),
        sequence=1,
        trigger="interval",
        monotonic_ms=0.0,
        capture_id="interval-only",
        page_path="/futures/TAO_USDT",
        page_host="www.mexc.com",
    )
    updated = html.replace(">226.15<", ">227.01<")
    second = extract_html(
        updated,
        received_at_local=_stamp(500),
        sequence=2,
        trigger="interval",
        previous=first,
        monotonic_ms=500.0,
        capture_id="interval-only",
        page_path="/futures/TAO_USDT",
        page_host="www.mexc.com",
    )
    assert first.fields["last"].value == pytest.approx(226.15)
    assert second.fields["last"].value == pytest.approx(227.01)
    assert second.fields["last"].value != first.fields["last"].value


def test_missing_fields_are_not_copied_from_previous_rows() -> None:
    html = _html()
    first = extract_html(
        html,
        received_at_local=_stamp(0),
        sequence=1,
        trigger="interval",
        monotonic_ms=0.0,
        capture_id="interval-only",
        page_path="/futures/TAO_USDT",
        page_host="www.mexc.com",
    )
    assert first.fields["mark"].value is not None
    stripped = html.replace(
        "<span>Fair Price</span><span>226.13</span>",
        "<span>Fair Price</span><span>--</span>",
    )
    second = extract_html(
        stripped,
        received_at_local=_stamp(500),
        sequence=2,
        trigger="interval",
        previous=first,
        monotonic_ms=500.0,
        capture_id="interval-only",
        page_path="/futures/TAO_USDT",
        page_host="www.mexc.com",
    )
    assert second.fields["mark"].parse_status == "missing"
    assert second.fields["mark"].value is None
    assert second.fields["mark"].value != first.fields["mark"].value
    assert second.fields["bid"].value == first.fields["bid"].value


def test_sanitize_keeps_dirty_flag_and_drops_private_mutation_payload() -> None:
    clean = sanitize_stage_diagnostics(
        {
            "trigger": "interval",
            "dirty_since_last_interval": True,
            "mutation_callback_ordinal": 40,
            "html": "<account>nope</account>",
            "raw_text": "226.13",
            "innerText": "secret",
        }
    )
    assert clean is not None
    assert clean["dirty_since_last_interval"] is True
    assert clean["mutation_callback_ordinal"] == 40
    assert "html" not in clean
    assert "raw_text" not in clean
    assert "innerText" not in clean


def test_milestone_report_does_not_change_protocol(tmp_path: Path) -> None:
    out_json = tmp_path / "interval.json"
    out_md = tmp_path / "interval.md"
    report = write_reports(out_json=out_json, out_md=out_md)
    assert report["status"] == MILESTONE_STATUS
    assert report["decision"] == MILESTONE_DECISION
    assert report["protocol_changed"] is False
    assert report["protocol_v2_can_remain_unchanged"] is True
    assert report["mutation_raw_rows"] is False
    assert report["persistence_decoupling"] is False
    assert report["indexeddb_batching"] is False
    assert report["paper"] is False
    assert report["live"] is False
    assert report["mom_gap_inspected"] is False
    text = out_md.read_text(encoding="utf-8")
    assert "Protocol v2.0.0: **unchanged**" in text
    assert "must not enqueue a raw market snapshot" in text
