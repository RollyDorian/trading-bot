"""Phase A gates and descriptive long-capture stats. No strategy tuning."""

from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_bot.research.mexc_shadow.ui_capture.durable import is_session_record
from trading_bot.research.mexc_shadow.ui_capture.normalize import (
    observation_from_snapshot,
    snapshot_from_mapping,
)
from trading_bot.research.mexc_shadow.ui_capture.quality import (
    _ok_price,
    _percentile,
    quality_as_dict,
    summarize_capture,
)
from trading_bot.research.mexc_shadow.ui_capture.replay import replay_capture_smoke
from trading_bot.research.mexc_shadow.ui_capture.schema import CaptureQualityReport
from trading_bot.research.mexc_shadow.ui_capture.store import iter_all_mappings

HORIZONS_S = (1, 2, 5, 10, 30, 60)
MOVE_BPS = (1, 2, 3, 5, 10)
XCORR_LAGS_S = tuple(range(-5, 6))
PRICE_FIELDS = ("last", "mid", "mark", "index")
QUOTE_FIELDS = ("bid", "ask", "last", "mark", "index")
AMBIGUOUS_REASON_TOKENS = ("ambiguous",)
PHASE_A_MIN_DURATION_MS = 8 * 60 * 1000
PHASE_A_MAX_DURATION_MS = 20 * 60 * 1000
PHASE_B_MIN_DURATION_MS = 8 * 60 * 60 * 1000
PHASE_B_MAX_DURATION_MS = 12 * 60 * 60 * 1000 + 30 * 60 * 1000
HYDRATION_CDP_SESSION_ID = "hydration-gate-v1-tao-2026-09-01"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _ms(stamp: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() * 1000.0


def _gate(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


def _numeric_dist(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    n = len(ordered)
    mean = sum(ordered) / n if n else None
    std = None
    if n > 1 and mean is not None:
        std = math.sqrt(sum((value - mean) ** 2 for value in ordered) / (n - 1))
    return {
        "n": n,
        "mean": mean,
        "std": std,
        "min": ordered[0] if ordered else None,
        "max": ordered[-1] if ordered else None,
        "p50": _percentile(ordered, 0.50),
        "p90": _percentile(ordered, 0.90),
        "p95": _percentile(ordered, 0.95),
        "p99": _percentile(ordered, 0.99),
    }


def _return_dist(values: list[float]) -> dict[str, Any]:
    payload = _numeric_dist(values)
    payload["freq_abs_ge_bps"] = {
        str(threshold): sum(1 for value in values if abs(value) >= threshold)
        for threshold in MOVE_BPS
    }
    return payload


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    den_x = sum((x - mean_x) ** 2 for x in xs)
    den_y = sum((y - mean_y) ** 2 for y in ys)
    if den_x <= 0 or den_y <= 0:
        return None
    return num / math.sqrt(den_x * den_y)


def _bps_gap(left: float, right: float) -> float:
    denom = (abs(left) + abs(right)) / 2.0
    if denom <= 0:
        return 0.0
    return (left - right) / denom * 10_000.0


def _has_ambiguous(reasons: dict[str, int]) -> bool:
    for key, count in reasons.items():
        if count <= 0:
            continue
        lower = key.lower()
        if any(token in lower for token in AMBIGUOUS_REASON_TOKENS):
            return True
    return False


def _storage_errors(report: CaptureQualityReport) -> list[str]:
    errors: list[str] = []
    for row in report.sessions:
        end = row.get("end") or {}
        message = end.get("storage_error")
        if message:
            errors.append(str(message))
        if str(end.get("status") or "") == "failed":
            errors.append(f"session {row.get('session_id')} status=failed")
    return errors


def capture_interruptions(path: Path) -> list[dict[str, Any]]:
    """Gaps between consecutive session_end / session_start pairs."""

    events: list[dict[str, Any]] = []
    pending_end: dict[str, Any] | None = None
    for payload in iter_all_mappings(path):
        if not is_session_record(payload):
            continue
        record_type = str(payload.get("record_type") or "")
        if record_type == "session_end":
            pending_end = payload
            if payload.get("storage_error") or payload.get("status") == "failed":
                events.append(
                    {
                        "kind": "storage_failed",
                        "session_id": payload.get("session_id"),
                        "detail": payload.get("storage_error") or payload.get("status"),
                    }
                )
        elif record_type == "session_start" and pending_end is not None:
            ended = _ms(str(pending_end.get("ended_at") or pending_end.get("started_at") or ""))
            started = _ms(str(payload.get("started_at") or ""))
            gap_ms = None
            if ended is not None and started is not None:
                gap_ms = started - ended
            events.append(
                {
                    "kind": "session_boundary",
                    "from_session_id": pending_end.get("session_id"),
                    "to_session_id": payload.get("session_id"),
                    "gap_ms": gap_ms,
                    "cause": "operator_stop_start_or_reload",
                }
            )
            pending_end = None
    return events


def describe_market(path: Path) -> dict[str, Any]:
    """Descriptive movement and quote quality. Not a trading rule."""

    rows: list[dict[str, Any]] = []
    field_status: dict[str, Counter[str]] = defaultdict(Counter)
    spreads: list[float] = []
    change_times: dict[str, list[float]] = defaultdict(list)
    last_change_at: dict[str, float] = {}
    mutation_gaps: list[float] = []
    previous_mono: float | None = None
    previous_trigger: str | None = None
    for payload in iter_all_mappings(path):
        if is_session_record(payload):
            continue
        snap = snapshot_from_mapping(payload)
        rec = observation_from_snapshot(snap)
        stamp = _ms(snap.received_at_local)
        bid = _ok_price(snap.fields.get("bid"))
        ask = _ok_price(snap.fields.get("ask"))
        last = _ok_price(snap.fields.get("last"))
        mark = _ok_price(snap.fields.get("mark"))
        index = _ok_price(snap.fields.get("index"))
        mid = None
        if rec.observation is not None:
            mid = rec.observation.executable_mid()
            last = rec.observation.last if rec.observation.last is not None else last
            mark = rec.observation.mark if rec.observation.mark is not None else mark
            index = rec.observation.index if rec.observation.index is not None else index
        elif bid is not None and ask is not None and ask > bid:
            mid = (bid + ask) / 2.0
        if bid is not None and ask is not None and mid is not None and mid > 0:
            spreads.append((ask - bid) / mid * 10_000.0)
        if stamp is not None:
            rows.append(
                {
                    "t_ms": stamp,
                    "last": last,
                    "mid": mid,
                    "mark": mark,
                    "index": index,
                    "mono": snap.monotonic_ms,
                    "trigger": snap.trigger,
                }
            )
            for name in snap.changed_fields:
                prev = last_change_at.get(name)
                if prev is not None:
                    change_times[name].append(stamp - prev)
                last_change_at[name] = stamp
        monotonic = snap.monotonic_ms
        mutation_emit = (
            monotonic is not None
            and previous_trigger == "mutation"
            and snap.trigger == "mutation"
        )
        if mutation_emit and previous_mono is not None and monotonic is not None:
            mutation_gaps.append(float(monotonic) - float(previous_mono))
        previous_mono = monotonic
        previous_trigger = snap.trigger
        for name, field in snap.fields.items():
            field_status[name][field.parse_status] += 1

    n_rows = len(rows)
    availability: dict[str, dict[str, float | int | None]] = {}
    for name in QUOTE_FIELDS:
        counts = field_status.get(name, Counter())
        n_ok = int(counts.get("ok", 0) + counts.get("ok_redundant", 0))
        availability[name] = {
            "n_ok": n_ok,
            "n_missing": int(counts.get("missing", 0)),
            "n_unparsable": int(counts.get("unparsable", 0)),
            "n_ambiguous": int(counts.get("ambiguous", 0)),
            "ok_pct": (n_ok / n_rows * 100.0) if n_rows else None,
        }

    update_intervals = {
        name: _numeric_dist(values)
        for name, values in sorted(change_times.items())
        if name in QUOTE_FIELDS
    }
    duration_s = None
    if n_rows >= 2:
        duration_s = (rows[-1]["t_ms"] - rows[0]["t_ms"]) / 1000.0
    update_rate_per_s: dict[str, float | None] = {}
    for name in QUOTE_FIELDS:
        n_changes = max(
            0,
            len(change_times.get(name, [])) + (1 if name in last_change_at else 0),
        )
        if duration_s and duration_s > 0:
            update_rate_per_s[name] = n_changes / duration_s
        else:
            update_rate_per_s[name] = None

    gaps = {
        "mid_minus_mark_bps": _numeric_dist(
            [
                _bps_gap(row["mid"], row["mark"])
                for row in rows
                if row["mid"] is not None and row["mark"] is not None
            ]
        ),
        "mid_minus_index_bps": _numeric_dist(
            [
                _bps_gap(row["mid"], row["index"])
                for row in rows
                if row["mid"] is not None and row["index"] is not None
            ]
        ),
        "mark_minus_index_bps": _numeric_dist(
            [
                _bps_gap(row["mark"], row["index"])
                for row in rows
                if row["mark"] is not None and row["index"] is not None
            ]
        ),
    }

    returns: dict[str, dict[str, Any]] = {}
    for horizon in HORIZONS_S:
        horizon_ms = horizon * 1000
        collected: dict[str, list[float]] = {name: [] for name in PRICE_FIELDS}
        cursor = 0
        for index, row in enumerate(rows):
            target = row["t_ms"] + horizon_ms
            if cursor < index:
                cursor = index
            while cursor + 1 < n_rows and rows[cursor]["t_ms"] < target:
                cursor += 1
            if cursor >= n_rows or rows[cursor]["t_ms"] < target:
                continue
            later = rows[cursor]
            for name in PRICE_FIELDS:
                left = row[name]
                right = later[name]
                if left is None or right is None or left <= 0:
                    continue
                collected[name].append((right / left - 1.0) * 10_000.0)
        returns[f"{horizon}s"] = {name: _return_dist(values) for name, values in collected.items()}

    xcorr = _lead_lag_xcorr(rows)
    start_end = None
    if rows:
        start_end = {
            "first_t": datetime.fromtimestamp(rows[0]["t_ms"] / 1000.0, tz=UTC).isoformat(),
            "last_t": datetime.fromtimestamp(rows[-1]["t_ms"] / 1000.0, tz=UTC).isoformat(),
            "first": {name: rows[0][name] for name in PRICE_FIELDS},
            "last": {name: rows[-1][name] for name in PRICE_FIELDS},
        }

    return {
        "n_price_rows": n_rows,
        "start_end": start_end,
        "field_availability": availability,
        "spread_bps": _numeric_dist(spreads),
        "update_interval_ms": update_intervals,
        "update_rate_per_s": update_rate_per_s,
        "mutation_interarrival_ms": _numeric_dist([gap for gap in mutation_gaps if gap >= 0]),
        "mutation_latency_note": (
            "mutation_interarrival_ms is the delay between consecutive mutation "
            "emits (monotonic), not a measured MutationObserver callback latency."
        ),
        "horizon_returns_bps": returns,
        "gaps_bps": gaps,
        "lead_lag_xcorr": xcorr,
        "notes": (
            "Descriptive only. Not a fitted trading rule and not performance evidence.",
            "Do not retune lookbacks, mom/gap, thresholds, target, stops, throttle, or sizing.",
        ),
    }


def _lead_lag_xcorr(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) < 4:
        return {"status": "INSUFFICIENT_SAMPLE", "lags_s": list(XCORR_LAGS_S), "pairs": {}}
    start = int(rows[0]["t_ms"])
    end = int(rows[-1]["t_ms"])
    step = 1000
    grid: list[dict[str, float | None]] = []
    cursor = 0
    t = start
    while t <= end:
        while cursor + 1 < len(rows) and rows[cursor + 1]["t_ms"] <= t:
            cursor += 1
        grid.append({name: rows[cursor][name] for name in PRICE_FIELDS})
        t += step
    rets: dict[str, list[float | None]] = {name: [] for name in PRICE_FIELDS}
    for index in range(1, len(grid)):
        prev = grid[index - 1]
        cur = grid[index]
        for name in PRICE_FIELDS:
            left = prev[name]
            right = cur[name]
            if left is None or right is None or left <= 0:
                rets[name].append(None)
            else:
                rets[name].append((right / left - 1.0) * 10_000.0)
    pairs = (
        ("last", "mark"),
        ("last", "index"),
        ("mark", "index"),
        ("mid", "last"),
        ("mid", "mark"),
    )
    out: dict[str, dict[str, float | None]] = {}
    n = len(next(iter(rets.values())))
    for left_name, right_name in pairs:
        lag_map: dict[str, float | None] = {}
        for lag in XCORR_LAGS_S:
            xs: list[float] = []
            ys: list[float] = []
            for index in range(n):
                j = index + lag
                if j < 0 or j >= n:
                    continue
                x = rets[left_name][index]
                y = rets[right_name][j]
                if x is None or y is None:
                    continue
                xs.append(x)
                ys.append(y)
            lag_map[str(lag)] = _pearson(xs, ys)
        out[f"{left_name}_vs_{right_name}"] = lag_map
    return {
        "status": "DESCRIPTIVE_ONLY",
        "grid_s": 1,
        "n_return_steps": n,
        "lags_s": list(XCORR_LAGS_S),
        "pairs": out,
        "note": "Positive lag k is corr(x[t], y[t+k]). Not a trading signal.",
    }


def _hypothesis_smoke(path: Path) -> dict[str, Any]:
    report = replay_capture_smoke(path, "author_observed_v0", hypothesis_smoke=True)
    return {
        "label": "HYPOTHESIS_SMOKE",
        "not_performance_evidence": True,
        "profile_id": report.profile_id,
        "observations": report.observations,
        "n_candidates": len(report.candidates),
        "n_trades": len(report.trades),
        "n_open": report.n_open,
        "notes": list(report.notes),
        "pipeline_smoke_only": True,
    }


def evaluate_phase_a(
    path: Path | None,
    *,
    min_duration_ms: int = PHASE_A_MIN_DURATION_MS,
    max_duration_ms: int = PHASE_A_MAX_DURATION_MS,
    screenshot_agreement: str = "NOT_VERIFIED",
    restart_attested: bool = False,
    trading_interaction: str = "NONE_IN_CAPTURE_CODE",
) -> dict[str, Any]:
    """Fail closed unless a real unpacked-extension export meets every gate."""

    gates: list[dict[str, Any]] = []
    quality_payload: dict[str, Any] | None = None
    descriptive: dict[str, Any] | None = None
    smoke: dict[str, Any] | None = None
    file_hash = None
    rejected_as_cdp = False

    if path is None or not path.is_file():
        gates.append(_gate("operator_unpacked_extension_export", False, "no NDJSON export present"))
        gates.append(_gate("indexeddb_chunks_written", False, "no export to inspect chunks"))
        gates.append(_gate("sequences_contiguous", False, "no export"))
        gates.append(_gate("stop_start_session_boundaries", False, "no export"))
        gates.append(_gate("committed_data_survives_later_session", False, "no export"))
        gates.append(
            _gate(
                "reload_or_sw_restart_survives",
                False,
                "not attested; no multi-session export",
            )
        )
        gates.append(_gate("export_ordered_ndjson_hash", False, "no export"))
        gates.append(_gate("quality_stream", False, "no export"))
        gates.append(_gate("replay_smoke", False, "no export"))
        gates.append(
            _gate(
                "screenshot_quote_agreement",
                screenshot_agreement == "PASS",
                screenshot_agreement,
            )
        )
        gates.append(_gate("no_storage_errors", False, "no export"))
        gates.append(_gate("no_crossed_book", False, "no export"))
        gates.append(_gate("no_selector_ambiguity", False, "no export"))
        gates.append(
            _gate(
                "no_trading_interaction",
                trading_interaction.startswith("NONE"),
                trading_interaction,
            )
        )
        gates.append(_gate("duration_10_to_15_min", False, "no export"))
    else:
        file_hash = sha256_file(path)
        quality = summarize_capture(path)
        quality_payload = quality_as_dict(quality)
        descriptive = describe_market(path)
        session_ids = [str(row.get("session_id") or "") for row in quality.sessions]
        rejected_as_cdp = (
            HYDRATION_CDP_SESSION_ID in session_ids
            or str(quality.capture_id) == HYDRATION_CDP_SESSION_ID
        )
        export_ok = not rejected_as_cdp
        gates.append(
            _gate(
                "operator_unpacked_extension_export",
                export_ok,
                "hydration CDP sample is not a real extension IndexedDB export"
                if rejected_as_cdp
                else f"file={path.name} sha256={file_hash}",
            )
        )
        chunks_ok = (quality.n_chunks_total or 0) >= 1 or quality.n_raw > 0
        if rejected_as_cdp:
            chunks_ok = False
        gates.append(
            _gate(
                "indexeddb_chunks_written",
                chunks_ok and export_ok,
                f"n_chunks_total={quality.n_chunks_total} n_raw={quality.n_raw}",
            )
        )
        seq_ok = export_ok and not quality.sequence_diagnostics
        gates.append(
            _gate(
                "sequences_contiguous",
                seq_ok,
                "none" if not quality.sequence_diagnostics else str(quality.sequence_diagnostics),
            )
        )
        two_sessions = export_ok and quality.n_sessions >= 2
        gates.append(
            _gate(
                "stop_start_session_boundaries",
                two_sessions,
                f"n_sessions={quality.n_sessions}",
            )
        )
        gates.append(
            _gate(
                "committed_data_survives_later_session",
                two_sessions,
                "export-all must retain the earlier session after a later start",
            )
        )
        gates.append(
            _gate(
                "reload_or_sw_restart_survives",
                export_ok and restart_attested and two_sessions,
                "PASS only with operator reload/SW-restart attestation plus retained prior session",
            )
        )
        gates.append(
            _gate(
                "export_ordered_ndjson_hash",
                export_ok and bool(file_hash),
                file_hash or "missing",
            )
        )
        ambiguous = _has_ambiguous(quality.invalid_reasons)
        quality_ok = (
            export_ok
            and quality.n_raw > 0
            and quality.n_invalid == 0
            and not ambiguous
            and quality.timing_adequacy != "INSUFFICIENT_SAMPLE"
        )
        gates.append(
            _gate(
                "quality_stream",
                quality_ok,
                (
                    f"n_raw={quality.n_raw} n_invalid={quality.n_invalid} "
                    f"timing={quality.timing_adequacy}"
                ),
            )
        )
        try:
            smoke = _hypothesis_smoke(path) if export_ok else None
            replay_ok = bool(smoke and smoke.get("observations", 0) >= 0)
        except Exception as exc:  # noqa: BLE001 - gate must record the failure
            smoke = {"error": str(exc)}
            replay_ok = False
        if rejected_as_cdp:
            replay_ok = False
        replay_detail = "HYPOTHESIS_SMOKE" if replay_ok else "failed or skipped"
        gates.append(_gate("replay_smoke", replay_ok, replay_detail))
        gates.append(
            _gate(
                "screenshot_quote_agreement",
                screenshot_agreement == "PASS",
                screenshot_agreement,
            )
        )
        storage_errors = _storage_errors(quality)
        gates.append(
            _gate(
                "no_storage_errors",
                export_ok and not storage_errors,
                "none" if not storage_errors else ",".join(storage_errors),
            )
        )
        gates.append(
            _gate(
                "no_crossed_book",
                export_ok and quality.n_bid_ge_ask == 0,
                f"n_bid_ge_ask={quality.n_bid_ge_ask}",
            )
        )
        gates.append(
            _gate(
                "no_selector_ambiguity",
                export_ok and not ambiguous,
                str(quality.invalid_reasons) if ambiguous else "none",
            )
        )
        gates.append(
            _gate(
                "no_trading_interaction",
                trading_interaction.startswith("NONE"),
                trading_interaction,
            )
        )
        duration = quality.duration_ms
        duration_ok = (
            export_ok
            and duration is not None
            and min_duration_ms <= float(duration) <= max_duration_ms
        )
        gates.append(
            _gate(
                "duration_10_to_15_min",
                duration_ok,
                f"duration_ms={duration} window=[{min_duration_ms},{max_duration_ms}]",
            )
        )

    passed = all(gate["ok"] for gate in gates)
    return {
        "STATUS": "PASS" if passed else "FAIL",
        "pass": passed,
        "gates": gates,
        "file_sha256": file_hash,
        "rejected_hydration_cdp_as_phase_a": rejected_as_cdp,
        "quality": quality_payload,
        "descriptive_market": descriptive,
        "hypothesis_smoke": smoke,
        "interruptions": capture_interruptions(path) if path is not None and path.is_file() else [],
        "notes": [
            "Phase A requires the unpacked MV3 extension, IndexedDB chunks, and operator export.",
            "The Cursor IDE browser cannot load unpacked MV3; Python must not drive Chrome.",
            "Hydration-gate CDP NDJSON is not Phase A evidence.",
        ],
    }


def evaluate_phase_b(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {
            "STATUS": "NOT_STARTED",
            "reason": "Phase A did not pass; the 8-12h TAOUSDT session was not started.",
        }
    quality = summarize_capture(path)
    duration = quality.duration_ms or 0.0
    in_window = PHASE_B_MIN_DURATION_MS <= duration <= PHASE_B_MAX_DURATION_MS
    return {
        "STATUS": "READY_FOR_REVIEW" if in_window else "OUTSIDE_DURATION_WINDOW",
        "duration_ms": duration,
        "quality": quality_as_dict(quality),
        "descriptive_market": describe_market(path),
        "interruptions": capture_interruptions(path),
        "file_sha256": sha256_file(path),
        "hypothesis_smoke": _hypothesis_smoke(path),
        "notes": [
            "HYPOTHESIS_SMOKE is diagnostic only.",
            "Quiet periods are retained; no strategy retune.",
        ],
    }


def build_milestone_report(
    phase_a_path: Path | None,
    *,
    phase_b_path: Path | None = None,
    screenshot_agreement: str = "NOT_VERIFIED",
    restart_attested: bool = False,
    min_duration_ms: int = PHASE_A_MIN_DURATION_MS,
    max_duration_ms: int = PHASE_A_MAX_DURATION_MS,
) -> dict[str, Any]:
    phase_a = evaluate_phase_a(
        phase_a_path,
        min_duration_ms=min_duration_ms,
        max_duration_ms=max_duration_ms,
        screenshot_agreement=screenshot_agreement,
        restart_attested=restart_attested,
    )
    if not phase_a["pass"]:
        return {
            "STATUS": "MEXC_UI_EXTENSION_E2E_AND_LONG_CAPTURE_PHASE_A_BLOCKED",
            "DECISION": "STOP_FOR_LEAD_REVIEW",
            "ML_STATUS": "NOT_STARTED",
            "PAPER": False,
            "LIVE": False,
            "STRATEGY_TUNING": False,
            "profiles_frozen": ["author_observed_v0", "conservative_v0"],
            "phase_a": phase_a,
            "phase_b": {
                "STATUS": "NOT_STARTED",
                "reason": "Phase A gate failed; do not start the 8-12h observation.",
            },
        }
    return {
        "STATUS": "MEXC_UI_EXTENSION_E2E_AND_LONG_CAPTURE_PHASE_B_READY",
        "DECISION": "STOP_FOR_LEAD_REVIEW" if phase_b_path is None else "STOP_FOR_LEAD_REVIEW",
        "ML_STATUS": "NOT_STARTED",
        "PAPER": False,
        "LIVE": False,
        "STRATEGY_TUNING": False,
        "profiles_frozen": ["author_observed_v0", "conservative_v0"],
        "phase_a": phase_a,
        "phase_b": evaluate_phase_b(phase_b_path),
    }
