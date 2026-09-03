"""Long-observation warmup vs DATA_INVALID. No strategy retune."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_bot.research.mexc_shadow.ui_capture.durable import DurableCaptureStore
from trading_bot.research.mexc_shadow.ui_capture.extract import extract_html
from trading_bot.research.mexc_shadow.ui_capture.long_observation import (
    CLASS_DATA_INVALID,
    CLASS_READY_VALID,
    CLASS_STARTUP_WARMUP,
    classify_observation_row,
    scan_long_capture,
)
from trading_bot.research.mexc_shadow.ui_capture.parse import symbol_from_futures_path
from trading_bot.research.mexc_shadow.ui_capture.replay import replay_capture_smoke

BASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _stamp(offset_ms: int) -> str:
    return (BASE + timedelta(milliseconds=offset_ms)).isoformat()


def _html_ready(*, bid: float, ask: float, last: float) -> str:
    return f"""
    <div data-mexc-capture="symbol">TAOUSDT</div>
    <div data-mexc-capture="bid">{bid:.2f}</div>
    <div data-mexc-capture="ask">{ask:.2f}</div>
    <div data-mexc-capture="last">{last:.2f}</div>
    <div data-mexc-capture="mark">{bid + 0.01:.2f}</div>
    <div data-mexc-capture="index">{bid + 0.02:.2f}</div>
    """


def _html_missing_bbo(*, last: float) -> str:
    return f"""
    <div data-mexc-capture="symbol">TAOUSDT</div>
    <div data-mexc-capture="bid">--</div>
    <div data-mexc-capture="ask">--</div>
    <div data-mexc-capture="last">{last:.2f}</div>
    <div data-mexc-capture="mark">--</div>
    <div data-mexc-capture="index">--</div>
    """


def _append(store: DurableCaptureStore, html: str, *, offset_ms: int, previous=None):
    snap = extract_html(
        html,
        received_at_local=_stamp(offset_ms),
        sequence=0,
        previous=previous,
        monotonic_ms=float(offset_ms),
        trigger="interval",
        page_path="/en-US/futures/TAO_USDT",
    )
    store.append_snapshot(snap.as_dict())
    return snap


def test_locale_and_plain_futures_paths_yield_symbol_spot_does_not() -> None:
    assert symbol_from_futures_path("/futures/TAO_USDT") == "TAOUSDT"
    assert symbol_from_futures_path("/ru-RU/futures/TAO_USDT") == "TAOUSDT"
    assert symbol_from_futures_path("/en-US/futures/TAO_USDT") == "TAOUSDT"
    assert symbol_from_futures_path("/ru-RU/spot/TAO_USDT") is None


def test_classify_warmup_before_ready_then_data_invalid() -> None:
    assert (
        classify_observation_row(session_ready=False, observation_ok=False)
        == CLASS_STARTUP_WARMUP
    )
    assert (
        classify_observation_row(session_ready=False, observation_ok=True) == CLASS_READY_VALID
    )
    assert (
        classify_observation_row(session_ready=True, observation_ok=False) == CLASS_DATA_INVALID
    )
    assert classify_observation_row(session_ready=True, observation_ok=True) == CLASS_READY_VALID


def test_session_start_invalids_are_warmup_post_ready_missing_bbo_is_data_invalid(
    tmp_path: Path,
) -> None:
    store = DurableCaptureStore(chunk_size=10)
    store.start_session(started_at=_stamp(0), interval_ms=500, session_id="sess-a")
    prev = None
    prev = _append(store, _html_missing_bbo(last=100.00), offset_ms=0, previous=prev)
    prev = _append(store, _html_missing_bbo(last=100.00), offset_ms=400, previous=prev)
    prev = _append(
        store, _html_ready(bid=100.00, ask=100.02, last=100.01), offset_ms=800, previous=prev
    )
    prev = _append(store, _html_missing_bbo(last=100.03), offset_ms=1200, previous=prev)
    prev = _append(
        store, _html_ready(bid=100.04, ask=100.06, last=100.05), offset_ms=1600, previous=prev
    )
    store.stop_session(ended_at=_stamp(1700))
    store.start_session(started_at=_stamp(5000), interval_ms=500, session_id="sess-b")
    prev = _append(store, _html_missing_bbo(last=101.00), offset_ms=5000, previous=None)
    _append(store, _html_ready(bid=101.00, ask=101.02, last=101.01), offset_ms=5400, previous=prev)
    store.stop_session(ended_at=_stamp(5500))

    raw = tmp_path / "long.ndjson"
    raw.write_text(store.export_all_ndjson(), encoding="utf-8")
    # Raw still contains the warmup rows.
    scan = scan_long_capture(raw)
    assert scan["n_raw"] == 7
    assert scan["n_startup_warmup"] == 3
    assert scan["n_data_invalid"] == 1
    assert scan["n_ready_valid"] == 3
    mids = scan["_series_mids_for_tests"]
    # Two warmup None, ready mid, DATA_INVALID None (not the previous mid), ready mid,
    # session-b warmup None, ready mid.
    assert mids[0] is None
    assert mids[1] is None
    assert mids[2] == pytest.approx(100.01)
    assert mids[3] is None
    assert mids[4] == pytest.approx(100.05)
    assert mids[5] is None
    assert mids[6] == pytest.approx(101.01)
    classes = [burst["class"] for burst in scan["warmup_and_invalid_bursts"]]
    assert CLASS_STARTUP_WARMUP in classes
    assert CLASS_DATA_INVALID in classes
    # Do not retune: smoke stays a diagnostic label.
    smoke = replay_capture_smoke(raw, "author_observed_v0", hypothesis_smoke=True)
    assert any("HYPOTHESIS_SMOKE" in note for note in smoke.notes)
    assert smoke.observations == 3
