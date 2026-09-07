"""Score a 5–15 min capture against mom/gap protocol v2.0.0 data admissibility.

Compares the sample to PROTOCOL_VERSION 2.0.0, not v1.0.0. The v1 scientific
rules stay frozen and uninspected here: this module does not import hypothesis
replay, does not execute any of the 21 cells, and does not compute mom/gap or
PnL.

The 8.0 usable-hour corpus bar is a long-identification requirement. It is
reported as not applied on this short gate. Timing bars are the unchanged
v1/v2 values (p95 ≤1000 ms, ≥99% ≤2000 ms) and are not relaxed.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from trading_bot.research.mexc_shadow.ui_capture.catalog import (
    CATALOG_VERSION,
    DEFAULT_SAMPLE_INTERVAL_MS,
    SCHEMA_NAME,
    SCHEMA_VERSION,
)
from trading_bot.research.mexc_shadow.ui_capture.durable import DEFAULT_CHUNK_SIZE
from trading_bot.research.mexc_shadow.ui_capture.long_observation import (
    scan_long_capture,
)
from trading_bot.research.mexc_shadow.ui_capture.normalize import (
    observation_from_snapshot,
    snapshot_from_mapping,
)
from trading_bot.research.mexc_shadow.ui_capture.parse import (
    join_price_tokens,
    locale_from_document_lang,
    locale_from_pathname,
    parse_price,
)
from trading_bot.research.mexc_shadow.ui_capture.quality import summarize_capture
from trading_bot.research.mexc_shadow.ui_capture.schema import UiRawSnapshot
from trading_bot.research.mexc_shadow.ui_capture.store import iter_all_mappings

SHORT_MIN_MS = 5 * 60 * 1000
SHORT_MAX_MS = 15 * 60 * 1000
TAO_LAST_MIN = 50.0
TAO_LAST_MAX = 800.0
REQUIRED_CATALOG = CATALOG_VERSION
REQUIRED_INTERVAL_MS = DEFAULT_SAMPLE_INTERVAL_MS
FROZEN_SIMULTANEOUS_MIN = 0.95
FROZEN_INTERARRIVAL_LE_2000_MIN = 0.99
FROZEN_P95_MAX_MS = 1000.0
STRATEGY_FIELDS = ("bid", "ask", "last", "mark", "index")
V133_SNAPSHOT_KEYS = ("locale_source", "document_lang", "parser_locale")
SUPPORTED_LOCALES = frozenset({"ru-RU", "en-US"})
PROTOCOL_VERSION = "2.0.0"
PROTOCOL_V1_VERSION = "1.0.0"
# Merge of the v2 data-contract amendment onto main (PR #48).
PROTOCOL_V2_MERGE_COMMIT = "0b4f761bb4c7fa8a6e4520901bfc32f8f2bf59d1"
PROTOCOL_V2_AMENDMENT_COMMIT = "cb679745b1fda6ecde509d8f2656232a89dd192b"
EXTENSION_EXPECTED = "1.3.3"

# Operator 1.3.3 / catalog v1.2 export scored by this milestone. Not rewritten.
SCORED_CAPTURE_SHA256 = (
    "1f83d307cc42324f803b26c01f6a6afde5eb1dc65b938909cf50eaeb09b56f2b"
)

PathKind = Literal["bare", "localized", "other"]


def _report_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _percentile(sorted_vals: list[float], fraction: float) -> float | None:
    """Match quality.py rank estimator used by the frozen p95 bar."""

    if not sorted_vals:
        return None
    index = min(len(sorted_vals) - 1, int(len(sorted_vals) * fraction))
    return sorted_vals[index]


def _close_missing_burst(
    burst: dict[str, Any] | None, *, end_seq: int, end_t: str | None, end_ms: float | None
) -> dict[str, Any] | None:
    if burst is None:
        return None
    start_ms = burst.get("start_t_ms")
    duration_ms = None
    if start_ms is not None and end_ms is not None:
        duration_ms = max(0.0, float(end_ms) - float(start_ms))
    return {
        **burst,
        "end_seq": end_seq,
        "end_t": end_t,
        "duration_ms": duration_ms,
    }


def _futures_path_kind(path: str) -> PathKind:
    """Classify /futures/ vs /xx-XX/futures/ without inferring locale from digits."""

    parts = [item.split("?")[0] for item in path.split("/") if item]
    if not parts:
        return "other"
    if parts[0] == "futures":
        return "bare"
    if (
        len(parts) >= 2
        and parts[1] == "futures"
        and len(parts[0]) == 5
        and parts[0][2] == "-"
        and parts[0][:2].isalpha()
        and parts[0][3:].isalpha()
    ):
        return "localized"
    return "other"


def _five_key(snap: UiRawSnapshot) -> tuple[float | str | None, ...]:
    values: list[float | str | None] = []
    for name in STRATEGY_FIELDS:
        field = snap.fields.get(name)
        values.append(None if field is None else field.value)
    return tuple(values)


def _values_close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=0.0, abs_tol=1e-9)


def _locale_route(snap: UiRawSnapshot) -> str | None:
    """Return v2 §5 item 9 route name, or None if the row is inadmissible."""

    kind = _futures_path_kind(snap.page_path or "")
    parser = str(snap.parser_locale or "unknown")
    source = str(snap.locale_source or "unknown")
    if snap.parser_mode and str(snap.parser_mode) != parser:
        return None
    if snap.ui_locale and str(snap.ui_locale) != parser:
        return None
    if kind == "localized":
        path_locale = locale_from_pathname(snap.page_path)
        if (
            source == "path"
            and path_locale in SUPPORTED_LOCALES
            and parser == path_locale
        ):
            return "path"
        return None
    if kind == "bare":
        doc_locale = locale_from_document_lang(snap.document_lang)
        observed = (snap.document_lang or "").strip()
        if (
            source == "document_lang"
            and observed
            and doc_locale in SUPPORTED_LOCALES
            and parser == doc_locale
        ):
            return "document_lang"
        return None
    return None


def _scale_audit_ok(snap: UiRawSnapshot) -> tuple[bool, str | None]:
    """Re-parse retained raw_text/tokens under the stamped locale. No rescaling."""

    locale = str(snap.parser_locale or "unknown")
    for name in STRATEGY_FIELDS:
        field = snap.fields.get(name)
        if field is None or field.parse_status not in {"ok", "ok_redundant"}:
            continue
        stored = field.value
        if not isinstance(stored, int | float) or isinstance(stored, bool):
            return False, f"{name}:stored_not_numeric"
        raw = field.raw_text
        reparsed = parse_price(raw, locale)
        if reparsed is None or not _values_close(float(stored), reparsed):
            return False, f"{name}:raw_text_mismatch"
        tokens = field.raw_tokens
        if tokens:
            joined = join_price_tokens(list(tokens))
            token_parsed = parse_price(joined, locale)
            if token_parsed is None or not _values_close(float(stored), token_parsed):
                return False, f"{name}:raw_tokens_mismatch"
    return True, None


def score_v2_contract_gate(path: Path) -> dict[str, Any]:
    """Score one 5–15 min capture against protocol v2.0.0 data-admissibility bars."""

    quality = summarize_capture(path)
    scan = scan_long_capture(path)
    n_considered = 0
    n_strategy_ready = 0
    n_unknown_on_ready = 0
    n_missing_v133_keys = 0
    n_schema_mismatch = 0
    n_wrong_interval = 0
    n_locale_route_fail = 0
    n_disagree = 0
    n_scale_fail = 0
    scale_fail_reason: str | None = None
    lasts: list[float] = []
    bids: list[float] = []
    asks: list[float] = []
    marks: list[float] = []
    indexes: list[float] = []
    locales: Counter[str] = Counter()
    locale_sources: Counter[str] = Counter()
    document_langs: Counter[str] = Counter()
    field_parser_locales: Counter[str] = Counter()
    locale_routes: Counter[str] = Counter()
    paths: Counter[str] = Counter()
    catalogs: Counter[str] = Counter()
    schema_versions: Counter[int] = Counter()
    intervals: Counter[int] = Counter()
    alias_counts: Counter[int] = Counter()
    header_item_counts: Counter[int] = Counter()
    title_hits: Counter[tuple[int, int, int]] = Counter()
    mark_selectors: Counter[str] = Counter()
    index_selectors: Counter[str] = Counter()
    raw_has_comma = 0
    raw_has_dot = 0
    scale_outliers = 0
    open_missing: dict[str, Any] | None = None
    missing_bursts: list[dict[str, Any]] = []
    first_symbol: str | None = None
    min_alias: int | None = None
    n_interval = 0
    n_unchanged_interval = 0
    previous_key: tuple[float | str | None, ...] | None = None
    arrivals: list[float] = []
    sequences: list[int] = []

    for payload in iter_all_mappings(path):
        if payload.get("schema") != SCHEMA_NAME:
            continue
        n_considered += 1
        if int(payload.get("schema_version") or 0) != SCHEMA_VERSION:
            n_schema_mismatch += 1
        if any(key not in payload for key in V133_SNAPSHOT_KEYS):
            n_missing_v133_keys += 1
        snap = snapshot_from_mapping(payload)
        sequences.append(int(snap.sequence))
        locale = str(snap.parser_locale or snap.ui_locale or snap.parser_mode or "unknown")
        locales[locale] += 1
        locale_sources[str(snap.locale_source or "unknown")] += 1
        document_langs[str(snap.document_lang or "")] += 1
        paths[str(snap.page_path or "")] += 1
        catalogs[str(snap.selector_catalog_version or "")] += 1
        schema_versions[int(snap.schema_version)] += 1
        interval = snap.sample_interval_ms
        if interval is None:
            n_wrong_interval += 1
        else:
            intervals[int(interval)] += 1
            if int(interval) != REQUIRED_INTERVAL_MS:
                n_wrong_interval += 1
        if snap.locale_path_document_disagree:
            n_disagree += 1
        route = _locale_route(snap)
        if route is None:
            n_locale_route_fail += 1
            locale_routes["inadmissible"] += 1
        else:
            locale_routes[route] += 1
        scale_ok_row, scale_reason = _scale_audit_ok(snap)
        if not scale_ok_row:
            n_scale_fail += 1
            if scale_fail_reason is None:
                scale_fail_reason = scale_reason
        alias = int((snap.header_diagnostics or {}).get("header_alias_count") or 0)
        alias_counts[alias] += 1
        min_alias = alias if min_alias is None else min(min_alias, alias)
        diag = snap.header_diagnostics or {}
        header_item_counts[int(diag.get("header_item_count") or 0)] += 1
        title_hits[
            (
                int(diag.get("header_title_hits_mark") or 0),
                int(diag.get("header_title_hits_index") or 0),
                int(diag.get("header_title_hits_funding") or 0),
            )
        ] += 1
        rec = observation_from_snapshot(snap)
        obs = rec.observation
        missing_names: list[str] = []
        for name in STRATEGY_FIELDS:
            field = snap.fields.get(name)
            raw = None if field is None else field.raw_text
            if field is not None and field.parser_locale:
                field_parser_locales[str(field.parser_locale)] += 1
            if raw and "," in raw:
                raw_has_comma += 1
            if raw and "." in raw:
                raw_has_dot += 1
            value = None if field is None else field.value
            ok = (
                field is not None
                and field.parse_status in {"ok", "ok_redundant"}
                and isinstance(value, int | float)
                and not isinstance(value, bool)
                and float(value) > 0
            )
            if not ok:
                missing_names.append(name)
            elif (
                name == "last"
                and isinstance(value, int | float)
                and not isinstance(value, bool)
                and not (TAO_LAST_MIN <= float(value) <= TAO_LAST_MAX)
            ):
                scale_outliers += 1
        stamp = snap.received_at_local
        try:
            stamp_ms = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000.0
        except (TypeError, ValueError, AttributeError):
            stamp_ms = None
        if stamp_ms is not None:
            arrivals.append(stamp_ms)
        if missing_names:
            key = tuple(missing_names)
            if open_missing is not None and tuple(open_missing["fields"]) == key:
                open_missing["n"] += 1
            else:
                closed = _close_missing_burst(
                    open_missing,
                    end_seq=int(snap.sequence),
                    end_t=stamp,
                    end_ms=stamp_ms,
                )
                if closed is not None:
                    missing_bursts.append(closed)
                open_missing = {
                    "fields": list(key),
                    "n": 1,
                    "start_seq": int(snap.sequence),
                    "start_t": stamp,
                    "start_t_ms": stamp_ms,
                }
        elif open_missing is not None:
            closed = _close_missing_burst(
                open_missing,
                end_seq=int(snap.sequence),
                end_t=stamp,
                end_ms=stamp_ms,
            )
            if closed is not None:
                missing_bursts.append(closed)
            open_missing = None
        five = _five_key(snap)
        if snap.trigger == "interval":
            n_interval += 1
            if previous_key is not None and five == previous_key:
                n_unchanged_interval += 1
        previous_key = five
        if obs is None:
            continue
        if first_symbol is None:
            first_symbol = obs.symbol
        if obs.last is not None:
            lasts.append(obs.last)
        bids.append(obs.bid)
        asks.append(obs.ask)
        if obs.mark is not None:
            marks.append(obs.mark)
        if obs.index is not None:
            indexes.append(obs.index)
        mark_rec = snap.fields.get("mark")
        index_rec = snap.fields.get("index")
        if mark_rec and mark_rec.selector_id:
            mark_selectors[str(mark_rec.selector_id)] += 1
        if index_rec and index_rec.selector_id:
            index_selectors[str(index_rec.selector_id)] += 1
        ready = all((obs.symbol, obs.bid, obs.ask, obs.last, obs.mark, obs.index))
        if ready:
            n_strategy_ready += 1
            if (
                str(snap.parser_locale or "unknown") == "unknown"
                or str(snap.locale_source or "unknown") == "unknown"
            ):
                n_unknown_on_ready += 1

    if open_missing is not None:
        closed = _close_missing_burst(
            open_missing,
            end_seq=int(open_missing.get("start_seq") or 0),
            end_t=str(open_missing.get("start_t")),
            end_ms=open_missing.get("start_t_ms"),
        )
        if closed is not None:
            missing_bursts.append(closed)

    duration_ms = quality.duration_ms
    duration_ok = (
        duration_ms is not None and SHORT_MIN_MS <= float(duration_ms) <= SHORT_MAX_MS
    )
    median_last = _median(lasts)
    scale_ok = (
        median_last is not None
        and TAO_LAST_MIN <= median_last <= TAO_LAST_MAX
        and scale_outliers == 0
        and n_scale_fail == 0
    )
    catalog_ok = catalogs.get(REQUIRED_CATALOG, 0) == n_considered and n_considered > 0
    schema_ok = (
        n_considered > 0
        and n_schema_mismatch == 0
        and schema_versions.get(SCHEMA_VERSION, 0) == n_considered
    )
    extension_fields_ok = n_considered > 0 and n_missing_v133_keys == 0
    # quality.sessions keeps start/end payloads; scan.sessions is the compact view.
    quality_sessions = list(quality.sessions or [])
    scan_sessions = list(scan.get("sessions") or [])
    session_intervals: list[int] = []
    session_chunk_mismatch = 0
    for row in quality_sessions:
        start = row.get("start") or {}
        end = row.get("end") or {}
        for raw_interval in (start.get("interval_ms"), end.get("interval_ms")):
            if raw_interval is None:
                continue
            session_intervals.append(int(raw_interval))
        n_session_snaps = end.get("n_snapshots")
        n_chunks = end.get("n_chunks")
        chunk_size = int(start.get("chunk_size") or DEFAULT_CHUNK_SIZE)
        if n_session_snaps is not None and n_chunks is not None:
            expected_chunks = (
                math.ceil(int(n_session_snaps) / chunk_size) if int(n_session_snaps) else 0
            )
            if int(n_chunks) != expected_chunks:
                session_chunk_mismatch += 1
            first_seq = end.get("first_sequence")
            last_seq = end.get("last_sequence")
            if first_seq is not None and int(first_seq) != 1:
                session_chunk_mismatch += 1
            if last_seq is not None and int(last_seq) != int(n_session_snaps):
                session_chunk_mismatch += 1
    interval_ok = (
        n_considered > 0
        and n_wrong_interval == 0
        and intervals.get(REQUIRED_INTERVAL_MS, 0) == n_considered
        and all(value == REQUIRED_INTERVAL_MS for value in session_intervals)
    )
    symbol_ok = first_symbol == "TAOUSDT"
    crossed = int(quality.n_bid_ge_ask or 0)
    ambiguity = 0
    diag = (scan.get("selector_diagnostics") or {}).get("ambiguity_reason") or {}
    for reason, count in diag.items():
        if reason not in {None, "", "none", "None"}:
            ambiguity += int(count)
    for reason, count in (quality.invalid_reasons or {}).items():
        if "ambiguous" in str(reason):
            ambiguity += int(count)
    data_invalid = int(scan.get("n_data_invalid") or 0)
    warmup_n = int(scan.get("n_startup_warmup") or 0)
    storage_errors = list(scan.get("storage_errors") or [])
    for row in scan_sessions:
        if row.get("storage_error"):
            storage_errors.append(str(row["storage_error"]))
        if str(row.get("status") or "") == "failed":
            storage_errors.append(f"session {row.get('session_id')} status=failed")
    storage_errors = list(dict.fromkeys(storage_errors))
    seq_ok = (
        not quality.sequence_diagnostics
        and not any(
            row.get("sequence_gaps") or row.get("client_sequence_mismatches")
            for row in scan_sessions
        )
        and sequences == list(range(1, n_considered + 1))
        and session_chunk_mismatch == 0
    )
    mark_struct = sum(
        count for key, count in mark_selectors.items() if key.startswith("header_struct:mark")
    )
    index_struct = sum(
        count for key, count in index_selectors.items() if key.startswith("header_struct:index")
    )
    mark_on_index = sum(count for key, count in mark_selectors.items() if "index" in key)
    index_on_mark = sum(
        count for key, count in index_selectors.items() if key.startswith("header_struct:mark")
    )
    swapped = mark_on_index > 0 or index_on_mark > 0
    simultaneous = int(quality.n_simultaneous_bid_ask_last_mark_index or 0)
    simultaneous_rate = simultaneous / n_considered if n_considered else 0.0
    strategy_ready_rate = n_strategy_ready / n_considered if n_considered else 0.0
    deltas = [arrivals[i] - arrivals[i - 1] for i in range(1, len(arrivals))]
    ordered = sorted(deltas)
    le_2000 = sum(1 for delta in deltas if delta <= 2000.0)
    n_gt_2000 = len(deltas) - le_2000
    n_gt_1000 = sum(1 for delta in deltas if delta > 1000.0)
    frac_le_2000 = le_2000 / len(deltas) if deltas else 0.0
    inter = {
        "n": len(ordered),
        "p50_ms": _percentile(ordered, 0.50),
        "p90_ms": _percentile(ordered, 0.90),
        "p95_ms": _percentile(ordered, 0.95),
        "p99_ms": _percentile(ordered, 0.99),
        "min_ms": ordered[0] if ordered else None,
        "max_ms": ordered[-1] if ordered else None,
    }
    p95 = inter.get("p95_ms")
    # Split v2 §5 item 8 so a p95 miss is not collapsed into the 99% bar.
    p95_ok = p95 is not None and float(p95) <= FROZEN_P95_MAX_MS
    frac_ok = bool(deltas) and frac_le_2000 >= FROZEN_INTERARRIVAL_LE_2000_MIN
    # v2 §5 item 9: localized path or bare /futures/ plus document_lang. Not v1 ru-RU-only.
    locale_ok = n_considered > 0 and n_locale_route_fail == 0
    # Unchanged five-field key on an interval tick proves 1.3.3 did not skip heartbeats.
    heartbeat_ok = n_interval > 0 and n_unchanged_interval > 0
    warmup_separated = "n_startup_warmup" in scan and "n_data_invalid" in scan
    gates = {
        "duration_5_to_15_min": duration_ok,
        "extension_1_3_3_locale_fields": extension_fields_ok,
        "schema_mexc_ui_raw_snapshot_v1": schema_ok,
        "catalog_v1_2": catalog_ok,
        "configured_interval_500_ms": interval_ok,
        "heartbeat_unchanged_interval_commits": heartbeat_ok,
        "sequence_chunk_continuity": seq_ok,
        "no_storage_errors": not storage_errors,
        "locale_v2_provenance": locale_ok,
        "no_unknown_locale_on_strategy_ready": n_unknown_on_ready == 0,
        "symbol_taousdt": symbol_ok,
        "raw_text_tokens_match_locale_scale": n_scale_fail == 0 and n_considered > 0,
        "absolute_price_scale": (
            median_last is not None
            and TAO_LAST_MIN <= median_last <= TAO_LAST_MAX
            and scale_outliers == 0
        ),
        "bid_lt_ask": crossed == 0 and n_considered > 0,
        "mark_index_not_swapped": mark_struct > 0 and index_struct > 0 and not swapped,
        "simultaneous_coverage_ge_95pct": simultaneous_rate >= FROZEN_SIMULTANEOUS_MIN,
        "startup_warmup_separated_from_data_invalid": warmup_separated,
        "no_post_readiness_data_invalid": data_invalid == 0,
        "no_selector_ambiguity_burst": ambiguity == 0,
        "export_replay_deterministic": quality.replay_determinism_sha256 is not None,
        "frozen_interarrival_p95": p95_ok,
        "frozen_interarrival_le_2000": frac_ok,
    }
    failure_classes = [name for name, ok in gates.items() if not ok]
    passed = not failure_classes
    return {
        "protocol_version_scored": PROTOCOL_VERSION,
        "protocol_version_not_scored": PROTOCOL_V1_VERSION,
        "path": _report_path(path),
        "sha256": sha256_file(path),
        "n_snapshots": n_considered,
        "duration_ms": duration_ms,
        "duration_minutes": (
            None if duration_ms is None else round(float(duration_ms) / 60_000.0, 4)
        ),
        "page_paths": dict(paths),
        "locales": dict(locales),
        "locale_sources": dict(locale_sources),
        "document_langs": dict(document_langs),
        "locale_routes": dict(locale_routes),
        "n_locale_path_document_disagree": n_disagree,
        "n_locale_route_fail": n_locale_route_fail,
        "field_parser_locales": dict(field_parser_locales),
        "catalog_versions": dict(catalogs),
        "schema_versions": {str(key): val for key, val in sorted(schema_versions.items())},
        "sample_interval_ms": {str(key): val for key, val in sorted(intervals.items())},
        "session_interval_ms": session_intervals,
        "header_alias_counts": {str(key): val for key, val in sorted(alias_counts.items())},
        "min_header_alias_count": min_alias,
        "header_item_counts": {str(key): val for key, val in sorted(header_item_counts.items())},
        "header_title_hits_mark_index_funding": {
            f"{a},{b},{c}": n for (a, b, c), n in sorted(title_hits.items())
        },
        "median_last": median_last,
        "median_bid": _median(bids),
        "median_ask": _median(asks),
        "median_mark": _median(marks),
        "median_index": _median(indexes),
        "n_strategy_ready": n_strategy_ready,
        "strategy_ready_rate": strategy_ready_rate,
        "strategy_ready_pct": round(strategy_ready_rate * 100.0, 4),
        "n_unknown_locale_on_strategy_ready": n_unknown_on_ready,
        "simultaneous_bid_ask_last_mark_index": simultaneous,
        "simultaneous_rate": simultaneous_rate,
        "simultaneous_pct": round(simultaneous_rate * 100.0, 4),
        "missing_field_bursts": missing_bursts,
        "n_missing_field_bursts": len(missing_bursts),
        "n_data_invalid": data_invalid,
        "n_startup_warmup": warmup_n,
        "n_ready_valid": scan.get("n_ready_valid"),
        "class_counts": scan.get("class_counts"),
        "warmup_and_invalid_bursts": scan.get("warmup_and_invalid_bursts") or [],
        "n_bid_ge_ask": crossed,
        "raw_text_has_comma": raw_has_comma,
        "raw_text_has_dot": raw_has_dot,
        "n_scale_audit_fail": n_scale_fail,
        "scale_audit_first_reason": scale_fail_reason,
        "numeric_scale_audit_ok": scale_ok,
        "selector_diagnostics": scan.get("selector_diagnostics"),
        "first_ready": scan.get("first_ready"),
        "mark_selectors": dict(mark_selectors),
        "index_selectors": dict(index_selectors),
        "replay_canonical_sha256": quality.replay_determinism_sha256,
        "storage_errors": storage_errors,
        "sessions": scan_sessions,
        "sequence_diagnostics": quality.sequence_diagnostics,
        "trigger_counts": dict(quality.trigger_counts or {}),
        "n_interval_triggers": n_interval,
        "n_unchanged_interval_commits": n_unchanged_interval,
        "n_missing_v133_keys": n_missing_v133_keys,
        "interarrival_ms": inter,
        "quality_interarrival_ms": quality.interarrival_ms,
        "interarrival_frac_le_2000_ms": frac_le_2000,
        "interarrival_n_gt_2000_ms": n_gt_2000,
        "interarrival_n_gt_1000_ms": n_gt_1000,
        "timing_adequacy": quality.timing_adequacy,
        "n_sessions": quality.n_sessions,
        "n_chunks_total": quality.n_chunks_total,
        "quality_invalid_reasons": quality.invalid_reasons,
        "hours_requirement": {
            "protocol_v2_item": 6,
            "required_usable_hours": 8.0,
            "applied": False,
            "reason": "short_gate_5_15_min_not_identification_corpus",
        },
        "v1_vs_v2": {
            "catalog_v1": "v1.1",
            "catalog_v2": REQUIRED_CATALOG,
            "heartbeat_v1": "not required",
            "heartbeat_v2": "interval ticks persist when values are unchanged",
            "locale_v1": "path-implied; catalog v1.1",
            "locale_v2": (
                "localized path with locale_source=path, or bare /futures/ "
                "with locale_source=document_lang"
            ),
            "timing_bars": "unchanged p95<=1000ms and >=99% <=2000ms",
        },
        "gates": gates,
        "failure_classes": failure_classes,
        "passed": passed,
    }


def screenshot_comparisons_for(digest: str) -> list[dict[str, Any]]:
    """Operator screenshots for the scored 1.3.3 export only."""

    if digest != SCORED_CAPTURE_SHA256:
        return []
    return [
        {
            "local_time": "2026-09-07T17:54:14+04:00",
            "utc": "2026-09-07T13:54:14Z",
            "visible": {
                "last": "264.50",
                "index": "264.55",
                "fair": "264.55",
                "bid": "264.51",
                "ask": "264.55",
                "ui_labels": "English",
                "decimal": "point",
                "logged_in": True,
                "symbol": "TAOUSDT",
            },
            "nearest_snapshot": "2026-09-07T13:54:14.020Z",
            "captured": {
                "last": 264.62,
                "bid": 264.57,
                "ask": 264.60,
                "mark": 264.57,
                "index": 264.56,
            },
            "scale_match": True,
            "bid_lt_ask": True,
            "notes": (
                "Absolute prices ~264. Last within 12 cents of the screenshot. "
                "Fair/index within 2 cents of header_struct mark/index. Ordinary lag."
            ),
        },
        {
            "local_time": "2026-09-07T17:59:17+04:00",
            "utc": "2026-09-07T13:59:17Z",
            "visible": {
                "last": "264.56",
                "index": "264.49",
                "fair": "264.52",
                "bid": "264.53",
                "ask": "264.56",
                "ui_labels": "English",
                "decimal": "point",
                "logged_in": True,
                "symbol": "TAOUSDT",
            },
            "nearest_snapshot": "2026-09-07T13:59:16.981Z",
            "captured": {
                "last": 264.53,
                "bid": 264.52,
                "ask": 264.55,
                "mark": 264.57,
                "index": 264.57,
            },
            "scale_match": True,
            "bid_lt_ask": True,
            "notes": (
                "Last within 3 cents. Screenshot Fair 264.52 / Index 264.49 vs "
                "snapshot mark 264.57 / index 264.57: lag, not a mark/index swap "
                "(selectors remain header_struct:mark vs header_struct:index)."
            ),
        },
    ]


def build_milestone_payload(raw: Path) -> dict[str, Any]:
    short = score_v2_contract_gate(raw)
    passed = bool(short["passed"])
    if passed:
        status = "MEXC_UI_CAPTURE_V2_CONTRACT_FINAL_GATE_PASS"
    else:
        status = "MEXC_UI_CAPTURE_V2_CONTRACT_FINAL_GATE_FAIL"
    return {
        "milestone": "MEXC_UI_CAPTURE_V2_CONTRACT_FINAL_GATE",
        "status": status,
        "decision": "STOP_FOR_LEAD_REVIEW",
        "ml_status": "NOT_STARTED",
        "paper": False,
        "live": False,
        "strategy_tuning": False,
        "mom_gap_inspected": False,
        "n_cells_executed": 0,
        "long_capture_started": False,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "extension_expected": EXTENSION_EXPECTED,
        "extension_version_in_ndjson": None,
        "catalog_required": REQUIRED_CATALOG,
        "protocol": {
            "version_scored": PROTOCOL_VERSION,
            "version_not_scored": PROTOCOL_V1_VERSION,
            "amendment_doc": "docs/mexc_mom_gap_hypothesis_protocol_v2_data_contract_amendment.md",
            "v1_design_doc": "docs/mexc_mom_gap_hypothesis_protocol_design_v1.md",
            "v1_blob": "4e8d05940b2bc759acff881779159a7bcba1604e",
            "v2_merge_commit": PROTOCOL_V2_MERGE_COMMIT,
            "v2_amendment_commit": PROTOCOL_V2_AMENDMENT_COMMIT,
        },
        "frozen_protocol_v2": {
            "simultaneous_min": FROZEN_SIMULTANEOUS_MIN,
            "interarrival_le_2000_min": FROZEN_INTERARRIVAL_LE_2000_MIN,
            "p95_max_ms": FROZEN_P95_MAX_MS,
            "hours_requirement": "not applied to this 5-15 min gate",
            "catalog_v1": "v1.1",
            "catalog_v2": REQUIRED_CATALOG,
            "heartbeat_required": True,
            "locale_routes": ["path", "document_lang"],
        },
        "short_validation": short,
        "screenshots": screenshot_comparisons_for(str(short.get("sha256") or "")),
        "notes": [
            "Scored against protocol v2.0.0 data admissibility, not v1.0.0.",
            "Did not inspect mom/gap formulas or execute any of the 21 cells.",
            "Did not retune frozen profiles, thresholds, exits, or lookbacks.",
            "Did not start the 8-12h identification corpus.",
            "Extension version is not stamped in NDJSON; 1.3.3 is attested by "
            "locale_source/document_lang/parser_locale on every snapshot.",
        ],
    }


def render_markdown(payload: dict[str, Any]) -> str:
    short = payload["short_validation"]
    inter = short.get("interarrival_ms") or {}
    lines = [
        "# MEXC UI capture v2 contract final gate",
        "",
        f"STATUS: `{payload['status']}`",
        "",
        f"DECISION: `{payload['decision']}`",
        "",
        "ML_STATUS: `NOT_STARTED`",
        "",
        "PAPER: **false**",
        "",
        "LIVE: **false**",
        "",
        "STRATEGY_TUNING: **false**",
        "",
        "MOM/GAP: **not inspected** (0 of 21 cells executed)",
        "",
        "Long capture: **not started**",
        "",
        "## Purpose",
        "",
        "Score a 5–15 minute logged-in TAOUSDT capture from extension 1.3.3 /",
        "catalog v1.2 against the frozen mom/gap protocol **v2.0.0** data-admissibility",
        "contract. This is not a v1.0.0 score: v2 requires exact catalog v1.2,",
        "heartbeat interval ticks when values are unchanged, and explicit locale",
        "provenance (localized path **or** bare `/futures/` plus `document_lang`).",
        "The 8.0 usable-hour identification bar is not applied to this short gate.",
        "Timing bars are unchanged from v1 (p95 ≤1000 ms, ≥99% ≤2000 ms) and are",
        "not relaxed.",
        "",
        "## Protocol comparison (v1.0.0 vs v2.0.0 vs this sample)",
        "",
        "| Item | v1.0.0 | v2.0.0 | this sample |",
        "| --- | --- | --- | --- |",
        (
            f"| protocol version scored | {PROTOCOL_V1_VERSION} | "
            f"**{PROTOCOL_VERSION}** | **{PROTOCOL_VERSION}** |"
        ),
        "| catalog | v1.1 | exactly v1.2 | "
        f"`{short.get('catalog_versions')}` |",
        "| heartbeat | not required | interval ticks persist when unchanged | "
        f"interval={short.get('n_interval_triggers')}, unchanged="
        f"{short.get('n_unchanged_interval_commits')} |",
        "| locale | path-implied | path `locale_source=path` **or** bare `/futures/` "
        "`locale_source=document_lang` | "
        f"routes `{short.get('locale_routes')}` |",
        "| 8.0 usable hours | identification corpus | same | **not applied** |",
        "| p95 ≤1000 ms and ≥99% ≤2000 ms | required | unchanged | "
        f"p95={inter.get('p95_ms')} frac≤2000="
        f"{short.get('interarrival_frac_le_2000_ms')} |",
        "",
        "## Capture",
        "",
        f"- path: `{short['path']}`",
        f"- sha256: `{short['sha256']}`",
        f"- snapshots: {short['n_snapshots']}",
        f"- duration minutes: {short['duration_minutes']}",
        f"- page_paths: `{short['page_paths']}`",
        f"- parser_locale: `{short['locales']}`",
        f"- locale_source: `{short.get('locale_sources')}`",
        f"- document_lang: `{short.get('document_langs')}`",
        f"- locale routes: `{short.get('locale_routes')}`",
        f"- locale_path_document_disagree: {short.get('n_locale_path_document_disagree')}",
        f"- field parser_locale: `{short.get('field_parser_locales')}`",
        f"- catalog: `{short['catalog_versions']}`",
        f"- schema_version: `{short.get('schema_versions')}`",
        f"- sample_interval_ms: `{short.get('sample_interval_ms')}`",
        f"- session interval_ms: `{short.get('session_interval_ms')}`",
        f"- trigger_counts: `{short.get('trigger_counts')}`",
        (
            f"- heartbeat: interval={short.get('n_interval_triggers')} "
            f"unchanged_interval_commits={short.get('n_unchanged_interval_commits')}"
        ),
        f"- min header_alias_count: {short['min_header_alias_count']}",
        f"- header_item_count: `{short.get('header_item_counts')}`",
        (
            "- header title hits mark,index,funding: "
            f"`{short.get('header_title_hits_mark_index_funding')}`"
        ),
        (
            "- median last/bid/ask/mark/index: "
            f"{short['median_last']} / {short['median_bid']} / "
            f"{short['median_ask']} / {short['median_mark']} / "
            f"{short['median_index']}"
        ),
        (
            f"- simultaneous bid+ask+last+mark+index: "
            f"{short['simultaneous_bid_ask_last_mark_index']} "
            f"({short['simultaneous_pct']}%)"
        ),
        f"- strategy-ready: {short['n_strategy_ready']} ({short['strategy_ready_pct']}%)",
        f"- unknown locale on strategy-ready: {short.get('n_unknown_locale_on_strategy_ready')}",
        f"- DATA_INVALID: {short['n_data_invalid']}",
        f"- STARTUP_WARMUP: {short['n_startup_warmup']}",
        f"- missing-field bursts: {short['n_missing_field_bursts']}",
        (
            "- raw interarrival p50/p90/p95/p99: "
            f"{inter.get('p50_ms')} / {inter.get('p90_ms')} / "
            f"{inter.get('p95_ms')} / {inter.get('p99_ms')}"
        ),
        (
            "- frac≤2000ms / n>2000ms / n>1000ms / min / max: "
            f"{short.get('interarrival_frac_le_2000_ms')} / "
            f"{short.get('interarrival_n_gt_2000_ms')} / "
            f"{short.get('interarrival_n_gt_1000_ms')} / "
            f"{inter.get('min_ms')} / {inter.get('max_ms')}"
        ),
        f"- raw_text comma/dot counts: {short['raw_text_has_comma']} / {short['raw_text_has_dot']}",
        (
            "- scale audit fails: "
            f"{short.get('n_scale_audit_fail')} ({short.get('scale_audit_first_reason')})"
        ),
        f"- replay_canonical_sha256: `{short.get('replay_canonical_sha256')}`",
        f"- passed: **{short['passed']}**",
        "",
        "| Gate | Result |",
        "| --- | --- |",
    ]
    for name, ok in short["gates"].items():
        lines.append(f"| `{name}` | {'PASS' if ok else 'FAIL'} |")
    lines.extend(
        [
            "",
            "### Failure classes",
            "",
        ]
    )
    if short["failure_classes"]:
        for name in short["failure_classes"]:
            lines.append(f"- `{name}`")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "### Missing-field bursts",
            "",
        ]
    )
    bursts = short.get("missing_field_bursts") or []
    if not bursts:
        lines.append("None. Every snapshot had simultaneous bid, ask, last, mark, and index.")
    else:
        lines.append("| fields | n | seq | duration_ms | start_t |")
        lines.append("| --- | --- | --- | --- | --- |")
        for burst in bursts:
            fields = ",".join(burst.get("fields") or [])
            seq = f"{burst.get('start_seq')}–{burst.get('end_seq')}"
            lines.append(
                f"| `{fields}` | {burst.get('n')} | {seq} | "
                f"{burst.get('duration_ms')} | {burst.get('start_t')} |"
            )
    lines.extend(
        [
            "",
            "### Screenshots vs nearest snapshots",
            "",
        ]
    )
    shots = payload.get("screenshots") or []
    if not shots:
        lines.append("No screenshot table bound to this SHA-256.")
    else:
        for shot in shots:
            vis = shot["visible"]
            cap = shot["captured"]
            lines.append(
                f"- {shot['local_time']}: UI last `{vis['last']}` / Fair `{vis['fair']}` / "
                f"Index `{vis['index']}` vs snapshot `{shot['nearest_snapshot']}` "
                f"last {cap['last']} bid {cap['bid']} ask {cap['ask']} "
                f"mark {cap['mark']} index {cap['index']}. "
                f"{shot['notes']}"
            )
    gates = short["gates"]
    lines.extend(["", "## Findings", ""])
    if short["passed"]:
        lines.append(
            "All short-gate v2.0.0 data-admissibility bars passed. "
            "The 8–12h identification corpus is still not started."
        )
    else:
        lines.append(
            "This sample is **not** ADMISSIBLE under protocol v2.0.0 data admissibility."
        )
        if not gates.get("frozen_interarrival_p95") or not gates.get("frozen_interarrival_le_2000"):
            lines.append(
                "- v2 §5 item 8 frozen interarrival failed "
                f"(p95_ms={inter.get('p95_ms')}, "
                f"frac≤2000ms={short.get('interarrival_frac_le_2000_ms')}, "
                f"n>2000ms={short.get('interarrival_n_gt_2000_ms')}, "
                f"n>1000ms={short.get('interarrival_n_gt_1000_ms')}). "
                "These bars are unchanged from v1.0.0 and are not relaxed."
            )
        if not gates.get("locale_v2_provenance"):
            lines.append(
                f"- locale v2 provenance failed (routes `{short.get('locale_routes')}`, "
                f"n_route_fail={short.get('n_locale_route_fail')})."
            )
        if not gates.get("heartbeat_unchanged_interval_commits"):
            lines.append(
                "- heartbeat contract failed "
                f"(interval={short.get('n_interval_triggers')}, "
                f"unchanged={short.get('n_unchanged_interval_commits')})."
            )
        if short.get("n_locale_path_document_disagree"):
            lines.append(
                "- `locale_path_document_disagree` count "
                f"{short.get('n_locale_path_document_disagree')} "
                "(reported; path remains authoritative when localized)."
            )
    lines.extend(
        [
            "",
            "## Decision",
            "",
            "**STOP_FOR_LEAD_REVIEW.** Do not start the 8–12h corpus. Do not retune mom/gap.",
            "",
        ]
    )
    if short["failure_classes"]:
        lines.append(
            "Replacement long corpus remains blocked until a 5–15 min extension 1.3.3 "
            "/ catalog v1.2 recapture passes every v2.0.0 short-gate bar above, "
            "including the unchanged interarrival thresholds."
        )
        lines.append("")
    return "\n".join(lines) + "\n"


def write_reports(*, raw: Path, out_json: Path, out_md: Path) -> dict[str, Any]:
    payload = build_milestone_payload(raw)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out_md.write_text(render_markdown(payload), encoding="utf-8")
    return payload
