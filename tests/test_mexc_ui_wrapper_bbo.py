"""Fail-closed live BBO: wrappers first, headings fallback, visibility before ambiguity."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_bot.research.mexc_shadow.ui_capture.extract import extract_html
from trading_bot.research.mexc_shadow.ui_capture.normalize import (
    observation_from_snapshot,
    snapshot_from_mapping,
)
from trading_bot.research.mexc_shadow.ui_capture.schema import ORDERBOOK_DIAGNOSTIC_INT_KEYS

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "mexc_ui_capture"
BASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _stamp(offset_ms: int = 0) -> str:
    return (BASE + timedelta(milliseconds=offset_ms)).isoformat()


def _extract(name: str):
    return extract_html(
        _html(name),
        received_at_local=_stamp(0),
        sequence=1,
        page_path="/futures/TAO_USDT",
        monotonic_ms=0.0,
    )


def test_extension_manifest_is_1_3_1_header_probe() -> None:
    manifest = json.loads(
        (REPO / "extensions" / "mexc_ui_capture" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["version"] == "1.3.1"


def _assert_diag_shape(snap) -> None:
    diag = snap.orderbook_diagnostics
    for key in ORDERBOOK_DIAGNOSTIC_INT_KEYS:
        assert key in diag
        assert isinstance(diag[key], int)
        assert diag[key] >= 0
    assert "chosen_bbo_source" in diag
    assert "ambiguity_reason" in diag
    dumped = snap.as_dict()["orderbook_diagnostics"]
    assert "html" not in dumped
    assert "page_html" not in dumped
    assert "inner_html" not in dumped


def test_two_headings_one_wrapper_pair_is_valid() -> None:
    snap = _extract("tao_two_headings_one_wrapper.html")
    _assert_diag_shape(snap)
    rec = observation_from_snapshot(snap)
    assert rec.observation is not None
    assert rec.observation.bid == pytest.approx(226.10)
    assert rec.observation.ask == pytest.approx(226.18)
    assert snap.observation_valid is True
    assert "ambiguous_orderbook_heading" not in snap.invalid_reasons
    assert snap.fields["bid"].selector_id == "live_asks_bids_wrapper"
    diag = snap.orderbook_diagnostics
    assert diag["orderbook_heading_count"] == 2
    assert diag["visible_orderbook_heading_count"] == 2
    assert diag["visible_asks_wrapper_count"] == 1
    assert diag["visible_bids_wrapper_count"] == 1
    assert diag["chosen_bbo_source"] == "live_asks_bids_wrapper"
    assert diag["ambiguity_reason"] is None


def test_hidden_duplicate_heading_one_wrapper_is_valid() -> None:
    snap = _extract("tao_hidden_heading_one_wrapper.html")
    rec = observation_from_snapshot(snap)
    assert rec.observation is not None
    assert rec.observation.bid == pytest.approx(226.10)
    assert rec.observation.ask == pytest.approx(226.18)
    assert snap.observation_valid is True
    diag = snap.orderbook_diagnostics
    assert diag["orderbook_heading_count"] == 2
    assert diag["visible_orderbook_heading_count"] == 1
    assert diag["chosen_bbo_source"] == "live_asks_bids_wrapper"


def test_hidden_duplicate_wrapper_one_visible_pair_is_valid() -> None:
    snap = _extract("tao_hidden_wrapper_one_visible.html")
    rec = observation_from_snapshot(snap)
    assert rec.observation is not None
    assert rec.observation.bid == pytest.approx(226.10)
    assert rec.observation.ask == pytest.approx(226.18)
    assert rec.observation.bid != pytest.approx(330.10)
    assert snap.observation_valid is True
    diag = snap.orderbook_diagnostics
    assert diag["asks_wrapper_count"] == 2
    assert diag["bids_wrapper_count"] == 2
    assert diag["visible_asks_wrapper_count"] == 1
    assert diag["visible_bids_wrapper_count"] == 1
    assert diag["chosen_bbo_source"] == "live_asks_bids_wrapper"
    assert diag["ambiguity_reason"] is None


def test_two_visible_wrapper_pairs_conflicting_bbo_is_invalid() -> None:
    snap = _extract("tao_two_visible_wrapper_conflict.html")
    rec = observation_from_snapshot(snap)
    assert rec.observation is None
    assert snap.observation_valid is False
    assert "ambiguous_live_orderbook" in snap.invalid_reasons
    # Wrapper path was available, so heading fallback must not invent a BBO.
    assert snap.fields["bid"].parse_status == "missing"
    diag = snap.orderbook_diagnostics
    assert diag["visible_asks_wrapper_count"] == 2
    assert diag["visible_bids_wrapper_count"] == 2
    assert diag["chosen_bbo_source"] == "none"
    assert diag["ambiguity_reason"] == "ambiguous_live_orderbook"


def test_wrapper_bbo_valid_while_heading_path_ambiguous() -> None:
    snap = _extract("tao_two_headings_one_wrapper.html")
    assert snap.observation_valid is True
    assert snap.orderbook_diagnostics["visible_orderbook_heading_count"] == 2
    assert snap.orderbook_diagnostics["chosen_bbo_source"] == "live_asks_bids_wrapper"


def test_no_wrappers_one_heading_fallback_is_valid() -> None:
    snap = _extract("tao_live_orderbook_panel.html")
    rec = observation_from_snapshot(snap)
    assert rec.observation is not None
    assert rec.observation.bid == pytest.approx(226.10)
    assert rec.observation.ask == pytest.approx(226.18)
    assert snap.fields["bid"].selector_id == "live_orderbook_split_by_last"
    diag = snap.orderbook_diagnostics
    assert diag["asks_wrapper_count"] == 0
    assert diag["bids_wrapper_count"] == 0
    assert diag["visible_orderbook_heading_count"] == 1
    assert diag["chosen_bbo_source"] == "live_orderbook_heading_fallback"
    assert diag["ambiguity_reason"] is None


def test_crossed_wrapper_bbo_is_invalid() -> None:
    snap = _extract("tao_crossed_wrapper.html")
    rec = observation_from_snapshot(snap)
    assert rec.observation is None
    assert snap.observation_valid is False
    assert "crossed_wrapper_bbo" in snap.invalid_reasons
    assert snap.fields["bid"].parse_status == "missing"
    assert snap.fields["ask"].parse_status == "missing"
    diag = snap.orderbook_diagnostics
    assert diag["chosen_bbo_source"] == "none"
    assert diag["ambiguity_reason"] == "crossed_wrapper_bbo"


def test_ticket_prices_cannot_become_bbo() -> None:
    snap = _extract("tao_live_wrappers.html")
    rec = observation_from_snapshot(snap)
    assert rec.observation is not None
    assert rec.observation.bid == pytest.approx(226.10)
    assert rec.observation.ask == pytest.approx(226.18)
    assert rec.observation.bid != pytest.approx(888.88)
    assert rec.observation.ask != pytest.approx(999.99)
    assert snap.fields["bid"].selector_id == "live_asks_bids_wrapper"
    assert snap.orderbook_diagnostics["chosen_bbo_source"] == "live_asks_bids_wrapper"


def test_wrapper_bbo_does_not_split_sides_by_last() -> None:
    snap = _extract("tao_wrapper_stale_last.html")
    rec = observation_from_snapshot(snap)
    assert rec.observation is not None
    assert rec.observation.last == pytest.approx(100.00)
    assert rec.observation.bid == pytest.approx(226.10)
    assert rec.observation.ask == pytest.approx(226.18)
    assert snap.fields["bid"].selector_id == "live_asks_bids_wrapper"


def test_heading_only_duplicate_panels_stay_invalid() -> None:
    snap = _extract("tao_ambiguous_orderbook.html")
    assert snap.observation_valid is False
    assert "ambiguous_orderbook_heading" in snap.invalid_reasons
    diag = snap.orderbook_diagnostics
    assert diag["asks_wrapper_count"] == 0
    assert diag["chosen_bbo_source"] == "none"
    assert diag["ambiguity_reason"] == "ambiguous_orderbook_heading"


def test_diagnostics_roundtrip_drops_html_payloads() -> None:
    snap = _extract("tao_live_wrappers.html")
    clean = snapshot_from_mapping(snap.as_dict())
    assert clean.orderbook_diagnostics["chosen_bbo_source"] == "live_asks_bids_wrapper"
    payload = snap.as_dict()
    payload["orderbook_diagnostics"]["page_html"] = "<html>secret</html>"
    payload["orderbook_diagnostics"]["chosen_bbo_source"] = "not_a_source"
    rebuilt = snapshot_from_mapping(payload)
    assert "page_html" not in rebuilt.orderbook_diagnostics
    assert rebuilt.orderbook_diagnostics["chosen_bbo_source"] == "none"
    assert rebuilt.orderbook_diagnostics["visible_asks_wrapper_count"] == 1
