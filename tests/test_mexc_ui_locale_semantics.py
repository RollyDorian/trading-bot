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
from trading_bot.research.mexc_shadow.ui_capture.schema import sanitize_header_diagnostics

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


def test_public_header_emits_bounded_structural_probe() -> None:
    snap = extract_html(
        (FIXTURES / "tao_ru_locale_header.html").read_text(encoding="utf-8"),
        received_at_local=_stamp(0),
        sequence=1,
        page_path="/ru-RU/futures/TAO_USDT",
        capture_id="public-header",
        monotonic_ms=0.0,
    )

    probe = snap.as_dict()["header_diagnostics"]["market_header_probe"]
    assert probe["probe_version"] == 1
    assert probe["matched_item_count"] == 3
    assert probe["items_truncated"] is False
    assert len(probe["items"]) == 3
    mark_item = next(
        item for item in probe["items"] if "Справедливая цена" in item["visible_text"]
    )
    assert mark_item["current_title_token_matched"] is True
    assert mark_item["current_value_token_matched"] is True
    assert mark_item["direct_children"][0]["tag"] == "div"
    assert "outerHTML" not in json.dumps(probe)


def test_header_probe_records_title_class_mismatch_without_extracting_mark() -> None:
    snap = extract_html(
        (FIXTURES / "tao_header_title_class_mismatch.html").read_text(encoding="utf-8"),
        received_at_local=_stamp(0),
        sequence=1,
        page_path="/ru-RU/futures/TAO_USDT",
    )

    assert snap.fields["mark"].parse_status == "missing"
    assert snap.header_diagnostics["header_title_hits_mark"] == 0
    item = snap.as_dict()["header_diagnostics"]["market_header_probe"]["items"][0]
    assert item["current_title_token_matched"] is False
    assert item["current_value_token_matched"] is True
    assert "Справедливая цена" in item["visible_text"]
    assert item["descendant_attributes"] == [
        {"tag": "div", "attributes": {"title": "Рыночный ориентир"}}
    ]


def test_header_probe_records_value_class_mismatch_without_extracting_mark() -> None:
    snap = extract_html(
        (FIXTURES / "tao_header_value_class_mismatch.html").read_text(encoding="utf-8"),
        received_at_local=_stamp(0),
        sequence=1,
        page_path="/ru-RU/futures/TAO_USDT",
    )

    assert snap.fields["mark"].parse_status in {"missing", "unparsable"}
    assert snap.fields["mark"].value is None
    assert snap.header_diagnostics["header_title_hits_mark"] == 1
    item = snap.as_dict()["header_diagnostics"]["market_header_probe"]["items"][0]
    assert item["current_title_token_matched"] is True
    assert item["current_value_token_matched"] is False
    assert "218,14" in item["visible_text"]


def test_header_probe_records_unknown_title_without_guessing_a_field() -> None:
    snap = extract_html(
        (FIXTURES / "tao_header_unknown_title.html").read_text(encoding="utf-8"),
        received_at_local=_stamp(0),
        sequence=1,
        page_path="/ru-RU/futures/TAO_USDT",
    )

    assert all(snap.fields[name].parse_status == "missing" for name in ("mark", "index", "funding"))
    assert snap.header_diagnostics["header_title_hits_mark"] == 0
    assert snap.header_diagnostics["header_title_hits_index"] == 0
    assert snap.header_diagnostics["header_title_hits_funding"] == 0
    item = snap.as_dict()["header_diagnostics"]["market_header_probe"]["items"][0]
    assert item["current_title_token_matched"] is True
    assert item["current_value_token_matched"] is True
    assert "Расчетная база" in item["visible_text"]


def test_header_probe_is_bounded_and_redacts_private_ui_subtrees() -> None:
    snap = extract_html(
        (FIXTURES / "tao_header_probe_bounds.html").read_text(encoding="utf-8"),
        received_at_local=_stamp(0),
        sequence=1,
        page_path="/ru-RU/futures/TAO_USDT",
    )

    probe = snap.as_dict()["header_diagnostics"]["market_header_probe"]
    assert probe["matched_item_count"] == 14
    assert probe["items_truncated"] is True
    assert len(probe["items"]) == 12
    assert all(len(item["class_string"]) <= 240 for item in probe["items"])
    assert all(len(item["visible_text_tokens"]) <= 16 for item in probe["items"])
    dumped = json.dumps(probe, ensure_ascii=False).lower()
    for forbidden in ("outside-private", "secret", "account", "balance", "999999"):
        assert forbidden not in dumped


def test_header_probe_schema_drops_unknown_keys_and_scrubs_private_text() -> None:
    sanitized = sanitize_header_diagnostics(
        {
            "market_header_probe": {
                "matched_item_count": 10000,
                "outerHTML": "<body>SECRET</body>",
                "items": [
                    {
                        "item_index": 0,
                        "tag": "div",
                        "visible_text": "Available balance SECRET 999999",
                        "visible_text_tokens": ["SECRET"] * 30,
                        "attributes": {"aria-label": "account secret", "data-user": "x"},
                        "direct_children": [],
                    }
                ]
                * 20,
            }
        }
    )["market_header_probe"]

    assert sanitized["matched_item_count"] == 999
    assert sanitized["items_truncated"] is True
    assert len(sanitized["items"]) == 12
    dumped = json.dumps(sanitized).lower()
    assert "outerhtml" not in dumped
    assert "data-user" not in dumped
    assert "secret" not in dumped
    assert "account" not in dumped
    assert "999999" not in dumped


def test_header_probe_emits_once_per_session_or_changed_structure() -> None:
    public_html = (FIXTURES / "tao_ru_locale_header.html").read_text(encoding="utf-8")
    first = extract_html(
        public_html,
        received_at_local=_stamp(0),
        sequence=1,
        page_path="/ru-RU/futures/TAO_USDT",
        capture_id="session-a",
    )
    price_changed = extract_html(
        public_html.replace("218,14", "218,15"),
        received_at_local=_stamp(1),
        sequence=2,
        page_path="/ru-RU/futures/TAO_USDT",
        capture_id="session-a",
        previous=first,
    )
    structural_change = extract_html(
        (FIXTURES / "tao_header_title_class_mismatch.html").read_text(encoding="utf-8"),
        received_at_local=_stamp(2),
        sequence=3,
        page_path="/ru-RU/futures/TAO_USDT",
        capture_id="session-a",
        previous=price_changed,
    )
    new_session = extract_html(
        public_html,
        received_at_local=_stamp(3),
        sequence=1,
        page_path="/ru-RU/futures/TAO_USDT",
        capture_id="session-b",
        previous=structural_change,
    )

    assert first.header_diagnostics["market_header_probe"] is not None
    assert price_changed.header_diagnostics["market_header_probe"] is None
    assert structural_change.header_diagnostics["market_header_probe"] is not None
    assert new_session.header_diagnostics["market_header_probe"] is not None


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
    assert manifest["version"] == "1.3.1"
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
