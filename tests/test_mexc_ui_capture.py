"""Synthetic DOM capture, fail-closed selectors, and frozen-profile smoke."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_bot.research.mexc_shadow.profiles import author_observed_v0
from trading_bot.research.mexc_shadow.safety import (
    extension_source_violations,
    package_import_violations,
    package_source_violations,
)
from trading_bot.research.mexc_shadow.shadow import ShadowBook, executable_pnl_bps
from trading_bot.research.mexc_shadow.source import MexcUiObserver
from trading_bot.research.mexc_shadow.types import Candidate
from trading_bot.research.mexc_shadow.ui_capture.catalog import SELECTOR_CATALOG
from trading_bot.research.mexc_shadow.ui_capture.extract import extract_html
from trading_bot.research.mexc_shadow.ui_capture.normalize import observation_from_snapshot
from trading_bot.research.mexc_shadow.ui_capture.quality import summarize_capture
from trading_bot.research.mexc_shadow.ui_capture.replay import (
    CaptureNdjsonSource,
    replay_capture_smoke,
)
from trading_bot.research.mexc_shadow.ui_capture.store import append_snapshot

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "mexc_ui_capture"
EXTENSION_CATALOG = REPO / "extensions" / "mexc_ui_capture" / "selector_catalog_v1.json"
BASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _stamp(offset_ms: int) -> str:
    return (BASE + timedelta(milliseconds=offset_ms)).isoformat()


def test_extension_catalog_matches_python_catalog() -> None:
    loaded = json.loads(EXTENSION_CATALOG.read_text(encoding="utf-8"))
    assert loaded == SELECTOR_CATALOG


def test_mv3_web_accessible_resources_matches_are_chrome_origin_wide() -> None:
    """Chrome rejects WAR match patterns whose path is not exactly /*."""

    manifest = json.loads(
        (REPO / "extensions" / "mexc_ui_capture" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["manifest_version"] == 3
    war = manifest["web_accessible_resources"]
    assert len(war) == 1
    war_matches = war[0]["matches"]
    # Chrome: "Invalid match pattern" if the path is anything other than /*.
    chrome_war_match = re.compile(r"^https://[^/\s]+/\*$")
    assert war_matches == [
        "https://www.mexc.com/*",
        "https://futures.mexc.com/*",
    ]
    assert all(chrome_war_match.fullmatch(pattern) for pattern in war_matches)
    assert "https://www.mexc.com/futures/*" not in war_matches
    # Injection and host access stay futures-scoped; only WAR matches are origin-wide.
    assert manifest["host_permissions"] == [
        "https://www.mexc.com/futures/*",
        "https://futures.mexc.com/*",
    ]
    assert manifest["content_scripts"][0]["matches"] == [
        "https://www.mexc.com/futures/*",
        "https://futures.mexc.com/*",
    ]


def test_synthetic_dom_captures_header_and_orderbook_not_ticket() -> None:
    snap = extract_html(
        _html("tao_page_fixture_v1.html"),
        received_at_local=_stamp(0),
        sequence=1,
        page_path="/futures/TAO_USDT",
    )
    assert snap.observation_valid is True
    rec = observation_from_snapshot(snap)
    assert rec.observation is not None
    obs = rec.observation
    assert obs.symbol == "TAOUSDT"
    assert obs.bid == pytest.approx(226.10)
    assert obs.ask == pytest.approx(226.18)
    assert obs.bid != 999.99
    assert obs.mark == pytest.approx(226.12)
    assert obs.index == pytest.approx(226.10)
    assert obs.last == pytest.approx(226.15)
    assert obs.mid is None
    funding = snap.fields["funding"]
    assert funding.parse_status == "ok"
    assert funding.value == pytest.approx(0.01)
    assert funding.unit == "percent"


def test_ambiguous_index_marks_snapshot_invalid_but_keeps_raw() -> None:
    snap = extract_html(
        _html("tao_ambiguous_index.html"),
        received_at_local=_stamp(0),
        sequence=1,
    )
    assert snap.observation_valid is False
    assert any(reason.startswith("ambiguous:index") for reason in snap.invalid_reasons)
    rec = observation_from_snapshot(snap)
    assert rec.observation is None
    assert snap.fields["bid"].value == pytest.approx(100.0)


def test_missing_values_stay_null() -> None:
    html = """
    <div data-mexc-capture="symbol">TAOUSDT</div>
    <div data-mexc-capture="bid">100.00</div>
    <div data-mexc-capture="ask">100.02</div>
    <div><span>Index Price</span><span>--</span></div>
    """
    snap = extract_html(html, received_at_local=_stamp(0), sequence=1)
    assert snap.fields["index"].parse_status == "missing"
    assert snap.fields["index"].value is None
    rec = observation_from_snapshot(snap)
    assert rec.observation is not None
    assert rec.observation.mid is None


def _scratch_ndjson() -> Path:
    path = REPO / "data" / "mexc_ui_capture" / "_pytest.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    return path


def test_append_only_and_deterministic_replay() -> None:
    raw = _scratch_ndjson()
    html = _html("tao_page_fixture_v1.html")
    first = extract_html(html, received_at_local=_stamp(0), sequence=1)
    second = extract_html(
        html.replace("226.10", "226.11").replace("226.18", "226.19"),
        received_at_local=_stamp(500),
        sequence=2,
        previous=first,
    )
    append_snapshot(raw, first)
    append_snapshot(raw, second)
    before = raw.read_bytes()
    append_snapshot(raw, second)
    after = raw.read_bytes()
    assert after.startswith(before)
    assert after.count(b"\n") == 3
    quality_a = summarize_capture(raw)
    quality_b = summarize_capture(raw)
    assert quality_a.replay_determinism_sha256 == quality_b.replay_determinism_sha256
    rows_a = list(CaptureNdjsonSource(raw).iter_observations())
    rows_b = list(CaptureNdjsonSource(raw).iter_observations())
    assert [(row.bid, row.ask, row.received_at) for row in rows_a] == [
        (row.bid, row.ask, row.received_at) for row in rows_b
    ]
    raw.unlink(missing_ok=True)


def test_shadow_uses_executable_bid_ask_not_mid() -> None:
    entry_html = """
    <div data-mexc-capture="symbol">TAOUSDT</div>
    <div data-mexc-capture="bid">100.00</div>
    <div data-mexc-capture="ask">100.20</div>
    """
    exit_html = """
    <div data-mexc-capture="symbol">TAOUSDT</div>
    <div data-mexc-capture="bid">100.40</div>
    <div data-mexc-capture="ask">100.50</div>
    """
    entry_snap = extract_html(entry_html, received_at_local=_stamp(0), sequence=1)
    exit_snap = extract_html(exit_html, received_at_local=_stamp(1000), sequence=2)
    entry = observation_from_snapshot(entry_snap).observation
    exit_obs = observation_from_snapshot(exit_snap).observation
    assert entry is not None and exit_obs is not None
    long_bps = executable_pnl_bps("long", entry.bid, entry.ask, exit_obs.bid, exit_obs.ask)
    short_bps = executable_pnl_bps("short", entry.bid, entry.ask, exit_obs.bid, exit_obs.ask)
    mid_long = (exit_obs.executable_mid() / entry.executable_mid() - 1.0) * 10_000.0
    assert long_bps == pytest.approx((exit_obs.bid / entry.ask - 1.0) * 10_000.0)
    assert short_bps == pytest.approx((entry.bid / exit_obs.ask - 1.0) * 10_000.0)
    assert long_bps != pytest.approx(mid_long)
    book = ShadowBook(author_observed_v0().shadow)
    candidate = Candidate(
        observed_at=entry.observed_at,
        symbol=entry.symbol,
        direction="long",
        mom_bps=4.0,
        gap_bps=-3.0,
        target_bps=6.0,
        throttle="accepted",
        accepted_for_shadow=True,
        notional_multiplier=1.0,
    )
    book.maybe_open(candidate, entry, author_observed_v0().shadow)
    trade = book.on_observation(exit_obs)
    assert trade is not None
    assert trade.gross_bps == pytest.approx(long_bps)


def test_frozen_profile_replay_is_pipeline_smoke_only() -> None:
    raw = _scratch_ndjson()
    previous = None
    for index in range(8):
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
            sequence=index + 1,
            previous=previous,
        )
        append_snapshot(raw, snap)
        previous = snap
    report = replay_capture_smoke(raw, "author_observed_v0")
    assert report.observations == 8
    assert any("PIPELINE_SMOKE_ONLY" in note for note in report.notes)
    observer_rows = list(
        MexcUiObserver(
            [
                json.loads(line)
                for line in raw.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        ).iter_observations()
    )
    assert len(observer_rows) == 8
    assert all(row.source == "mexc_ui_capture_v1" for row in observer_rows)
    raw.unlink(missing_ok=True)


def test_capture_and_extension_safety_boundaries() -> None:
    assert package_source_violations() == []
    assert package_import_violations() == []
    assert extension_source_violations() == []
