"""Protocol v2.0.0 short-gate data contract. Does not run mom/gap cells."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trading_bot.research.mexc_shadow.ui_capture.durable import (
    DEFAULT_CHUNK_SIZE,
    SessionMeta,
    session_end_record,
    session_start_record,
)
from trading_bot.research.mexc_shadow.ui_capture.extract import extract_html
from trading_bot.research.mexc_shadow.ui_capture.v2_contract_gate import (
    REQUIRED_CATALOG,
    score_v2_contract_gate,
    write_reports,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "mexc_ui_capture"
BASE = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
INTERVAL_MS = 500
DENSE_DURATION_MS = 5 * 60 * 1000
DENSE_N = DENSE_DURATION_MS // INTERVAL_MS + 1


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _dump(path: Path, objects: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for obj in objects:
            handle.write(
                json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                + "\n"
            )


def _base_snapshot(*, page_path: str, document_lang: str | None = None) -> dict[str, Any]:
    html_name = (
        "tao_logged_in_ru_header_probe.html"
        if page_path.startswith("/ru-RU/")
        else "tao_live_wrappers.html"
    )
    snap = extract_html(
        _html(html_name),
        received_at_local=BASE.isoformat(),
        sequence=1,
        page_path=page_path,
        page_host="www.mexc.com",
        trigger="interval",
        sample_interval_ms=INTERVAL_MS,
        monotonic_ms=0.0,
        capture_id="v2-gate-test",
        document_lang=document_lang,
    )
    return snap.as_dict()


def _clone(
    base: dict[str, Any],
    *,
    sequence: int,
    offset_ms: int,
    trigger: str = "interval",
) -> dict[str, Any]:
    row: dict[str, Any] = json.loads(json.dumps(base))
    stamp = (BASE + timedelta(milliseconds=offset_ms)).isoformat()
    row["sequence"] = sequence
    row["received_at_local"] = stamp
    row["observed_at_local"] = stamp
    row["monotonic_ms"] = float(offset_ms)
    row["trigger"] = trigger
    row["sample_interval_ms"] = INTERVAL_MS
    row["capture_id"] = "v2-gate-test"
    return row


def _session_pair(
    *, n_snapshots: int, duration_ms: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    meta = SessionMeta(
        session_id="v2-gate-test",
        started_at=BASE.isoformat(),
        interval_ms=INTERVAL_MS,
        page_host="www.mexc.com",
        page_path="/futures/TAO_USDT",
        ended_at=(BASE + timedelta(milliseconds=duration_ms)).isoformat(),
        status="stopped",
        n_snapshots=n_snapshots,
        n_chunks=math.ceil(n_snapshots / DEFAULT_CHUNK_SIZE) if n_snapshots else 0,
        first_sequence=1 if n_snapshots else None,
        last_sequence=n_snapshots if n_snapshots else None,
        chunk_size=DEFAULT_CHUNK_SIZE,
        storage_error=None,
    )
    return session_start_record(meta), session_end_record(meta)


def _write_pair(path: Path, page_path: str, gap_minutes: int) -> None:
    base = _base_snapshot(page_path=page_path)
    start, end = _session_pair(n_snapshots=2, duration_ms=gap_minutes * 60_000)
    rows = [
        start,
        _clone(base, sequence=1, offset_ms=0),
        _clone(base, sequence=2, offset_ms=gap_minutes * 60_000),
        end,
    ]
    _dump(path, rows)


def test_catalog_lock_is_v1_2() -> None:
    assert REQUIRED_CATALOG == "v1.2"


def test_bare_path_document_lang_is_v2_locale_route(tmp_path: Path) -> None:
    raw = tmp_path / "bare-en.ndjson"
    _write_pair(raw, "/futures/TAO_USDT", gap_minutes=6)
    scored = score_v2_contract_gate(raw)
    assert scored["protocol_version_scored"] == "2.0.0"
    assert scored["protocol_version_not_scored"] == "1.0.0"
    assert scored["gates"]["locale_v2_provenance"] is True
    assert scored["locale_routes"].get("document_lang") == 2
    assert scored["n_locale_path_document_disagree"] == 0
    assert scored["gates"]["duration_5_to_15_min"] is True
    assert scored["gates"]["catalog_v1_2"] is True
    assert scored["gates"]["heartbeat_unchanged_interval_commits"] is True
    assert scored["gates"]["frozen_interarrival_p95"] is False
    assert scored["gates"]["frozen_interarrival_le_2000"] is False
    assert "frozen_interarrival_p95" in scored["failure_classes"]
    assert "frozen_interarrival_le_2000" in scored["failure_classes"]
    assert scored["passed"] is False


def test_ru_path_is_v2_path_locale_route(tmp_path: Path) -> None:
    raw = tmp_path / "ru-path.ndjson"
    _write_pair(raw, "/ru-RU/futures/TAO_USDT", gap_minutes=6)
    scored = score_v2_contract_gate(raw)
    assert scored["gates"]["locale_v2_provenance"] is True
    assert scored["locale_routes"].get("path") == 2
    assert scored["locales"].get("ru-RU") == 2
    assert scored["passed"] is False
    assert "frozen_interarrival_p95" in scored["failure_classes"]


def test_unknown_document_lang_on_bare_path_fails_locale(tmp_path: Path) -> None:
    raw = tmp_path / "unknown.ndjson"
    base = _base_snapshot(page_path="/futures/TAO_USDT", document_lang="zh-CN")
    start, end = _session_pair(n_snapshots=2, duration_ms=6 * 60_000)
    _dump(
        raw,
        [
            start,
            _clone(base, sequence=1, offset_ms=0),
            _clone(base, sequence=2, offset_ms=6 * 60_000),
            end,
        ],
    )
    scored = score_v2_contract_gate(raw)
    assert scored["gates"]["locale_v2_provenance"] is False
    assert "locale_v2_provenance" in scored["failure_classes"]


def test_duration_outside_5_to_15_fails(tmp_path: Path) -> None:
    raw = tmp_path / "short.ndjson"
    _write_pair(raw, "/futures/TAO_USDT", gap_minutes=1)
    scored = score_v2_contract_gate(raw)
    assert scored["gates"]["duration_5_to_15_min"] is False
    assert "duration_5_to_15_min" in scored["failure_classes"]


def test_dense_500ms_heartbeat_passes_v2_short_bars(tmp_path: Path) -> None:
    raw = tmp_path / "dense.ndjson"
    base = _base_snapshot(page_path="/ru-RU/futures/TAO_USDT")
    start, end = _session_pair(n_snapshots=DENSE_N, duration_ms=DENSE_DURATION_MS)
    rows: list[dict[str, Any]] = [start]
    for index in range(DENSE_N):
        trigger = "interval" if index else "manual"
        rows.append(
            _clone(
                base,
                sequence=index + 1,
                offset_ms=index * INTERVAL_MS,
                trigger=trigger,
            )
        )
    rows.append(end)
    _dump(raw, rows)
    scored = score_v2_contract_gate(raw)
    assert scored["n_snapshots"] == DENSE_N
    assert scored["gates"]["duration_5_to_15_min"] is True
    assert scored["gates"]["configured_interval_500_ms"] is True
    assert scored["gates"]["heartbeat_unchanged_interval_commits"] is True
    assert scored["n_unchanged_interval_commits"] >= DENSE_N - 2
    assert scored["gates"]["frozen_interarrival_p95"] is True
    assert scored["gates"]["frozen_interarrival_le_2000"] is True
    assert scored["gates"]["locale_v2_provenance"] is True
    assert scored["gates"]["schema_mexc_ui_raw_snapshot_v1"] is True
    assert scored["gates"]["catalog_v1_2"] is True
    assert scored["gates"]["sequence_chunk_continuity"] is True
    assert scored["gates"]["no_storage_errors"] is True
    assert scored["gates"]["simultaneous_coverage_ge_95pct"] is True
    assert scored["gates"]["bid_lt_ask"] is True
    assert scored["gates"]["mark_index_not_swapped"] is True
    assert scored["gates"]["no_post_readiness_data_invalid"] is True
    assert scored["gates"]["symbol_taousdt"] is True
    assert scored["gates"]["export_replay_deterministic"] is True
    assert scored["passed"] is True
    assert scored["failure_classes"] == []
    assert scored["hours_requirement"]["applied"] is False


def test_v2_contract_report_writes_fail_status(tmp_path: Path) -> None:
    raw = tmp_path / "bare-en.ndjson"
    _write_pair(raw, "/futures/TAO_USDT", gap_minutes=6)
    out_json = tmp_path / "gate.json"
    out_md = tmp_path / "gate.md"
    payload = write_reports(raw=raw, out_json=out_json, out_md=out_md)
    assert payload["status"] == "MEXC_UI_CAPTURE_V2_CONTRACT_FINAL_GATE_FAIL"
    assert payload["decision"] == "STOP_FOR_LEAD_REVIEW"
    assert payload["mom_gap_inspected"] is False
    assert payload["n_cells_executed"] == 0
    assert payload["long_capture_started"] is False
    assert payload["protocol"]["version_scored"] == "2.0.0"
    text = out_md.read_text(encoding="utf-8")
    assert "V2_CONTRACT_FINAL_GATE_FAIL" in text
    assert "v2.0.0" in text
    assert "v1.0.0" in text
