"""Final 5–15 min locale/header data-semantics gate.

Scores one logged-in TAOUSDT capture against the frozen long-corpus
admissibility bars that apply to a short sample (95% simultaneous
bid/ask/last/mark/index, 99% interarrival ≤2000 ms, p95 ≤1000 ms).
The 8-hour corpus-length bar is not applied here.

Does not import or run mom/gap hypothesis replay. Does not retune
profiles, thresholds, exits, or lookbacks.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_bot.research.mexc_shadow.ui_capture.long_observation import (
    scan_long_capture,
)
from trading_bot.research.mexc_shadow.ui_capture.normalize import (
    observation_from_snapshot,
    snapshot_from_mapping,
)
from trading_bot.research.mexc_shadow.ui_capture.parse import locale_from_pathname
from trading_bot.research.mexc_shadow.ui_capture.quality import summarize_capture
from trading_bot.research.mexc_shadow.ui_capture.store import iter_all_mappings

SHORT_MIN_MS = 5 * 60 * 1000
SHORT_MAX_MS = 15 * 60 * 1000
TAO_LAST_MIN = 50.0
TAO_LAST_MAX = 800.0
REQUIRED_CATALOG = "v1.2"
REQUIRED_PATH_PREFIX = "/ru-RU/futures/"
FROZEN_SIMULTANEOUS_MIN = 0.95
FROZEN_INTERARRIVAL_LE_2000_MIN = 0.99
FROZEN_P95_MAX_MS = 1000.0
STRATEGY_FIELDS = ("bid", "ask", "last", "mark", "index")

# Operator 1.3.2 export scored by this milestone. Not rewritten.
SCORED_CAPTURE_SHA256 = (
    "5665207fd95c46611fecf8a2082ba39ceb5809ada5f7dd26aefdb4807f9613e9"
)


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


def score_final_gate(path: Path) -> dict[str, Any]:
    """Score one 5–15 min capture against locale/header continuity gates."""

    quality = summarize_capture(path)
    scan = scan_long_capture(path)
    n_considered = 0
    n_strategy_ready = 0
    lasts: list[float] = []
    bids: list[float] = []
    asks: list[float] = []
    marks: list[float] = []
    indexes: list[float] = []
    locales: Counter[str] = Counter()
    field_parser_locales: Counter[str] = Counter()
    paths: Counter[str] = Counter()
    catalogs: Counter[str] = Counter()
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

    for payload in iter_all_mappings(path):
        if payload.get("schema") != "mexc_ui_raw_snapshot":
            continue
        snap = snapshot_from_mapping(payload)
        n_considered += 1
        locale = str(snap.ui_locale or snap.parser_mode or "unknown")
        locales[locale] += 1
        paths[str(snap.page_path or "")] += 1
        catalogs[str(snap.selector_catalog_version or "")] += 1
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
        ready = all(
            (obs.symbol, obs.bid, obs.ask, obs.last, obs.mark, obs.index)
        )
        if ready:
            n_strategy_ready += 1

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
    # Inclusive 5–15 min window: prior 15.26 min recapture was out of range.
    duration_ok = (
        duration_ms is not None and SHORT_MIN_MS <= float(duration_ms) <= SHORT_MAX_MS
    )
    median_last = _median(lasts)
    scale_ok = (
        median_last is not None
        and TAO_LAST_MIN <= median_last <= TAO_LAST_MAX
        and scale_outliers == 0
    )
    dominant_path = paths.most_common(1)[0][0] if paths else ""
    path_ok = dominant_path.startswith(REQUIRED_PATH_PREFIX)
    locale_ok = locales.get("ru-RU", 0) == n_considered and n_considered > 0
    catalog_ok = catalogs.get(REQUIRED_CATALOG, 0) == n_considered and n_considered > 0
    alias_ok = min_alias is not None and min_alias > 0
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
    sessions = scan.get("sessions") or []
    storage_errors = list(scan.get("storage_errors") or [])
    for row in sessions:
        if row.get("storage_error"):
            storage_errors.append(str(row["storage_error"]))
        if str(row.get("status") or "") == "failed":
            storage_errors.append(f"session {row.get('session_id')} status=failed")
    seq_ok = not quality.sequence_diagnostics and not any(
        row.get("sequence_gaps") or row.get("client_sequence_mismatches") for row in sessions
    )
    # Fair must land on header_struct:mark and Index on header_struct:index.
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
    inter = quality.interarrival_ms or {}
    # Recompute the exact ≤2000 ms fraction. Quality p95 uses a rank estimator.
    arrivals: list[float] = []
    for payload in iter_all_mappings(path):
        if payload.get("schema") != "mexc_ui_raw_snapshot":
            continue
        raw_stamp = payload.get("received_at_local")
        if not raw_stamp:
            continue
        try:
            arrivals.append(
                datetime.fromisoformat(str(raw_stamp).replace("Z", "+00:00")).timestamp()
                * 1000.0
            )
        except ValueError:
            continue
    deltas = [arrivals[i] - arrivals[i - 1] for i in range(1, len(arrivals))]
    le_2000 = sum(1 for delta in deltas if delta <= 2000.0)
    frac_le_2000 = le_2000 / len(deltas) if deltas else 0.0
    p95 = inter.get("p95_ms")
    interarrival_ok = (
        frac_le_2000 >= FROZEN_INTERARRIVAL_LE_2000_MIN
        and p95 is not None
        and float(p95) <= FROZEN_P95_MAX_MS
    )
    implied_locale = locale_from_pathname(dominant_path)
    # ru-RU decimal comma must survive as DOM raw_text, not String(parsed).
    raw_text_ru_ok = locale_ok and raw_has_comma > 0
    gates = {
        "duration_5_to_15_min": duration_ok,
        "page_path_ru_RU_futures": path_ok,
        "parser_locale_ru_RU": locale_ok,
        "catalog_v1_2": catalog_ok,
        "header_alias_count_positive": alias_ok,
        "symbol_taousdt": symbol_ok,
        "absolute_price_scale": scale_ok,
        "bid_lt_ask": crossed == 0,
        "mark_index_header_struct": mark_struct > 0 and index_struct > 0 and not swapped,
        "no_selector_ambiguity_burst": ambiguity == 0,
        "no_post_readiness_data_invalid": data_invalid == 0,
        "sequence_storage_ok": seq_ok and not storage_errors,
        "export_replay_deterministic": quality.replay_determinism_sha256 is not None,
        "simultaneous_coverage_ge_95pct": simultaneous_rate >= FROZEN_SIMULTANEOUS_MIN,
        "frozen_interarrival": interarrival_ok,
        "raw_text_ru_decimal_comma": raw_text_ru_ok,
    }
    failure_classes = [name for name, ok in gates.items() if not ok]
    passed = not failure_classes
    return {
        "path": _report_path(path),
        "sha256": sha256_file(path),
        "n_snapshots": n_considered,
        "duration_ms": duration_ms,
        "duration_minutes": (
            None if duration_ms is None else round(float(duration_ms) / 60_000.0, 4)
        ),
        "page_paths": dict(paths),
        "locales": dict(locales),
        "field_parser_locales": dict(field_parser_locales),
        "implied_locale_from_path": implied_locale,
        "catalog_versions": dict(catalogs),
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
        "simultaneous_bid_ask_last_mark_index": simultaneous,
        "simultaneous_rate": simultaneous_rate,
        "simultaneous_pct": round(simultaneous_rate * 100.0, 4),
        "missing_field_bursts": missing_bursts,
        "n_missing_field_bursts": len(missing_bursts),
        "n_data_invalid": data_invalid,
        "n_startup_warmup": scan.get("n_startup_warmup"),
        "n_ready_valid": scan.get("n_ready_valid"),
        "class_counts": scan.get("class_counts"),
        "warmup_and_invalid_bursts": scan.get("warmup_and_invalid_bursts") or [],
        "n_bid_ge_ask": crossed,
        "raw_text_has_comma": raw_has_comma,
        "raw_text_has_dot": raw_has_dot,
        "selector_diagnostics": scan.get("selector_diagnostics"),
        "first_ready": scan.get("first_ready"),
        "mark_selectors": dict(mark_selectors),
        "index_selectors": dict(index_selectors),
        "replay_canonical_sha256": quality.replay_determinism_sha256,
        "storage_errors": storage_errors,
        "sessions": sessions,
        "sequence_diagnostics": quality.sequence_diagnostics,
        "interarrival_ms": inter,
        "interarrival_frac_le_2000_ms": frac_le_2000,
        "interarrival_n_gt_2000_ms": len(deltas) - le_2000,
        "timing_adequacy": quality.timing_adequacy,
        "n_sessions": quality.n_sessions,
        "n_chunks_total": quality.n_chunks_total,
        "quality_invalid_reasons": quality.invalid_reasons,
        "gates": gates,
        "failure_classes": failure_classes,
        "passed": passed,
    }


def screenshot_comparisons_for(digest: str) -> list[dict[str, Any]]:
    """Operator screenshots for the scored 1.3.2 export only."""

    if digest != SCORED_CAPTURE_SHA256:
        return []
    return [
        {
            "local_time": "2026-09-07T16:48:04+04:00",
            "utc": "2026-09-07T12:48:04Z",
            "visible": {
                "last": "265.02",
                "index": "265.08",
                "fair": "265.09",
                "bid": "264.99",
                "ask": "265.03",
                "ui_labels": "English",
                "decimal": "point",
            },
            "nearest_snapshot": "2026-09-07T12:48:04.125Z",
            "captured": {
                "last": 265.04,
                "bid": 265.03,
                "ask": 265.08,
                "mark": 265.08,
                "index": 265.08,
            },
            "scale_match": True,
            "notes": "Last within 2 cents. BBO/Fair differ by ordinary lag. Index matches 265.08.",
        },
        {
            "local_time": "2026-09-07T16:53:55+04:00",
            "utc": "2026-09-07T12:53:55Z",
            "visible": {
                "last": "265.44",
                "index": "265.48",
                "fair": "265.49",
                "bid": "265.48",
                "ask": "265.51",
                "ui_labels": "English",
                "decimal": "point",
            },
            "nearest_snapshot": "2026-09-07T12:53:54.917Z",
            "captured": {
                "last": 265.44,
                "bid": 265.53,
                "ask": 265.58,
                "mark": 265.50,
                "index": 265.49,
            },
            "scale_match": True,
            "notes": (
                "Last exact. Fair/index within 1–2 cents. "
                "Wrapper BBO a few cents behind the screenshot book."
            ),
        },
    ]


def build_milestone_payload(raw: Path) -> dict[str, Any]:
    short = score_final_gate(raw)
    passed = bool(short["passed"])
    if passed:
        status = "MEXC_UI_LOCALE_DATA_SEMANTICS_FINAL_GATE_PASS"
    else:
        status = "MEXC_UI_LOCALE_DATA_SEMANTICS_FINAL_GATE_FAIL"
    return {
        "milestone": "MEXC_UI_LOCALE_DATA_SEMANTICS_FINAL_GATE_V1",
        "status": status,
        "decision": "STOP_FOR_LEAD_REVIEW",
        "ml_status": "NOT_STARTED",
        "paper": False,
        "live": False,
        "strategy_tuning": False,
        "mom_gap_inspected": False,
        "long_capture_started": False,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "extension_expected": "1.3.2",
        "catalog_required": REQUIRED_CATALOG,
        "frozen_protocol_preview": {
            "simultaneous_min": FROZEN_SIMULTANEOUS_MIN,
            "interarrival_le_2000_min": FROZEN_INTERARRIVAL_LE_2000_MIN,
            "p95_max_ms": FROZEN_P95_MAX_MS,
            "hours_requirement": "not applied to this 5-15 min gate",
            "catalog_text_in_protocol": "v1.1",
            "catalog_required_here": REQUIRED_CATALOG,
        },
        "short_validation": short,
        "screenshots": screenshot_comparisons_for(str(short.get("sha256") or "")),
        "notes": [
            "Did not inspect or execute MEXC_MOM_GAP_HYPOTHESIS_PROTOCOL_DESIGN_V1.",
            "Did not retune frozen profiles, thresholds, exits, or lookbacks.",
            "Did not start a replacement long corpus.",
        ],
    }


def render_markdown(payload: dict[str, Any]) -> str:
    short = payload["short_validation"]
    lines = [
        "# MEXC UI locale data semantics final gate v1",
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
        "MOM/GAP: **not inspected**",
        "",
        "Long capture: **not started**",
        "",
        "## Purpose",
        "",
        "Score a 5–15 minute logged-in `/ru-RU/futures/TAO_USDT` capture from",
        "extension 1.3.2 / catalog v1.2 before any replacement long corpus.",
        "Frozen 95% simultaneous coverage and interarrival bars are applied to",
        "this sample. The 8-hour corpus-length bar is not applied here.",
        "",
        "## Capture",
        "",
        f"- path: `{short['path']}`",
        f"- sha256: `{short['sha256']}`",
        f"- snapshots: {short['n_snapshots']}",
        f"- duration minutes: {short['duration_minutes']}",
        f"- page_paths: `{short['page_paths']}`",
        f"- locales: `{short['locales']}`",
        f"- field parser_locale: `{short.get('field_parser_locales')}`",
        f"- catalog: `{short['catalog_versions']}`",
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
        f"- DATA_INVALID: {short['n_data_invalid']}",
        f"- STARTUP_WARMUP: {short['n_startup_warmup']}",
        f"- missing-field bursts: {short['n_missing_field_bursts']}",
        (
            "- raw interarrival p95_ms / frac≤2000ms / n>2000ms: "
            f"{(short.get('interarrival_ms') or {}).get('p95_ms')} / "
            f"{short.get('interarrival_frac_le_2000_ms')} / "
            f"{short.get('interarrival_n_gt_2000_ms')}"
        ),
        f"- raw_text comma/dot counts: {short['raw_text_has_comma']} / {short['raw_text_has_dot']}",
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
                f"last {cap['last']} mark {cap['mark']} index {cap['index']}. "
                f"{shot['notes']}"
            )
    gates = short["gates"]
    lines.extend(["", "## Findings", ""])
    if short["passed"]:
        lines.append("All short-gate bars passed. Long capture is still not started.")
    else:
        lines.append(
            "This sample is **not** admissible as the ru-RU locale/header final gate."
        )
        if not gates.get("page_path_ru_RU_futures"):
            lines.append(
                f"- page_path observed `{short['page_paths']}`; required prefix "
                f"`{REQUIRED_PATH_PREFIX}`."
            )
        if not gates.get("parser_locale_ru_RU"):
            lines.append(
                f"- parser_locale observed `{short['locales']}` "
                f"(fields `{short.get('field_parser_locales')}`); required `ru-RU`."
            )
        if not gates.get("raw_text_ru_decimal_comma"):
            lines.append(
                "- retained raw_text decimal semantics: "
                f"{short['raw_text_has_comma']} comma / {short['raw_text_has_dot']} dot "
                "tokens on bid/ask/last/mark/index."
            )
        if not gates.get("frozen_interarrival"):
            frac = short.get("interarrival_frac_le_2000_ms")
            lines.append(
                "- frozen interarrival preview failed "
                f"(p95_ms={(short.get('interarrival_ms') or {}).get('p95_ms')}, "
                f"frac≤2000ms={frac}, n>2000ms={short.get('interarrival_n_gt_2000_ms')}). "
                "These bars are not relaxed."
            )
        if not gates.get("page_path_ru_RU_futures") or not gates.get("parser_locale_ru_RU"):
            lines.append(
                "Header/catalog/scale/continuity on this file are reported above; they do "
                "not override the ru-RU path/locale requirement."
            )
    lines.extend(
        [
            "",
            "## Decision",
            "",
            "**STOP_FOR_LEAD_REVIEW.** Do not start the long capture. Do not retune mom/gap.",
            "",
        ]
    )
    if short["failure_classes"]:
        lines.append(
            "Replacement long corpus remains blocked until a 5–15 min "
            "`/ru-RU/futures/TAO_USDT` recapture passes every gate above."
        )
        lines.append("")
    return "\n".join(lines) + "\n"


def write_reports(*, raw: Path, out_json: Path, out_md: Path) -> dict[str, Any]:
    payload = build_milestone_payload(raw)
    out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out_md.write_text(render_markdown(payload), encoding="utf-8")
    return payload
