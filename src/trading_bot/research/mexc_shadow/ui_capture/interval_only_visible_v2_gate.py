"""Visible-tab interval-only short gate against frozen protocol v2.0.0.

Scores one 5–15 min logged-in TAOUSDT capture from extension 1.3.5.
Does not inspect mom/gap formulas, does not compute PnL, and does not
start ML, PAPER, LIVE, or the 8–12 h corpus. The whole session is scored;
bad periods are not cropped. Protocol v2 timing bars are unchanged.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_bot.research.mexc_shadow.ui_capture.catalog import (
    CATALOG_VERSION,
    SCHEMA_NAME,
)
from trading_bot.research.mexc_shadow.ui_capture.durable import is_session_record
from trading_bot.research.mexc_shadow.ui_capture.stage_diagnostic_analysis import (
    _stats,
    histogram_stats,
)
from trading_bot.research.mexc_shadow.ui_capture.stage_diagnostics import (
    expected_interval_opportunities,
    finite_number,
)
from trading_bot.research.mexc_shadow.ui_capture.store import iter_all_mappings
from trading_bot.research.mexc_shadow.ui_capture.v2_contract_gate import (
    FROZEN_INTERARRIVAL_LE_2000_MIN,
    FROZEN_P95_MAX_MS,
    FROZEN_SIMULTANEOUS_MIN,
    PROTOCOL_V1_VERSION,
    PROTOCOL_V2_AMENDMENT_COMMIT,
    PROTOCOL_V2_MERGE_COMMIT,
    PROTOCOL_VERSION,
    REQUIRED_INTERVAL_MS,
    score_v2_contract_gate,
)

MILESTONE = "MEXC_UI_INTERVAL_ONLY_VISIBLE_FINAL_V2_GATE"
MILESTONE_PASS = "MEXC_UI_INTERVAL_ONLY_VISIBLE_FINAL_V2_GATE_PASS"
MILESTONE_FAIL = "MEXC_UI_INTERVAL_ONLY_VISIBLE_FINAL_V2_GATE_FAIL"
MILESTONE_DECISION = "STOP_FOR_LEAD_REVIEW"
EXTENSION_EXPECTED = "1.3.5"
REQUIRED_CATALOG = CATALOG_VERSION

DEFAULT_CAPTURE = (
    Path("data")
    / "mexc_ui_capture"
    / "mexc_ui_capture_e03fc35f-4280-486c-8144-86ffb1aa6515_2026-09-07T18-30-52-811Z.ndjson"
)
SCORED_CAPTURE_SHA256 = (
    "d1d4c08f785efa489760cf33d853223277f00c4837ac9773249c5345f74e3f45"
)

# 1.3.4 visibility experiment, final-visible phase (not a pass bar).
PRIOR_EXTENSION = "1.3.4"
PRIOR_CAPTURE_SHA256 = "c3699bb2261b153dda093e36e801e9f352ffb02891345c7aea1b1ff1ba6c81c2"
PRIOR_PHASE = "final_visible"
PRIOR_CONTENT_WAIT_P95_MS = 1525.699999988079
PRIOR_CONTENT_QUEUE_DEPTH_HIGH_WATER = 26

# Operator screenshot taken during the scored session. Image stays gitignored.
SCREENSHOT_LOCAL = "2026-09-07T22:28:44+04:00"
SCREENSHOT_UTC = "2026-09-07T18:28:44Z"
SCREENSHOT_VISIBLE = {
    "last": 258.70,
    "fair": 258.72,
    "index": 258.71,
    "bid": 258.67,
    "ask": 258.73,
    "funding": "+0.0050%/01:31:18",
    "ui_labels": "English",
    "decimal": "point",
    "logged_in": True,
    "symbol": "TAOUSDT",
}


def _report_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _stamp_ms(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000.0
    except (TypeError, ValueError, AttributeError):
        return None


def _field_num(fields: dict[str, Any], name: str) -> float | None:
    rec = fields.get(name) or {}
    return finite_number(rec.get("value"))


def _trigger_count(counts: dict[str, Any], name: str) -> int:
    return int(counts.get(name) or 0)


def _nested_count(block: Any, name: str) -> int | None:
    if not isinstance(block, dict):
        return None
    value = block.get(name)
    if value is None:
        return None
    return int(value)


def _visible_to_hidden_count(lifecycle: list[dict[str, Any]]) -> int:
    n = 0
    for event in lifecycle:
        if event.get("kind") != "visibilitychange":
            continue
        if event.get("from_state") == "visible" and event.get("to_state") == "hidden":
            n += 1
    return n


def _nearest_snapshot(
    path: Path, utc_stamp: str
) -> dict[str, Any] | None:
    """Whole-session nearest raw row. Do not crop around the screenshot."""

    target = _stamp_ms(utc_stamp)
    if target is None:
        return None
    best: dict[str, Any] | None = None
    best_delta: float | None = None
    for payload in iter_all_mappings(path):
        if payload.get("schema") != SCHEMA_NAME:
            continue
        stamp = str(payload.get("received_at_local") or "")
        ms = _stamp_ms(stamp)
        if ms is None:
            continue
        delta = abs(ms - target)
        if best_delta is None or delta < best_delta:
            fields = payload.get("fields") or {}
            best_delta = delta
            best = {
                "sequence": payload.get("sequence"),
                "received_at_local": stamp,
                "trigger": payload.get("trigger"),
                "delta_ms": delta,
                "last": _field_num(fields, "last"),
                "bid": _field_num(fields, "bid"),
                "ask": _field_num(fields, "ask"),
                "mark": _field_num(fields, "mark"),
                "index": _field_num(fields, "index"),
                "mark_selector": (fields.get("mark") or {}).get("selector_id"),
                "index_selector": (fields.get("index") or {}).get("selector_id"),
            }
    return best


def screenshot_comparisons_for(path: Path, digest: str) -> list[dict[str, Any]]:
    """Bind the operator screenshot only to the scored 1.3.5 export."""

    if digest != SCORED_CAPTURE_SHA256:
        return []
    nearest = _nearest_snapshot(path, SCREENSHOT_UTC)
    if nearest is None:
        return []
    vis = SCREENSHOT_VISIBLE
    captured_last = finite_number(nearest.get("last"))
    captured_mark = finite_number(nearest.get("mark"))
    captured_index = finite_number(nearest.get("index"))
    captured_bid = finite_number(nearest.get("bid"))
    captured_ask = finite_number(nearest.get("ask"))
    ui_last = finite_number(vis["last"])
    ui_fair = finite_number(vis["fair"])
    ui_index = finite_number(vis["index"])
    ui_bid = finite_number(vis["bid"])
    ui_ask = finite_number(vis["ask"])
    scale_values = [
        ui_last,
        ui_fair,
        ui_index,
        ui_bid,
        ui_ask,
        captured_last,
        captured_mark,
        captured_index,
        captured_bid,
        captured_ask,
    ]
    scale_match = all(
        value is not None and 50.0 <= value <= 800.0 for value in scale_values
    )
    bid_lt_ask = (
        ui_bid is not None
        and ui_ask is not None
        and ui_bid < ui_ask
        and captured_bid is not None
        and captured_ask is not None
        and captured_bid < captured_ask
    )
    last_delta = (
        None if captured_last is None or ui_last is None else abs(captured_last - ui_last)
    )
    fair_delta = (
        None if captured_mark is None or ui_fair is None else abs(captured_mark - ui_fair)
    )
    index_delta = (
        None
        if captured_index is None or ui_index is None
        else abs(captured_index - ui_index)
    )
    not_swapped = (
        str(nearest.get("mark_selector") or "").startswith("header_struct:mark")
        and str(nearest.get("index_selector") or "").startswith("header_struct:index")
        and fair_delta is not None
        and index_delta is not None
        and fair_delta <= 0.05
        and index_delta <= 0.05
    )
    notes = (
        f"Absolute prices ~258. Last delta {last_delta} vs UI 258.70. "
        f"Fair/mark delta {fair_delta}; index delta {index_delta}. "
        "Ordinary book lag of 1–2 cents on bid/ask. Not a mark/index swap."
    )
    return [
        {
            "local_time": SCREENSHOT_LOCAL,
            "utc": SCREENSHOT_UTC,
            "filename": "Снимок экрана 2026-09-07 222844.png",
            "visible": vis,
            "nearest_snapshot": nearest.get("received_at_local"),
            "nearest_sequence": nearest.get("sequence"),
            "nearest_delta_ms": nearest.get("delta_ms"),
            "captured": {
                "last": captured_last,
                "bid": captured_bid,
                "ask": captured_ask,
                "mark": captured_mark,
                "index": captured_index,
            },
            "scale_match": scale_match,
            "bid_lt_ask": bid_lt_ask,
            "mark_index_not_swapped": not_swapped,
            "last_abs_delta": last_delta,
            "fair_abs_delta": fair_delta,
            "index_abs_delta": index_delta,
            "notes": notes,
        }
    ]


def _interval_only_audit(path: Path) -> dict[str, Any]:
    """Session-wide interval-only / visibility / FIFO audit. No cropping."""

    n_snapshots = 0
    n_missing_diag = 0
    n_wrong_extension = 0
    n_stale = 0
    n_mismatch = 0
    vis_states: Counter[str] = Counter()
    vis_extract: Counter[str] = Counter()
    first_trigger: str | None = None
    later_triggers: Counter[str] = Counter()
    session_extensions: list[str] = []
    lifecycle: list[dict[str, Any]] = []
    counters: dict[str, Any] = {}
    high_water: dict[str, Any] = {}
    histograms: dict[str, Any] = {}
    timer_mono: float | None = None
    stop_mono: float | None = None
    wait_vals: list[float | None] = []
    depth_vals: list[float | None] = []
    extract_vals: list[float | None] = []
    bg_wait_vals: list[float | None] = []
    delay_vals: list[float | None] = []
    bg_depth_vals: list[float | None] = []
    dirty_true = 0
    mut_ord_max = 0

    for payload in iter_all_mappings(path):
        if is_session_record(payload):
            ext = payload.get("extension_version")
            if ext is not None:
                session_extensions.append(str(ext))
            if payload.get("record_type") == "session_end":
                summary = payload.get("stage_diagnostic_summary") or {}
                if isinstance(summary, dict):
                    counters = dict(summary.get("counters") or {})
                    high_water = dict(summary.get("high_water") or {})
                    histograms = dict(summary.get("histograms") or {})
                    lifecycle = list(summary.get("lifecycle") or [])
            continue
        if payload.get("schema") != SCHEMA_NAME:
            continue
        n_snapshots += 1
        trigger = str(payload.get("trigger") or "")
        if first_trigger is None:
            first_trigger = trigger
        else:
            later_triggers[trigger] += 1
        diag = payload.get("stage_diagnostics")
        if not isinstance(diag, dict):
            n_missing_diag += 1
            continue
        ext = str(diag.get("extension_version") or "")
        if ext != EXTENSION_EXPECTED:
            n_wrong_extension += 1
        vis = str(diag.get("visibility_state") or "unknown")
        vis_x = str(diag.get("visibility_state_at_extract") or "unknown")
        vis_states[vis] += 1
        vis_extract[vis_x] += 1
        if diag.get("stale_generation"):
            n_stale += 1
        if diag.get("session_id_mismatch"):
            n_mismatch += 1
        if diag.get("dirty_since_last_interval"):
            dirty_true += 1
        mut_ord = diag.get("mutation_callback_ordinal")
        if isinstance(mut_ord, int):
            mut_ord_max = max(mut_ord_max, mut_ord)
        wait_vals.append(finite_number(diag.get("content_queue_wait_ms")))
        depth_vals.append(finite_number(diag.get("content_queue_depth")))
        extract_vals.append(finite_number(diag.get("extract_duration_ms")))
        bg_wait_vals.append(finite_number(diag.get("background_queue_wait_ms")))
        bg_depth_vals.append(finite_number(diag.get("background_queue_depth")))
        delay_vals.append(finite_number(diag.get("callback_delay_ms")))

    for event in lifecycle:
        kind = event.get("kind")
        mono = finite_number(event.get("mono"))
        if kind == "timer_registered" and mono is not None:
            timer_mono = mono
        if kind == "session_stop" and mono is not None:
            stop_mono = mono

    expected = counters.get("expected_interval_opportunities")
    if expected is None:
        expected = expected_interval_opportunities(
            timer_mono, stop_mono, REQUIRED_INTERVAL_MS
        )
    timer_callbacks = int(counters.get("timer_callbacks") or 0)
    mutation_callbacks = int(counters.get("mutation_callbacks") or 0)
    enqueued = counters.get("callbacks_enqueued") or {}
    persisted = counters.get("snapshots_persisted") or {}
    wait_stats = _stats(wait_vals)
    depth_stats = _stats(depth_vals)
    depth_hw = high_water.get("content_queue_depth")
    if depth_hw is None and depth_stats.get("max") is not None:
        depth_hw = depth_stats["max"]
    return {
        "n_snapshots": n_snapshots,
        "n_missing_stage_diagnostics": n_missing_diag,
        "n_wrong_extension": n_wrong_extension,
        "session_extension_versions": session_extensions,
        "first_trigger": first_trigger,
        "later_trigger_counts": dict(later_triggers),
        "visibility_states": dict(vis_states),
        "visibility_states_at_extract": dict(vis_extract),
        "n_stale_generation": n_stale,
        "n_session_id_mismatch": n_mismatch,
        "n_dirty_since_last_interval": dirty_true,
        "mutation_callback_ordinal_max": mut_ord_max,
        "visible_to_hidden_transitions": _visible_to_hidden_count(lifecycle),
        "lifecycle_kinds": dict(Counter(str(event.get("kind")) for event in lifecycle)),
        "lifecycle": lifecycle,
        "expected_interval_opportunities": int(expected or 0),
        "timer_callbacks": timer_callbacks,
        "mutation_callbacks": mutation_callbacks,
        "mutation_dirty_sets": int(counters.get("mutation_dirty_sets") or 0),
        "manual_callbacks": int(counters.get("manual_callbacks") or 0),
        "callbacks_enqueued": dict(enqueued) if isinstance(enqueued, dict) else {},
        "snapshots_persisted": dict(persisted) if isinstance(persisted, dict) else {},
        "stale_generation_detected": int(counters.get("stale_generation_detected") or 0),
        "session_id_mismatch_detected": int(
            counters.get("session_id_mismatch_detected") or 0
        ),
        "outstanding_at_stop": int(counters.get("outstanding_at_stop") or 0),
        "high_water": high_water,
        "content_queue_depth_high_water": depth_hw,
        "content_queue_wait_ms": wait_stats,
        "content_queue_depth": depth_stats,
        "extract_duration_ms": _stats(extract_vals),
        "background_queue_wait_ms": _stats(bg_wait_vals),
        "background_queue_depth": _stats(bg_depth_vals),
        "callback_delay_ms": _stats(delay_vals),
        "histogram_append_duration_ms": histogram_stats(
            histograms.get("append_duration_ms")
            if isinstance(histograms.get("append_duration_ms"), dict)
            else None
        ),
        "histogram_total_ack_latency_ms": histogram_stats(
            histograms.get("total_ack_latency_ms")
            if isinstance(histograms.get("total_ack_latency_ms"), dict)
            else None
        ),
        "histogram_content_queue_wait_ms": histogram_stats(
            histograms.get("content_queue_wait_ms")
            if isinstance(histograms.get("content_queue_wait_ms"), dict)
            else None
        ),
    }


def score_interval_only_visible_v2_gate(path: Path) -> dict[str, Any]:
    """Frozen v2 bars plus interval-only visible-tab session bars."""

    v2 = score_v2_contract_gate(path)
    audit = _interval_only_audit(path)
    digest = str(v2.get("sha256") or "")
    shots = screenshot_comparisons_for(path, digest)
    triggers = v2.get("trigger_counts") or {}
    n_manual = _trigger_count(triggers, "manual")
    n_interval = _trigger_count(triggers, "interval")
    n_mutation = _trigger_count(triggers, "mutation")
    n_snap = int(v2.get("n_snapshots") or 0)
    later = audit.get("later_trigger_counts") or {}
    later_only_interval = (
        n_snap > 1
        and audit.get("first_trigger") == "manual"
        and set(later) <= {"interval"}
        and _trigger_count(later, "interval") == n_snap - 1
    )
    vis_ok = (
        n_snap > 0
        and audit["n_missing_stage_diagnostics"] == 0
        and audit["visibility_states"] == {"visible": n_snap}
        and audit["visibility_states_at_extract"] == {"visible": n_snap}
    )
    session_ext_ok = bool(audit["session_extension_versions"]) and all(
        value == EXTENSION_EXPECTED for value in audit["session_extension_versions"]
    )
    extension_ok = (
        n_snap > 0
        and audit["n_missing_stage_diagnostics"] == 0
        and audit["n_wrong_extension"] == 0
        and session_ext_ok
    )
    enqueued_mut = _nested_count(audit.get("callbacks_enqueued"), "mutation")
    persisted_mut = _nested_count(audit.get("snapshots_persisted"), "mutation")
    mutation_not_enqueued = (
        n_mutation == 0
        and (enqueued_mut is None or enqueued_mut == 0)
        and (persisted_mut is None or persisted_mut == 0)
    )
    expected = int(audit.get("expected_interval_opportunities") or 0)
    timer_cb = int(audit.get("timer_callbacks") or 0)
    persisted_interval = _nested_count(audit.get("snapshots_persisted"), "interval")
    if persisted_interval is None:
        persisted_interval = n_interval
    heartbeat_expected_ok = expected > 0 and expected == timer_cb
    heartbeat_persisted_ok = timer_cb > 0 and persisted_interval == timer_cb
    screenshot_ok: bool | None
    if digest == SCORED_CAPTURE_SHA256:
        screenshot_ok = bool(shots) and all(
            shot.get("scale_match")
            and shot.get("bid_lt_ask")
            and shot.get("mark_index_not_swapped")
            for shot in shots
        )
    else:
        screenshot_ok = None
    extra_gates = {
        "extension_1_3_5": extension_ok,
        "exactly_one_manual_start_raw_row": n_manual == 1
        and audit.get("first_trigger") == "manual",
        "remaining_raw_rows_interval": later_only_interval and n_interval == n_snap - 1,
        "zero_mutation_raw_rows": n_mutation == 0,
        "mutation_callbacks_not_enqueued": mutation_not_enqueued,
        "continuous_visibility_visible": vis_ok,
        "zero_visible_to_hidden": audit["visible_to_hidden_transitions"] == 0,
        "heartbeat_expected_equals_timer_callbacks": heartbeat_expected_ok,
        "heartbeat_persisted_equals_timer_callbacks": heartbeat_persisted_ok,
        "no_stale_generation": audit["n_stale_generation"] == 0
        and audit["stale_generation_detected"] == 0,
        "no_session_id_mismatch": audit["n_session_id_mismatch"] == 0
        and audit["session_id_mismatch_detected"] == 0,
        "screenshot_agreement": screenshot_ok,
    }
    gates = {**v2["gates"], **extra_gates}
    failure_classes = [name for name, ok in gates.items() if ok is False]
    passed = not failure_classes
    wait_p95 = audit["content_queue_wait_ms"].get("p95")
    return {
        **v2,
        "extension_expected": EXTENSION_EXPECTED,
        "interval_only_audit": audit,
        "screenshots": shots,
        "fifo_before_after": {
            "prior_extension": PRIOR_EXTENSION,
            "prior_capture_sha256": PRIOR_CAPTURE_SHA256,
            "prior_phase": PRIOR_PHASE,
            "prior_content_wait_p95_ms": PRIOR_CONTENT_WAIT_P95_MS,
            "prior_content_queue_depth_high_water": (
                PRIOR_CONTENT_QUEUE_DEPTH_HIGH_WATER
            ),
            "now_content_wait_p95_ms": wait_p95,
            "now_content_wait_max_ms": audit["content_queue_wait_ms"].get("max"),
            "now_content_queue_depth_high_water": audit[
                "content_queue_depth_high_water"
            ],
            "improvement_percent_required": False,
        },
        "gates": gates,
        "v2_gates": v2["gates"],
        "extra_gates": extra_gates,
        "failure_classes": failure_classes,
        "passed": passed,
        "whole_session_scored": True,
        "bad_periods_cropped": False,
        "mom_gap_inspected": False,
    }


def build_milestone_payload(raw: Path) -> dict[str, Any]:
    short = score_interval_only_visible_v2_gate(raw)
    status = MILESTONE_PASS if short["passed"] else MILESTONE_FAIL
    return {
        "milestone": MILESTONE,
        "status": status,
        "decision": MILESTONE_DECISION,
        "ml_status": "NOT_STARTED",
        "paper": False,
        "live": False,
        "strategy_tuning": False,
        "mom_gap_inspected": False,
        "n_cells_executed": 0,
        "long_capture_started": False,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "extension_expected": EXTENSION_EXPECTED,
        "catalog_required": REQUIRED_CATALOG,
        "protocol": {
            "version_scored": PROTOCOL_VERSION,
            "version_not_scored": PROTOCOL_V1_VERSION,
            "amendment_doc": (
                "docs/mexc_mom_gap_hypothesis_protocol_v2_data_contract_amendment.md"
            ),
            "v1_blob": "4e8d05940b2bc759acff881779159a7bcba1604e",
            "v2_merge_commit": PROTOCOL_V2_MERGE_COMMIT,
            "v2_amendment_commit": PROTOCOL_V2_AMENDMENT_COMMIT,
        },
        "frozen_protocol_v2": {
            "simultaneous_min": FROZEN_SIMULTANEOUS_MIN,
            "interarrival_le_2000_min": FROZEN_INTERARRIVAL_LE_2000_MIN,
            "p95_max_ms": FROZEN_P95_MAX_MS,
            "hours_requirement": "not applied to this 5-15 min gate",
            "thresholds_relaxed": False,
        },
        "short_validation": short,
        "notes": [
            "Scored against protocol v2.0.0 data admissibility, not v1.0.0.",
            "Whole session scored; no hidden-phase crop and no bad-period crop.",
            "Did not inspect mom/gap formulas or execute any of the 21 cells.",
            "Did not retune frozen profiles, thresholds, exits, or lookbacks.",
            "Did not start the 8-12h identification corpus.",
        ],
    }


def _gate_cell(ok: bool | None) -> str:
    if ok is True:
        return "PASS"
    if ok is False:
        return "FAIL"
    return "N/A"


def render_markdown(payload: dict[str, Any]) -> str:
    short = payload["short_validation"]
    inter = short.get("interarrival_ms") or {}
    audit = short.get("interval_only_audit") or {}
    fifo = short.get("fifo_before_after") or {}
    wait = audit.get("content_queue_wait_ms") or {}
    extract = audit.get("extract_duration_ms") or {}
    bg = audit.get("background_queue_wait_ms") or {}
    delay = audit.get("callback_delay_ms") or {}
    append = audit.get("histogram_append_duration_ms") or {}
    ack = audit.get("histogram_total_ack_latency_ms") or {}
    lines = [
        "# MEXC UI interval-only visible final v2 gate",
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
        "Score a 5–15 minute logged-in TAOUSDT capture from extension 1.3.5 /",
        "catalog v1.2 after the interval-only remediation. Protocol **v2.0.0**",
        "data-admissibility bars are unchanged. The whole running session is",
        "scored while visible; hidden-tab 2 Hz remains out of scope.",
        "",
        "## Capture",
        "",
        f"- path: `{short.get('path')}`",
        f"- sha256: `{short.get('sha256')}`",
        f"- snapshots: {short.get('n_snapshots')}",
        f"- duration minutes: {short.get('duration_minutes')}",
        f"- page_paths: `{short.get('page_paths')}`",
        f"- parser_locale: `{short.get('locales')}`",
        f"- locale_source: `{short.get('locale_sources')}`",
        f"- document_lang: `{short.get('document_langs')}`",
        f"- locale routes: `{short.get('locale_routes')}`",
        f"- catalog: `{short.get('catalog_versions')}`",
        f"- sample_interval_ms: `{short.get('sample_interval_ms')}`",
        f"- trigger_counts: `{short.get('trigger_counts')}`",
        (
            "- heartbeat expected/timer/persisted interval: "
            f"{audit.get('expected_interval_opportunities')} / "
            f"{audit.get('timer_callbacks')} / "
            f"{(audit.get('snapshots_persisted') or {}).get('interval')}"
        ),
        (
            "- mutation callbacks / dirty_sets / enqueued / raw rows: "
            f"{audit.get('mutation_callbacks')} / "
            f"{audit.get('mutation_dirty_sets')} / "
            f"{(audit.get('callbacks_enqueued') or {}).get('mutation')} / "
            f"{(short.get('trigger_counts') or {}).get('mutation', 0)}"
        ),
        f"- visibility_states: `{audit.get('visibility_states')}`",
        (
            "- visible→hidden transitions: "
            f"{audit.get('visible_to_hidden_transitions')}"
        ),
        (
            "- stale_generation rows/counter: "
            f"{audit.get('n_stale_generation')} / "
            f"{audit.get('stale_generation_detected')}"
        ),
        (
            "- session_id_mismatch rows/counter: "
            f"{audit.get('n_session_id_mismatch')} / "
            f"{audit.get('session_id_mismatch_detected')}"
        ),
        (
            "- median last/bid/ask/mark/index: "
            f"{short.get('median_last')} / {short.get('median_bid')} / "
            f"{short.get('median_ask')} / {short.get('median_mark')} / "
            f"{short.get('median_index')}"
        ),
        (
            f"- simultaneous bid+ask+last+mark+index: "
            f"{short.get('simultaneous_bid_ask_last_mark_index')} "
            f"({short.get('simultaneous_pct')}%)"
        ),
        f"- DATA_INVALID: {short.get('n_data_invalid')}",
        f"- STARTUP_WARMUP: {short.get('n_startup_warmup')}",
        (
            "- raw interarrival p50/p90/p95/p99/max: "
            f"{inter.get('p50_ms')} / {inter.get('p90_ms')} / "
            f"{inter.get('p95_ms')} / {inter.get('p99_ms')} / {inter.get('max_ms')}"
        ),
        (
            "- frac≤2000ms / n>2000ms / n>1000ms: "
            f"{short.get('interarrival_frac_le_2000_ms')} / "
            f"{short.get('interarrival_n_gt_2000_ms')} / "
            f"{short.get('interarrival_n_gt_1000_ms')}"
        ),
        f"- replay_canonical_sha256: `{short.get('replay_canonical_sha256')}`",
        f"- passed: **{short['passed']}**",
        "",
        "## FIFO before/after (1.3.4 final-visible vs this session)",
        "",
        "Not a fitted-percentage pass bar. Measured evidence only.",
        "",
        (
            f"- prior {fifo.get('prior_phase')} content wait p95: "
            f"{fifo.get('prior_content_wait_p95_ms')} ms"
        ),
        (
            "- prior content queue depth high-water: "
            f"{fifo.get('prior_content_queue_depth_high_water')}"
        ),
        (
            "- now content wait p50/p90/p95/p99/max: "
            f"{wait.get('p50')} / {wait.get('p90')} / {wait.get('p95')} / "
            f"{wait.get('p99')} / {wait.get('max')} ms"
        ),
        (
            "- now content queue depth high-water: "
            f"{fifo.get('now_content_queue_depth_high_water')}"
        ),
        (
            "- extract duration p50/p95/max: "
            f"{extract.get('p50')} / {extract.get('p95')} / {extract.get('max')} ms"
        ),
        (
            "- background wait p50/p95/max: "
            f"{bg.get('p50')} / {bg.get('p95')} / {bg.get('max')} ms"
        ),
        (
            "- callback delay p50/p95/max: "
            f"{delay.get('p50')} / {delay.get('p95')} / {delay.get('max')} ms"
        ),
        (
            "- append duration (histogram) p50/p95/max: "
            f"{append.get('p50')} / {append.get('p95')} / {append.get('max')} ms"
        ),
        (
            "- ACK latency (histogram) p50/p95/max: "
            f"{ack.get('p50')} / {ack.get('p95')} / {ack.get('max')} ms"
        ),
        "",
        "| Gate | Result |",
        "| --- | --- |",
    ]
    for name, ok in short["gates"].items():
        lines.append(f"| `{name}` | {_gate_cell(ok)} |")
    lines.extend(["", "### Failure classes", ""])
    if short["failure_classes"]:
        for name in short["failure_classes"]:
            lines.append(f"- `{name}`")
    else:
        lines.append("- none")
    lines.extend(["", "### Screenshots vs nearest snapshots", ""])
    shots = short.get("screenshots") or []
    if not shots:
        lines.append("No screenshot table bound to this SHA-256.")
    else:
        for shot in shots:
            vis = shot["visible"]
            cap = shot["captured"]
            lines.append(
                f"- {shot['local_time']}: UI last `{vis['last']}` / Fair `{vis['fair']}` / "
                f"Index `{vis['index']}` / bid `{vis['bid']}` / ask `{vis['ask']}` "
                f"vs snapshot `{shot['nearest_snapshot']}` last {cap['last']} "
                f"bid {cap['bid']} ask {cap['ask']} mark {cap['mark']} "
                f"index {cap['index']}. {shot['notes']}"
            )
    lines.extend(["", "## Findings", ""])
    if short["passed"]:
        lines.append(
            "Every applicable frozen protocol-v2 bar and every interval-only "
            "visible-tab bar passed. The 8–12h identification corpus is still "
            "not started."
        )
    else:
        lines.append(
            "This sample is **not** ADMISSIBLE. Failure classes are listed above "
            "and were not relaxed."
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
    return "\n".join(lines) + "\n"


def write_reports(*, raw: Path, out_json: Path, out_md: Path) -> dict[str, Any]:
    payload = build_milestone_payload(raw)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    out_md.write_text(render_markdown(payload), encoding="utf-8")
    return payload
