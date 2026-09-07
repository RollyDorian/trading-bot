"""Interval heartbeat and locale provenance. No mom/gap retune."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_bot.research.mexc_shadow.ui_capture.durable import DurableCaptureStore
from trading_bot.research.mexc_shadow.ui_capture.extract import extract_html
from trading_bot.research.mexc_shadow.ui_capture.parse import (
    locale_from_document_lang,
    locale_from_pathname,
    resolve_locale_context,
)
from trading_bot.research.mexc_shadow.ui_capture.schema import CaptureTrigger, UiRawSnapshot

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "mexc_ui_capture"
CONTENT = (REPO / "extensions" / "mexc_ui_capture" / "content.js").read_text(
    encoding="utf-8"
)
BASE = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
INTERVAL_MS = 500


def _stamp(offset_ms: int) -> str:
    return (BASE + timedelta(milliseconds=offset_ms)).isoformat()


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _tick(
    html: str,
    *,
    sequence: int,
    offset_ms: int,
    trigger: CaptureTrigger,
    page_path: str,
    previous: UiRawSnapshot | None = None,
    document_lang: str | None = None,
    capture_id: str = "cadence",
) -> UiRawSnapshot:
    return extract_html(
        html,
        received_at_local=_stamp(offset_ms),
        sequence=sequence,
        page_path=page_path,
        page_host="www.mexc.com",
        trigger=trigger,
        sample_interval_ms=INTERVAL_MS,
        previous=previous,
        monotonic_ms=float(offset_ms),
        capture_id=capture_id,
        document_lang=document_lang,
    )


def test_content_script_commits_unchanged_interval_ticks() -> None:
    assert "lastEmitKey" not in CONTENT
    assert 'trigger === "interval" && key === lastEmitKey' not in CONTENT
    assert "Equal bid/ask/last/mark/index" in CONTENT
    assert "resolveLocaleContext" in CONTENT
    assert "localeFromDocumentLang" in CONTENT
    assert "refreshLocaleContext" in CONTENT
    assert "locale_source" in CONTENT
    assert "document_lang" in CONTENT


def test_locale_from_document_lang_is_fail_closed() -> None:
    assert locale_from_document_lang("ru") == "ru-RU"
    assert locale_from_document_lang("ru-RU") == "ru-RU"
    assert locale_from_document_lang("ru-ru") == "ru-RU"
    assert locale_from_document_lang("en") == "en-US"
    assert locale_from_document_lang("en-US") == "en-US"
    assert locale_from_document_lang("en-us") == "en-US"
    assert locale_from_document_lang("en-GB") == "unknown"
    assert locale_from_document_lang("zh-CN") == "unknown"
    assert locale_from_document_lang("") == "unknown"
    assert locale_from_document_lang(None) == "unknown"
    assert locale_from_pathname("/futures/TAO_USDT") == "unknown"


def test_path_locale_wins_and_records_disagreement() -> None:
    ctx = resolve_locale_context(
        page_path="/ru-RU/futures/TAO_USDT",
        document_lang="en",
    )
    assert ctx.parser_locale == "ru-RU"
    assert ctx.locale_source == "path"
    assert ctx.document_lang == "en"
    assert ctx.locale_path_document_disagree is True
    ctx_ok = resolve_locale_context(
        page_path="/en-US/futures/TAO_USDT",
        document_lang="en",
    )
    assert ctx_ok.locale_path_document_disagree is False
    assert ctx_ok.locale_source == "path"


def test_several_unchanged_interval_ticks_still_commit() -> None:
    html = _html("tao_live_wrappers.html")
    snaps = []
    previous = None
    for index in range(4):
        snap = _tick(
            html,
            sequence=index + 1,
            offset_ms=index * INTERVAL_MS,
            trigger="interval",
            page_path="/futures/TAO_USDT",
            previous=previous,
        )
        snaps.append(snap)
        previous = snap
    assert len(snaps) == 4
    lasts = [snap.fields["last"].value for snap in snaps]
    assert lasts == [226.15, 226.15, 226.15, 226.15]
    ages = [snap.fields["last"].age_ms for snap in snaps]
    assert ages[0] == 0
    assert ages[1] == INTERVAL_MS
    assert ages[2] == 2 * INTERVAL_MS
    assert ages[3] == 3 * INTERVAL_MS
    assert all(snap.trigger == "interval" for snap in snaps)
    assert all(snap.sample_interval_ms == INTERVAL_MS for snap in snaps)
    assert snaps[3].changed_fields == ()
    assert snaps[0].locale_source == "document_lang"
    assert snaps[0].parser_locale == "en-US"
    assert snaps[0].document_lang == "en"


def test_mutation_snapshots_interleave_with_heartbeat() -> None:
    html = _html("tao_live_wrappers.html")
    mutated = html.replace(">226.15<", ">226.19<")
    first = _tick(
        html,
        sequence=1,
        offset_ms=0,
        trigger="interval",
        page_path="/futures/TAO_USDT",
    )
    second = _tick(
        html,
        sequence=2,
        offset_ms=INTERVAL_MS,
        trigger="interval",
        page_path="/futures/TAO_USDT",
        previous=first,
    )
    mutation = _tick(
        mutated,
        sequence=3,
        offset_ms=INTERVAL_MS + 40,
        trigger="mutation",
        page_path="/futures/TAO_USDT",
        previous=second,
    )
    after = _tick(
        mutated,
        sequence=4,
        offset_ms=2 * INTERVAL_MS,
        trigger="interval",
        page_path="/futures/TAO_USDT",
        previous=mutation,
    )
    assert [snap.trigger for snap in (first, second, mutation, after)] == [
        "interval",
        "interval",
        "mutation",
        "interval",
    ]
    assert first.fields["last"].value == pytest.approx(226.15)
    assert second.fields["last"].value == pytest.approx(226.15)
    assert mutation.fields["last"].value == pytest.approx(226.19)
    assert after.fields["last"].value == pytest.approx(226.19)
    assert "last" in mutation.changed_fields
    assert after.fields["last"].age_ms == pytest.approx(INTERVAL_MS - 40)


def test_missing_value_is_not_copied_from_earlier_snapshot() -> None:
    html = _html("tao_live_wrappers.html")
    missing_last = html.replace(">226.15<", ">--<")
    first = _tick(
        html,
        sequence=1,
        offset_ms=0,
        trigger="interval",
        page_path="/futures/TAO_USDT",
    )
    second = _tick(
        missing_last,
        sequence=2,
        offset_ms=INTERVAL_MS,
        trigger="interval",
        page_path="/futures/TAO_USDT",
        previous=first,
    )
    assert first.fields["last"].value == pytest.approx(226.15)
    assert second.fields["last"].value is None
    assert second.fields["last"].parse_status in {"missing", "unparsable"}
    assert second.fields["last"].age_ms is None


def test_localized_ru_ru_path() -> None:
    snap = _tick(
        _html("tao_ru_locale_header.html"),
        sequence=1,
        offset_ms=0,
        trigger="interval",
        page_path="/ru-RU/futures/TAO_USDT",
        document_lang="en",
    )
    assert snap.parser_locale == "ru-RU"
    assert snap.locale_source == "path"
    assert snap.document_lang == "en"
    assert snap.locale_path_document_disagree is True
    assert snap.fields["last"].value == pytest.approx(218.11)
    assert snap.page_path == "/ru-RU/futures/TAO_USDT"


def test_localized_en_us_path() -> None:
    snap = _tick(
        _html("tao_live_wrappers.html"),
        sequence=1,
        offset_ms=0,
        trigger="interval",
        page_path="/en-US/futures/TAO_USDT",
        document_lang="ru",
    )
    assert snap.parser_locale == "en-US"
    assert snap.locale_source == "path"
    assert snap.locale_path_document_disagree is True
    assert snap.fields["last"].value == pytest.approx(226.15)


def test_bare_path_html_lang_en() -> None:
    snap = _tick(
        _html("tao_live_wrappers.html"),
        sequence=1,
        offset_ms=0,
        trigger="interval",
        page_path="/futures/TAO_USDT",
    )
    assert snap.parser_locale == "en-US"
    assert snap.locale_source == "document_lang"
    assert snap.document_lang == "en"
    assert snap.fields["last"].value == pytest.approx(226.15)


def test_bare_path_html_lang_ru() -> None:
    snap = _tick(
        _html("tao_ru_locale_header.html"),
        sequence=1,
        offset_ms=0,
        trigger="interval",
        page_path="/futures/TAO_USDT",
    )
    assert snap.parser_locale == "ru-RU"
    assert snap.locale_source == "document_lang"
    assert snap.document_lang == "ru"
    assert snap.fields["last"].value == pytest.approx(218.11)
    assert "," in (snap.fields["last"].raw_text or "")


def test_missing_or_unsupported_lang_is_unknown_fail_closed() -> None:
    html = _html("tao_ru_locale_header.html")
    missing = _tick(
        html,
        sequence=1,
        offset_ms=0,
        trigger="interval",
        page_path="/futures/TAO_USDT",
        document_lang="",
    )
    unsupported = _tick(
        html,
        sequence=1,
        offset_ms=0,
        trigger="interval",
        page_path="/futures/TAO_USDT",
        document_lang="zh-CN",
    )
    for snap in (missing, unsupported):
        assert snap.parser_locale == "unknown"
        assert snap.locale_source == "unknown"
        assert snap.fields["last"].parse_status == "unparsable"
        assert snap.fields["bid"].parse_status == "missing"


def test_spa_locale_and_path_change_is_recomputed() -> None:
    html = _html("tao_ru_locale_header.html")
    first = _tick(
        html,
        sequence=1,
        offset_ms=0,
        trigger="interval",
        page_path="/futures/TAO_USDT",
        document_lang="en",
    )
    after_lang = _tick(
        html,
        sequence=2,
        offset_ms=INTERVAL_MS,
        trigger="interval",
        page_path="/futures/TAO_USDT",
        previous=first,
        document_lang="ru",
    )
    after_path = _tick(
        html,
        sequence=3,
        offset_ms=2 * INTERVAL_MS,
        trigger="interval",
        page_path="/ru-RU/futures/TAO_USDT",
        previous=after_lang,
        document_lang="en",
    )
    assert first.parser_locale == "en-US"
    assert first.locale_source == "document_lang"
    assert first.fields["last"].parse_status == "unparsable"
    assert after_lang.parser_locale == "ru-RU"
    assert after_lang.locale_source == "document_lang"
    assert after_lang.fields["last"].value == pytest.approx(218.11)
    assert after_path.parser_locale == "ru-RU"
    assert after_path.locale_source == "path"
    assert after_path.locale_path_document_disagree is True
    assert after_path.page_path == "/ru-RU/futures/TAO_USDT"


def test_session_metadata_preserves_configured_interval() -> None:
    html = _html("tao_live_wrappers.html")
    store = DurableCaptureStore(chunk_size=50)
    started = store.start_session(
        started_at=_stamp(0),
        interval_ms=INTERVAL_MS,
        page_host="www.mexc.com",
        page_path="/futures/TAO_USDT",
        session_id="cadence-session",
    )
    assert started.interval_ms == INTERVAL_MS
    previous = None
    for index in range(3):
        snap = _tick(
            html,
            sequence=index + 1,
            offset_ms=index * INTERVAL_MS,
            trigger="interval",
            page_path="/futures/TAO_USDT",
            previous=previous,
            capture_id=started.session_id,
        )
        store.append_snapshot(snap.as_dict())
        previous = snap
    store.stop_session(ended_at=_stamp(3 * INTERVAL_MS))
    exported = store.export_all_ndjson()
    compact = exported.replace(" ", "")
    assert f'"interval_ms":{INTERVAL_MS}' in compact
    assert compact.count('"trigger":"interval"') == 3
    assert '"sample_interval_ms":500' in compact
