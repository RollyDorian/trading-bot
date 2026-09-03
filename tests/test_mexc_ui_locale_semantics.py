"""Locale-aware UI capture parsing. No mom/gap retune."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_bot.research.mexc_shadow.ui_capture.extract import extract_html
from trading_bot.research.mexc_shadow.ui_capture.locale_remediation import (
    historical_corpus_record,
    write_reports,
)
from trading_bot.research.mexc_shadow.ui_capture.normalize import observation_from_snapshot
from trading_bot.research.mexc_shadow.ui_capture.parse import (
    join_price_tokens,
    locale_from_pathname,
    parse_number,
    parse_price,
    symbol_from_futures_path,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "mexc_ui_capture"
BASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _stamp(offset_ms: int = 0) -> str:
    return (BASE + timedelta(milliseconds=offset_ms)).isoformat()


def test_locale_from_pathname_is_explicit() -> None:
    assert locale_from_pathname("/ru-RU/futures/TAO_USDT") == "ru-RU"
    assert locale_from_pathname("/en-US/futures/TAO_USDT") == "en-US"
    assert locale_from_pathname("/futures/TAO_USDT") == "unknown"
    assert locale_from_pathname("/zh-CN/futures/TAO_USDT") == "unknown"
    assert locale_from_pathname("/ru-RU/spot/TAO_USDT") == "unknown"


def test_symbol_from_futures_path_rejects_unrelated_routes() -> None:
    assert symbol_from_futures_path("/futures/TAO_USDT") == "TAOUSDT"
    assert symbol_from_futures_path("/ru-RU/futures/TAO_USDT") == "TAOUSDT"
    assert symbol_from_futures_path("/en-US/futures/TAO_USDT") == "TAOUSDT"
    assert symbol_from_futures_path("/ru-RU/spot/TAO_USDT") is None
    assert symbol_from_futures_path("/account/futures/TAO_USDT") is None
    assert symbol_from_futures_path("/futures/") is None


def test_parse_number_locale_semantics() -> None:
    assert parse_number("218,11", "ru-RU") == (218.11, None)
    assert parse_price("218,11", "ru-RU") == pytest.approx(218.11)
    assert parse_number("218.11", "en-US") == (218.11, None)
    assert parse_price("218.11", "en-US") == pytest.approx(218.11)
    assert parse_number("1,234.56", "en-US") == (1234.56, None)
    assert parse_number("1 234,56", "ru-RU") == (1234.56, None)
    assert parse_number("1\xa0234,56", "ru-RU") == (1234.56, None)
    assert parse_number("1.234,56", "ru-RU") == (1234.56, None)
    assert parse_number("0,0100%", "ru-RU") == (0.01, "percent")
    assert parse_number("-0,0018%/02:26:19", "ru-RU") == (-0.0018, "percent")
    assert parse_number("+0.0050%/01:00:00", "en-US") == (0.005, "percent")
    assert parse_price("+0.34%", "en-US") is None
    assert parse_number("218,11", "unknown") == (None, None)
    assert parse_number("1,234.56", "unknown") == (None, None)
    assert parse_number("1.234", "unknown") == (None, None)
    assert parse_number("218.11", "ru-RU") == (None, None)
    assert parse_number("218,11", "en-US") == (None, None)
    assert parse_price("\u200e218,11", "ru-RU") == pytest.approx(218.11)


def test_join_price_tokens_fails_closed_on_adjacent_digits() -> None:
    assert join_price_tokens(["218", ",", "11"]) == "218,11"
    assert join_price_tokens(["218,11"]) == "218,11"
    assert join_price_tokens(["218", "11"]) is None


def test_ru_header_and_wrapper_keep_dom_text_and_true_scale() -> None:
    snap = extract_html(
        (FIXTURES / "tao_ru_locale_header.html").read_text(encoding="utf-8"),
        received_at_local=_stamp(0),
        sequence=1,
        page_path="/ru-RU/futures/TAO_USDT",
        page_host="www.mexc.com",
        monotonic_ms=0.0,
    )
    rec = observation_from_snapshot(snap)
    assert rec.observation is not None
    obs = rec.observation
    assert obs.symbol == "TAOUSDT"
    assert snap.ui_locale == "ru-RU"
    assert snap.parser_mode == "ru-RU"
    assert obs.bid == pytest.approx(218.10)
    assert obs.ask == pytest.approx(218.16)
    assert obs.last == pytest.approx(218.11)
    assert obs.mark == pytest.approx(218.14)
    assert obs.index == pytest.approx(218.18)
    assert obs.bid < obs.ask
    assert obs.last != pytest.approx(21811)
    assert obs.bid != pytest.approx(21810)
    assert snap.fields["bid"].raw_text is not None
    assert "," in snap.fields["bid"].raw_text or "," in "".join(snap.fields["bid"].raw_tokens or ())
    assert snap.fields["bid"].raw_text != str(obs.bid)
    assert snap.fields["bid"].selector_id == "live_asks_bids_wrapper"
    assert snap.fields["bid"].parser_locale == "ru-RU"
    assert snap.fields["ask"].raw_tokens is not None
    assert snap.fields["mark"].selector_id == "header_struct:mark"
    assert snap.fields["index"].selector_id == "header_struct:index"
    assert snap.fields["funding"].value == pytest.approx(-0.0018)
    assert snap.fields["funding"].unit == "percent"
    assert snap.header_diagnostics["header_title_hits_mark"] >= 1
    assert snap.header_diagnostics["header_title_hits_index"] >= 1
    dumped = snap.as_dict()
    assert "html" not in dumped["header_diagnostics"]
    assert "page_html" not in dumped["header_diagnostics"]


def test_unknown_locale_does_not_guess_comma_decimals() -> None:
    snap = extract_html(
        (FIXTURES / "tao_ru_locale_header.html").read_text(encoding="utf-8"),
        received_at_local=_stamp(0),
        sequence=1,
        page_path="/futures/TAO_USDT",
        monotonic_ms=0.0,
    )
    assert snap.ui_locale == "unknown"
    assert snap.fields["bid"].parse_status == "missing"
    assert snap.fields["last"].parse_status == "unparsable"
    rec = observation_from_snapshot(snap)
    assert rec.observation is None


def test_en_us_path_still_parses_point_decimals() -> None:
    snap = extract_html(
        (FIXTURES / "tao_live_wrappers.html").read_text(encoding="utf-8"),
        received_at_local=_stamp(0),
        sequence=1,
        page_path="/en-US/futures/TAO_USDT",
        monotonic_ms=0.0,
    )
    rec = observation_from_snapshot(snap)
    assert rec.observation is not None
    assert rec.observation.bid == pytest.approx(226.10)
    assert rec.observation.ask == pytest.approx(226.18)
    assert rec.observation.last == pytest.approx(226.15)
    assert snap.ui_locale == "en-US"
    assert snap.fields["bid"].raw_text != str(rec.observation.bid) or "." in str(
        snap.fields["bid"].raw_text
    )


def test_extension_catalog_version_and_manifest() -> None:
    manifest = json.loads(
        (REPO / "extensions" / "mexc_ui_capture" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    catalog = json.loads(
        (REPO / "extensions" / "mexc_ui_capture" / "selector_catalog_v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["version"] == "1.3.0"
    assert catalog["catalog_version"] == "v1.1"
    assert "Справедливая цена" in catalog["fields"]["mark"]["labels"]
    assert "Индексная цена" in catalog["fields"]["index"]["labels"]
    assert catalog["market_header"]["root_class_contains"] == "contractDetail"


def test_historical_corpus_is_infrastructure_evidence_not_rewritten() -> None:
    record = historical_corpus_record(None)
    assert record["role"] == "CAPTURE_INFRASTRUCTURE_EVIDENCE"
    assert record["rewrite"] is False
    assert record["rescale"] is False


def test_locale_remediation_report_pending_without_operator_capture(tmp_path: Path) -> None:
    out_json = tmp_path / "remediation.json"
    out_md = tmp_path / "remediation.md"
    payload = write_reports(
        out_json=out_json,
        out_md=out_md,
        short_raw=None,
        historical_raw=None,
    )
    assert payload["decision"] == "GATE_PENDING_OPERATOR_CAPTURE"
    assert payload["paper"] is False
    assert payload["live"] is False
    assert payload["strategy_tuning"] is False
    assert "Справедливая цена" in payload["verified_header_aliases"]["ru-RU"]["mark"]
    assert out_md.read_text(encoding="utf-8").startswith("# MEXC UI locale")
