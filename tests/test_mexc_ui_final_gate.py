"""Final locale/header gate. Does not run mom/gap replay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading_bot.research.mexc_shadow.ui_capture.extract import extract_html
from trading_bot.research.mexc_shadow.ui_capture.final_gate import (
    REQUIRED_PATH_PREFIX,
    score_final_gate,
    write_reports,
)
from trading_bot.research.mexc_shadow.ui_capture.store import append_snapshot

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "mexc_ui_capture"
BASE = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _write_pair(path: Path, page_path: str, gap_minutes: int) -> None:
    html = (FIXTURES / "tao_logged_in_ru_header_probe.html").read_text(encoding="utf-8")
    for index, offset in enumerate((0, gap_minutes * 60_000)):
        snap = extract_html(
            html,
            received_at_local=(BASE + timedelta(milliseconds=offset)).isoformat(),
            sequence=index + 1,
            page_path=page_path,
            capture_id="final-gate-test",
            monotonic_ms=float(offset),
        )
        append_snapshot(path, snap)


def test_required_path_prefix_is_localized_ru() -> None:
    assert REQUIRED_PATH_PREFIX == "/ru-RU/futures/"


def test_ru_path_fixture_is_not_a_pass_with_sparse_samples(tmp_path: Path) -> None:
    raw = tmp_path / "ru.ndjson"
    _write_pair(raw, "/ru-RU/futures/TAO_USDT", gap_minutes=6)
    scored = score_final_gate(raw)
    assert scored["gates"]["page_path_ru_RU_futures"] is True
    assert scored["gates"]["parser_locale_ru_RU"] is True
    assert scored["gates"]["catalog_v1_2"] is True
    assert scored["gates"]["header_alias_count_positive"] is True
    assert scored["gates"]["symbol_taousdt"] is True
    assert scored["gates"]["duration_5_to_15_min"] is True
    assert scored["gates"]["raw_text_ru_decimal_comma"] is True
    assert scored["simultaneous_pct"] == 100.0
    assert scored["n_missing_field_bursts"] == 0
    assert scored["gates"]["frozen_interarrival"] is False
    assert scored["passed"] is False
    assert "frozen_interarrival" in scored["failure_classes"]


def test_nonlocalized_futures_path_fails_ru_locale_gate(tmp_path: Path) -> None:
    raw = tmp_path / "bare.ndjson"
    _write_pair(raw, "/futures/TAO_USDT", gap_minutes=6)
    scored = score_final_gate(raw)
    assert scored["gates"]["page_path_ru_RU_futures"] is False
    assert scored["gates"]["parser_locale_ru_RU"] is False
    assert "page_path_ru_RU_futures" in scored["failure_classes"]
    assert "parser_locale_ru_RU" in scored["failure_classes"]


def test_final_gate_report_writes_fail_status(tmp_path: Path) -> None:
    raw = tmp_path / "ru.ndjson"
    _write_pair(raw, "/ru-RU/futures/TAO_USDT", gap_minutes=6)
    out_json = tmp_path / "gate.json"
    out_md = tmp_path / "gate.md"
    payload = write_reports(raw=raw, out_json=out_json, out_md=out_md)
    assert payload["status"] == "MEXC_UI_LOCALE_DATA_SEMANTICS_FINAL_GATE_FAIL"
    assert payload["decision"] == "STOP_FOR_LEAD_REVIEW"
    assert payload["mom_gap_inspected"] is False
    assert payload["long_capture_started"] is False
    assert "FINAL_GATE_FAIL" in out_md.read_text(encoding="utf-8")
