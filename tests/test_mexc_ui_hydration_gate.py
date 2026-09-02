"""Hydration-gate: monotonic field age, durable chunks, live last-split BBO."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_bot.research.mexc_shadow.ui_capture.age import apply_field_ages, clock_ms
from trading_bot.research.mexc_shadow.ui_capture.durable import (
    DEFAULT_CHUNK_SIZE,
    DurableCaptureStore,
    DurableStorageError,
)
from trading_bot.research.mexc_shadow.ui_capture.extract import extract_html
from trading_bot.research.mexc_shadow.ui_capture.normalize import observation_from_snapshot
from trading_bot.research.mexc_shadow.ui_capture.quality import summarize_capture
from trading_bot.research.mexc_shadow.ui_capture.replay import replay_capture_smoke
from trading_bot.research.mexc_shadow.ui_capture.schema import FieldRecord
from trading_bot.research.mexc_shadow.ui_capture.store import iter_raw_mappings

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "mexc_ui_capture"
BASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _stamp(offset_ms: int) -> str:
    return (BASE + timedelta(milliseconds=offset_ms)).isoformat()


def _bbo_html(bid: str, ask: str, extra: str = "") -> str:
    return f"""
    <div data-mexc-capture="symbol">TAOUSDT</div>
    <div data-mexc-capture="bid">{bid}</div>
    <div data-mexc-capture="ask">{ask}</div>
    {extra}
    """


def test_age_uses_monotonic_last_change_not_interval_sum() -> None:
    html = _bbo_html("100.00", "100.20")
    first = extract_html(
        html,
        received_at_local=_stamp(0),
        sequence=1,
        monotonic_ms=1_000.0,
        capture_id="sess-a",
    )
    assert first.fields["bid"].age_ms == 0
    burst = first
    # Mutation burst: same value, 4 snapshots in 12 ms. Old code would add intervalMs.
    for offset, mono in enumerate((1004.0, 1008.0, 1012.0), start=2):
        burst = extract_html(
            html,
            received_at_local=_stamp(offset * 500),
            sequence=offset,
            monotonic_ms=mono,
            previous=burst,
            capture_id="sess-a",
            sample_interval_ms=500,
        )
        assert burst.fields["bid"].age_ms == int(mono - 1_000.0)
        assert burst.fields["bid"].age_ms != (offset - 1) * 500
    assert burst.fields["bid"].age_ms == 12
    assert burst.fields["ask"].age_ms == 12
    assert "bid" not in burst.changed_fields


def test_age_resets_on_value_change_and_missing_to_valid() -> None:
    first = extract_html(
        _bbo_html("100.00", "100.20"),
        received_at_local=_stamp(0),
        sequence=1,
        monotonic_ms=100.0,
        capture_id="sess-a",
    )
    missing_index = extract_html(
        _bbo_html("100.00", "100.20"),
        received_at_local=_stamp(50),
        sequence=2,
        monotonic_ms=150.0,
        previous=first,
        capture_id="sess-a",
    )
    assert missing_index.fields["index"].parse_status == "missing"
    assert missing_index.fields["index"].age_ms is None
    assert missing_index.fields["bid"].age_ms == 50
    appeared = extract_html(
        _bbo_html("100.00", "100.20", '<div><span>Index Price</span><span>99.50</span></div>'),
        received_at_local=_stamp(80),
        sequence=3,
        monotonic_ms=180.0,
        previous=missing_index,
        capture_id="sess-a",
    )
    assert appeared.fields["index"].parse_status == "ok"
    assert appeared.fields["index"].age_ms == 0
    assert "index" in appeared.changed_fields
    later = extract_html(
        _bbo_html("100.40", "100.60", '<div><span>Index Price</span><span>99.50</span></div>'),
        received_at_local=_stamp(90),
        sequence=4,
        monotonic_ms=190.0,
        previous=appeared,
        capture_id="sess-a",
    )
    assert later.fields["bid"].age_ms == 0
    assert later.fields["index"].age_ms == 10


def test_age_resets_on_capture_restart_and_page_change() -> None:
    first = extract_html(
        _bbo_html("100.00", "100.20"),
        received_at_local=_stamp(0),
        sequence=1,
        monotonic_ms=10.0,
        capture_id="sess-a",
        page_path="/futures/TAO_USDT",
    )
    same_page = extract_html(
        _bbo_html("100.00", "100.20"),
        received_at_local=_stamp(40),
        sequence=2,
        monotonic_ms=50.0,
        previous=first,
        capture_id="sess-a",
        page_path="/futures/TAO_USDT",
    )
    assert same_page.fields["bid"].age_ms == 40
    restarted = extract_html(
        _bbo_html("100.00", "100.20"),
        received_at_local=_stamp(80),
        sequence=1,
        monotonic_ms=90.0,
        previous=same_page,
        capture_id="sess-b",
        page_path="/futures/TAO_USDT",
    )
    assert restarted.fields["bid"].age_ms == 0
    other_page = extract_html(
        _bbo_html("100.00", "100.20"),
        received_at_local=_stamp(100),
        sequence=2,
        monotonic_ms=110.0,
        previous=restarted,
        capture_id="sess-b",
        page_path="/futures/ETH_USDT",
    )
    assert other_page.fields["bid"].age_ms == 0


def test_apply_field_ages_controlled_clock() -> None:
    def rec(value: float, age: int | None = None, changed_at: float | None = None) -> FieldRecord:
        return FieldRecord(
            name="bid",
            raw_text=str(value),
            value=value,
            selector_id="data_attr:bid",
            parse_status="ok",
            match_count=1,
            age_ms=age,
            changed_at_monotonic_ms=changed_at,
        )

    first = {
        "bid": rec(1.0),
    }
    aged, changed = apply_field_ages(
        first,
        now_ms=5.0,
        page_host="h",
        page_path="/futures/TAO_USDT",
        capture_id="s",
        previous=None,
        now_has_monotonic=True,
    )
    assert changed == ("bid",)
    assert aged["bid"].age_ms == 0
    assert aged["bid"].changed_at_monotonic_ms == 5.0
    assert clock_ms(12.0, _stamp(0)) == 12.0


def test_live_orderbook_splits_by_last_not_mark_or_ticket() -> None:
    snap = extract_html(
        _html("tao_live_orderbook_panel.html"),
        received_at_local=_stamp(0),
        sequence=1,
        page_path="/futures/TAO_USDT",
        monotonic_ms=0.0,
    )
    rec = observation_from_snapshot(snap)
    assert rec.observation is not None
    obs = rec.observation
    assert obs.symbol == "TAOUSDT"
    assert obs.bid == pytest.approx(226.10)
    assert obs.ask == pytest.approx(226.18)
    assert obs.bid != pytest.approx(226.14)
    assert obs.bid != pytest.approx(226.13)
    assert obs.ask != pytest.approx(226.99)
    assert obs.mark == pytest.approx(226.13)
    assert obs.index == pytest.approx(226.14)
    assert obs.last == pytest.approx(226.15)
    assert snap.fields["bid"].selector_id == "live_orderbook_split_by_last"
    assert snap.fields["ask"].selector_id == "live_orderbook_split_by_last"
    assert snap.orderbook_diagnostics["chosen_bbo_source"] == "live_orderbook_heading_fallback"


def test_wrapper_bbo_and_lastprice_class_ignore_ticket() -> None:
    snap = extract_html(
        _html("tao_live_wrappers.html"),
        received_at_local=_stamp(0),
        sequence=1,
        page_path="/futures/TAO_USDT",
        monotonic_ms=0.0,
    )
    rec = observation_from_snapshot(snap)
    assert rec.observation is not None
    obs = rec.observation
    assert obs.bid == pytest.approx(226.10)
    assert obs.ask == pytest.approx(226.18)
    assert obs.last == pytest.approx(226.15)
    assert obs.mark == pytest.approx(226.13)
    assert obs.bid != pytest.approx(888.88)
    assert obs.ask != pytest.approx(999.99)
    assert snap.fields["bid"].selector_id == "live_asks_bids_wrapper"
    assert snap.fields["last"].selector_id == "class:lastPrice"


def test_funding_uncle_walk_does_not_apply_to_last_price_dropdown() -> None:
    html = """
    <div data-mexc-capture="symbol">TAOUSDT</div>
    <div data-mexc-capture="bid">100.00</div>
    <div data-mexc-capture="ask">100.02</div>
    <div>
      <div><span>Funding Rate</span><span>/</span><span>Countdown</span></div>
      <div>+0.0050%/01:00:00</div>
    </div>
    <div>
      <span>Last Price</span>
    </div>
    <div>77.485</div>
    <div class="x__lastPrice">100.01</div>
    """
    snap = extract_html(html, received_at_local=_stamp(0), sequence=1, monotonic_ms=0.0)
    assert snap.fields["funding"].parse_status in {"ok", "ok_redundant"}
    assert snap.fields["funding"].value == pytest.approx(0.005)
    assert snap.fields["last"].value == pytest.approx(100.01)
    assert snap.fields["last"].value != pytest.approx(77.485)


def test_ambiguous_orderbook_heading_is_invalid() -> None:
    snap = extract_html(
        _html("tao_ambiguous_orderbook.html"),
        received_at_local=_stamp(0),
        sequence=1,
        page_path="/futures/TAO_USDT",
    )
    assert "ambiguous_orderbook_heading" in snap.invalid_reasons
    assert snap.observation_valid is False
    rec = observation_from_snapshot(snap)
    assert rec.observation is None


def test_durable_store_survives_restart_and_does_not_truncate() -> None:
    store = DurableCaptureStore(chunk_size=100)
    started = store.start_session(
        started_at=_stamp(0),
        interval_ms=500,
        page_host="www.mexc.com",
        page_path="/futures/TAO_USDT",
        session_id="cap-1",
    )
    assert started.session_id == "cap-1"
    n = 250
    for index in range(n):
        store.append_snapshot(
            {
                "schema": "mexc_ui_raw_snapshot",
                "schema_version": 1,
                "sequence": 0,
                "received_at_local": _stamp(index),
                "observed_at_local": _stamp(index),
                "monotonic_ms": float(index),
                "trigger": "interval",
                "selector_catalog_version": "v1",
                "page_host": "www.mexc.com",
                "page_path": "/futures/TAO_USDT",
                "observation_valid": True,
                "invalid_reasons": [],
                "changed_fields": [],
                "fields": {},
            }
        )
    state = store.snapshot_state()
    restored = DurableCaptureStore.from_state(state)
    assert restored.active_session_id == "cap-1"
    restored.append_snapshot(
        {
            "schema": "mexc_ui_raw_snapshot",
            "schema_version": 1,
            "sequence": 0,
            "received_at_local": _stamp(n),
            "observed_at_local": _stamp(n),
            "monotonic_ms": float(n),
            "trigger": "mutation",
            "selector_catalog_version": "v1",
            "page_host": "www.mexc.com",
            "page_path": "/futures/TAO_USDT",
            "observation_valid": True,
            "invalid_reasons": [],
            "changed_fields": [],
            "fields": {},
        }
    )
    restored.stop_session(ended_at=_stamp(n + 1))
    lines = restored.export_lines("cap-1")
    assert lines[0].startswith("{")
    start = json.loads(lines[0])
    end = json.loads(lines[-1])
    snapshots = [json.loads(line) for line in lines[1:-1]]
    assert start["record_type"] == "session_start"
    assert end["record_type"] == "session_end"
    assert end["n_snapshots"] == n + 1
    assert len(snapshots) == n + 1
    assert [row["sequence"] for row in snapshots] == list(range(1, n + 2))
    assert snapshots[0]["trigger"] == "interval"
    assert snapshots[-1]["trigger"] == "mutation"
    assert restored.sessions["cap-1"].n_chunks == 3
    assert DEFAULT_CHUNK_SIZE == 250


def test_durable_fail_closed_on_storage_error() -> None:
    store = DurableCaptureStore(chunk_size=10)
    store.start_session(started_at=_stamp(0), interval_ms=500, session_id="cap-fail")
    store.append_snapshot(
        {
            "schema": "mexc_ui_raw_snapshot",
            "schema_version": 1,
            "sequence": 0,
            "received_at_local": _stamp(0),
            "observed_at_local": _stamp(0),
            "trigger": "manual",
            "selector_catalog_version": "v1",
            "page_host": "www.mexc.com",
            "page_path": "/futures/TAO_USDT",
            "observation_valid": True,
            "invalid_reasons": [],
            "changed_fields": [],
            "fields": {},
        }
    )
    store.fail_next_append("disk full")
    with pytest.raises(DurableStorageError, match="disk full"):
        store.append_snapshot(
            {
                "schema": "mexc_ui_raw_snapshot",
                "schema_version": 1,
                "sequence": 0,
                "received_at_local": _stamp(1),
                "observed_at_local": _stamp(1),
                "trigger": "interval",
                "selector_catalog_version": "v1",
                "page_host": "www.mexc.com",
                "page_path": "/futures/TAO_USDT",
                "observation_valid": True,
                "invalid_reasons": [],
                "changed_fields": [],
                "fields": {},
            }
        )
    meta = store.sessions["cap-fail"]
    assert meta.status == "failed"
    assert meta.storage_error == "disk full"
    assert meta.n_snapshots == 1
    with pytest.raises(DurableStorageError):
        store.append_snapshot(
            {
                "schema": "mexc_ui_raw_snapshot",
                "schema_version": 1,
                "sequence": 0,
                "received_at_local": _stamp(2),
                "observed_at_local": _stamp(2),
                "trigger": "interval",
                "selector_catalog_version": "v1",
                "page_host": "www.mexc.com",
                "page_path": "/futures/TAO_USDT",
                "observation_valid": True,
                "invalid_reasons": [],
                "changed_fields": [],
                "fields": {},
            }
        )


def test_quality_and_replay_smoke_from_durable_export() -> None:
    raw = REPO / "data" / "mexc_ui_capture" / "_pytest_hydration.ndjson"
    raw.parent.mkdir(parents=True, exist_ok=True)
    if raw.exists():
        raw.unlink()
    store = DurableCaptureStore(chunk_size=8)
    store.start_session(
        started_at=_stamp(0),
        interval_ms=500,
        session_id="cap-replay",
        page_path="/futures/TAO_USDT",
    )
    previous = None
    for index in range(12):
        bid = 100.00 + index * 0.05
        ask = 100.20 + index * 0.05
        html = f"""
        <div data-mexc-capture="symbol">TAOUSDT</div>
        <div data-mexc-capture="bid">{bid:.2f}</div>
        <div data-mexc-capture="ask">{ask:.2f}</div>
        <div data-mexc-capture="mark">{bid + 0.05:.2f}</div>
        <div data-mexc-capture="index">{bid + 0.02:.2f}</div>
        """
        snap = extract_html(
            html,
            received_at_local=_stamp(index * 500),
            sequence=0,
            previous=previous,
            monotonic_ms=float(index * 500),
            capture_id="cap-replay",
            trigger="interval" if index else "manual",
        )
        store.append_snapshot(snap.as_dict())
        previous = snap
    store.stop_session(ended_at=_stamp(12 * 500))
    raw.write_text(store.export_ndjson("cap-replay"), encoding="utf-8")
    quality = summarize_capture(raw)
    assert quality.n_raw == 12
    assert quality.n_valid_for_replay == 12
    assert quality.capture_id == "cap-replay"
    assert quality.trigger_counts["interval"] == 11
    assert quality.trigger_counts["manual"] == 1
    assert quality.interarrival_ms["p99_ms"] is not None
    assert quality.n_simultaneous_bid_ask_mark_index == 12
    assert quality.n_bid_ge_ask == 0
    assert not quality.sequence_diagnostics
    quality_b = summarize_capture(raw)
    assert quality.replay_determinism_sha256 == quality_b.replay_determinism_sha256
    report = replay_capture_smoke(raw, "author_observed_v0")
    assert report.observations == 12
    assert any("PIPELINE_SMOKE_ONLY" in note for note in report.notes)
    payloads = list(iter_raw_mappings(raw))
    assert len(payloads) == 12
    assert payloads[0]["capture_id"] == "cap-replay"
    raw.unlink(missing_ok=True)
