"""Real-extension E2E gates and descriptive long-capture stats. No retune."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_bot.research.mexc_shadow.ui_capture.durable import DurableCaptureStore
from trading_bot.research.mexc_shadow.ui_capture.extract import extract_html
from trading_bot.research.mexc_shadow.ui_capture.long_report import (
    build_milestone_report,
    describe_market,
    evaluate_phase_a,
)
from trading_bot.research.mexc_shadow.ui_capture.quality import summarize_capture
from trading_bot.research.mexc_shadow.ui_capture.replay import (
    HYPOTHESIS_SMOKE_NOTE,
    replay_capture_smoke,
)

REPO = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _stamp(offset_ms: int) -> str:
    return (BASE + timedelta(milliseconds=offset_ms)).isoformat()


def _html(bid: float, ask: float, last: float, mark: float, index: float) -> str:
    return f"""
    <div data-mexc-capture="symbol">TAOUSDT</div>
    <div data-mexc-capture="bid">{bid:.2f}</div>
    <div data-mexc-capture="ask">{ask:.2f}</div>
    <div data-mexc-capture="last">{last:.2f}</div>
    <div data-mexc-capture="mark">{mark:.2f}</div>
    <div data-mexc-capture="index">{index:.2f}</div>
    """


def _append_priced(
    store: DurableCaptureStore,
    *,
    offset_ms: int,
    last: float,
    trigger: str = "interval",
    previous=None,
):
    snap = extract_html(
        _html(100.00, 100.02, last, 100.01, 100.03),
        received_at_local=_stamp(offset_ms),
        sequence=0,
        previous=previous,
        monotonic_ms=float(offset_ms),
        trigger=trigger,
        page_path="/futures/TAO_USDT",
    )
    store.append_snapshot(snap.as_dict())
    return snap


def test_export_all_keeps_stop_start_boundaries_and_per_session_sequences(tmp_path: Path) -> None:
    store = DurableCaptureStore(chunk_size=2)
    store.start_session(started_at=_stamp(0), interval_ms=500, session_id="sess-a")
    prev = None
    prev = _append_priced(store, offset_ms=0, last=100.00, trigger="manual", previous=prev)
    prev = _append_priced(store, offset_ms=500, last=100.00, previous=prev)
    prev = _append_priced(store, offset_ms=1000, last=100.01, previous=prev)
    store.stop_session(ended_at=_stamp(1100))
    store.start_session(started_at=_stamp(2000), interval_ms=500, session_id="sess-b")
    prev = _append_priced(store, offset_ms=2000, last=100.02, trigger="manual", previous=None)
    _append_priced(store, offset_ms=2500, last=100.02, previous=prev)
    store.stop_session(ended_at=_stamp(2600))

    raw = tmp_path / "two_sessions.ndjson"
    raw.write_text(store.export_all_ndjson(), encoding="utf-8")
    text = raw.read_text(encoding="utf-8")
    lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    types = [row.get("record_type") or "snapshot" for row in lines]
    assert types[0] == "session_start"
    assert types[-1] == "session_end"
    assert types.count("session_start") == 2
    assert types.count("session_end") == 2
    snapshots = [row for row in lines if row.get("schema") == "mexc_ui_raw_snapshot"]
    capture_ids = [row["capture_id"] for row in snapshots]
    assert capture_ids == ["sess-a", "sess-a", "sess-a", "sess-b", "sess-b"]
    assert [row["sequence"] for row in snapshots] == [1, 2, 3, 1, 2]

    quality = summarize_capture(raw)
    assert quality.n_sessions == 2
    assert quality.n_raw == 5
    assert quality.n_chunks_total == 3
    assert quality.capture_id is None
    assert not quality.sequence_diagnostics
    assert quality.interarrival_ms["p90_ms"] is not None


def test_phase_a_fails_without_operator_export() -> None:
    report = evaluate_phase_a(None)
    assert report["pass"] is False
    names = {gate["name"]: gate["ok"] for gate in report["gates"]}
    assert names["operator_unpacked_extension_export"] is False
    assert names["screenshot_quote_agreement"] is False
    milestone = build_milestone_report(None)
    assert milestone["STATUS"] == "MEXC_UI_EXTENSION_E2E_AND_LONG_CAPTURE_PHASE_A_BLOCKED"
    assert milestone["DECISION"] == "STOP_FOR_LEAD_REVIEW"
    assert milestone["phase_b"]["STATUS"] == "NOT_STARTED"


def test_phase_a_rejects_hydration_cdp_sample() -> None:
    hydration = REPO / "data" / "mexc_ui_capture" / "hydration_gate" / "tao_hydrated_150s.ndjson"
    if not hydration.is_file():
        return
    report = evaluate_phase_a(hydration, screenshot_agreement="PASS", restart_attested=True)
    assert report["pass"] is False
    assert report["rejected_hydration_cdp_as_phase_a"] is True
    names = {gate["name"]: gate["ok"] for gate in report["gates"]}
    assert names["operator_unpacked_extension_export"] is False


def test_phase_a_passes_synthetic_two_session_export(tmp_path: Path) -> None:
    store = DurableCaptureStore(chunk_size=10)
    store.start_session(started_at=_stamp(0), interval_ms=500, session_id="a1")
    prev = None
    for index in range(12):
        prev = _append_priced(
            store,
            offset_ms=index * 500,
            last=100.00 + index * 0.01,
            trigger="manual" if index == 0 else "interval",
            previous=prev,
        )
    store.stop_session(ended_at=_stamp(6000))
    store.start_session(started_at=_stamp(7000), interval_ms=500, session_id="a2")
    prev = None
    for index in range(12, 24):
        prev = _append_priced(
            store,
            offset_ms=index * 500,
            last=100.00 + index * 0.01,
            trigger="manual" if index == 12 else "mutation",
            previous=prev,
        )
    store.stop_session(ended_at=_stamp(12000))
    raw = tmp_path / "phase_a.ndjson"
    raw.write_text(store.export_all_ndjson(), encoding="utf-8")
    report = evaluate_phase_a(
        raw,
        min_duration_ms=1000,
        max_duration_ms=20_000,
        screenshot_agreement="PASS",
        restart_attested=True,
    )
    assert report["pass"] is True
    assert report["file_sha256"]
    assert report["hypothesis_smoke"]["label"] == "HYPOTHESIS_SMOKE"
    assert any(HYPOTHESIS_SMOKE_NOTE[:16] in note for note in report["hypothesis_smoke"]["notes"])
    smoke = replay_capture_smoke(raw, hypothesis_smoke=True)
    assert any("HYPOTHESIS_SMOKE" in note for note in smoke.notes)


def test_descriptive_returns_and_gaps_are_not_strategy_rules(tmp_path: Path) -> None:
    store = DurableCaptureStore(chunk_size=10)
    store.start_session(started_at=_stamp(0), interval_ms=1000, session_id="desc")
    prev = None
    # 100 -> 100.1 in 1s is 10 bps on last.
    prices = (100.00, 100.10, 100.20)
    for index, last in enumerate(prices):
        prev = _append_priced(
            store,
            offset_ms=index * 1000,
            last=last,
            trigger="interval",
            previous=prev,
        )
    store.stop_session(ended_at=_stamp(3000))
    raw = tmp_path / "desc.ndjson"
    raw.write_text(store.export_ndjson("desc"), encoding="utf-8")
    market = describe_market(raw)
    last_1s = market["horizon_returns_bps"]["1s"]["last"]
    assert last_1s["n"] == 2
    assert last_1s["mean"] == pytest.approx(9.995, abs=0.05)
    assert last_1s["freq_abs_ge_bps"]["1"] == 2
    assert market["spread_bps"]["n"] == 3
    assert market["gaps_bps"]["mid_minus_mark_bps"]["n"] == 3
    assert market["lead_lag_xcorr"]["status"] in {"DESCRIPTIVE_ONLY", "INSUFFICIENT_SAMPLE"}
    assert "Not a fitted trading rule" in market["notes"][0]
