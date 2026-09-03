"""Long TAOUSDT observation: quality, warmup vs DATA_INVALID, descriptive stats.

Does not retune frozen profiles. Does not forward-fill executable BBO.
Invalid rows stay in the raw NDJSON; classification is analysis-only.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_bot.research.mexc_shadow.ui_capture.durable import is_session_record
from trading_bot.research.mexc_shadow.ui_capture.long_report import (
    HORIZONS_S,
    PHASE_B_MAX_DURATION_MS,
    PHASE_B_MIN_DURATION_MS,
    PRICE_FIELDS,
    QUOTE_FIELDS,
    _bps_gap,
    _lead_lag_xcorr,
    _numeric_dist,
    _return_dist,
    sha256_file,
)
from trading_bot.research.mexc_shadow.ui_capture.normalize import (
    observation_from_snapshot,
    snapshot_from_mapping,
)
from trading_bot.research.mexc_shadow.ui_capture.quality import (
    _ok_price,
    _percentile,
    _timing_adequacy,
)
from trading_bot.research.mexc_shadow.ui_capture.replay import replay_capture_smoke
from trading_bot.research.mexc_shadow.ui_capture.store import iter_all_mappings

CLASS_READY_VALID = "READY_VALID"
CLASS_STARTUP_WARMUP = "STARTUP_WARMUP"
CLASS_DATA_INVALID = "DATA_INVALID"

# Catalog required_for_valid: symbol, bid, ask. Readiness is the first snapshot
# in a session that normalizes to an executable Observation (no forward-fill).
REQUIRED_READY_FIELDS = ("symbol", "bid", "ask")


def classify_observation_row(*, session_ready: bool, observation_ok: bool) -> str:
    """Map one snapshot onto warmup / ready-valid / post-ready invalid."""

    if observation_ok:
        return CLASS_READY_VALID
    if not session_ready:
        return CLASS_STARTUP_WARMUP
    return CLASS_DATA_INVALID


def _ms(stamp: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() * 1000.0


def _iso(t_ms: float | None) -> str | None:
    if t_ms is None:
        return None
    return datetime.fromtimestamp(t_ms / 1000.0, tz=UTC).isoformat()


def _close_burst(
    current: dict[str, Any] | None,
    *,
    end_seq: int,
    end_t_ms: float | None,
) -> dict[str, Any] | None:
    if current is None:
        return None
    current["end_seq"] = end_seq
    current["end_t"] = _iso(end_t_ms)
    start_t = current.get("start_t_ms")
    current["duration_ms"] = (
        None if start_t is None or end_t_ms is None else end_t_ms - start_t
    )
    current.pop("start_t_ms", None)
    return current


def scan_long_capture(path: Path) -> dict[str, Any]:
    """One streaming pass. Raw rows are never rewritten."""

    class_counts: Counter[str] = Counter()
    invalid_reasons: Counter[str] = Counter()
    warmup_reasons: Counter[str] = Counter()
    data_invalid_reasons: Counter[str] = Counter()
    triggers: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    bbo_source: Counter[str] = Counter()
    ambiguity: Counter[str] = Counter()
    heading_counts: Counter[int] = Counter()
    visible_heading_counts: Counter[int] = Counter()
    changes: Counter[str] = Counter()
    ages: dict[str, list[float]] = defaultdict(list)
    arrivals: list[float] = []
    rows: list[dict[str, Any]] = []
    spreads: list[float] = []
    change_times: dict[str, list[float]] = defaultdict(list)
    last_change_at: dict[str, float] = {}
    mutation_gaps: list[float] = []
    canonical: list[str] = []
    session_rows: list[dict[str, Any]] = []
    seq_diag: list[dict[str, Any]] = []
    seq_prev: dict[str, int | None] = {}
    seq_seen: dict[str, set[int]] = defaultdict(set)
    session_ready: dict[str, bool] = {}
    bursts: list[dict[str, Any]] = []
    open_burst: dict[str, Any] | None = None
    n_raw = 0
    n_valid = 0
    n_simultaneous_four = 0
    n_simultaneous_five = 0
    n_bid_ge_ask = 0
    coexist_valid = 0
    previous_mono: float | None = None
    previous_trigger: str | None = None
    previous_capture: str | None = None
    first_ready: dict[str, Any] | None = None
    field_ok_counts: Counter[str] = Counter()
    pending_end: dict[str, Any] | None = None
    interruptions: list[dict[str, Any]] = []

    def note_seq(session_id: str, seq: int, index: int) -> None:
        seen = seq_seen[session_id]
        if seq in seen:
            seq_diag.append(
                {
                    "kind": "duplicate_sequence",
                    "sequence": seq,
                    "index": index,
                    "session_id": session_id,
                }
            )
        seen.add(seq)
        previous = seq_prev.get(session_id)
        if previous is not None and seq != previous + 1:
            seq_diag.append(
                {
                    "kind": "gap",
                    "expected": previous + 1,
                    "got": seq,
                    "index": index,
                    "session_id": session_id,
                }
            )
        seq_prev[session_id] = seq

    def attach_session_end(payload: dict[str, Any]) -> None:
        session_id = str(payload.get("session_id") or "")
        for row in reversed(session_rows):
            if row.get("session_id") == session_id and row.get("end") is None:
                row["end"] = payload
                return
        session_rows.append({"session_id": session_id or None, "start": None, "end": payload})

    for payload in iter_all_mappings(path):
        if is_session_record(payload):
            record_type = str(payload.get("record_type") or "")
            if record_type == "session_start":
                session_id = str(payload.get("session_id") or "")
                session_rows.append(
                    {"session_id": payload.get("session_id"), "start": payload, "end": None}
                )
                # Reload / new session: warmup clock starts again.
                session_ready[session_id] = False
                if pending_end is not None:
                    ended = _ms(
                        str(pending_end.get("ended_at") or pending_end.get("started_at") or "")
                    )
                    started = _ms(str(payload.get("started_at") or ""))
                    gap_ms = None
                    if ended is not None and started is not None:
                        gap_ms = started - ended
                    interruptions.append(
                        {
                            "kind": "session_boundary",
                            "from_session_id": pending_end.get("session_id"),
                            "to_session_id": payload.get("session_id"),
                            "gap_ms": gap_ms,
                            "cause": "operator_stop_start_or_reload",
                        }
                    )
                    pending_end = None
            elif record_type == "session_end":
                pending_end = payload
                if payload.get("storage_error") or payload.get("status") == "failed":
                    interruptions.append(
                        {
                            "kind": "storage_failed",
                            "session_id": payload.get("session_id"),
                            "detail": payload.get("storage_error") or payload.get("status"),
                        }
                    )
                attach_session_end(payload)
            continue

        n_raw += 1
        snap = snapshot_from_mapping(payload)
        session_id = str(snap.capture_id or "_unknown")
        if previous_capture is not None and snap.capture_id != previous_capture:
            session_ready[session_id] = False
        previous_capture = snap.capture_id
        note_seq(session_id, snap.sequence, n_raw - 1)
        triggers[str(snap.trigger)] += 1
        rec = observation_from_snapshot(snap)
        observation_ok = rec.observation is not None
        ready = session_ready.get(session_id, False)
        klass = classify_observation_row(
            session_ready=ready, observation_ok=observation_ok
        )
        if observation_ok:
            session_ready[session_id] = True
            n_valid += 1
            obs = rec.observation
            assert obs is not None
            if first_ready is None:
                first_ready = {
                    "session_id": session_id,
                    "sequence": snap.sequence,
                    "received_at_local": snap.received_at_local,
                }
            if obs.mark is not None and obs.index is not None:
                coexist_valid += 1
            canonical.append(
                json.dumps(
                    {
                        "seq": snap.sequence,
                        "symbol": obs.symbol,
                        "bid": obs.bid,
                        "ask": obs.ask,
                        "last": obs.last,
                        "mark": obs.mark,
                        "index": obs.index,
                        "received_at": obs.received_at.isoformat(),
                    },
                    sort_keys=True,
                )
            )
        else:
            reason = rec.skipped_reason or "unknown"
            invalid_reasons[reason] += 1
            for item in snap.invalid_reasons:
                invalid_reasons[item] += 1
            bucket = warmup_reasons if klass == CLASS_STARTUP_WARMUP else data_invalid_reasons
            bucket[reason] += 1
            for item in snap.invalid_reasons:
                bucket[item] += 1
        class_counts[klass] += 1

        stamp = _ms(snap.received_at_local)
        if stamp is not None:
            arrivals.append(stamp)
        bid = _ok_price(snap.fields.get("bid"))
        ask = _ok_price(snap.fields.get("ask"))
        last = _ok_price(snap.fields.get("last"))
        mark = _ok_price(snap.fields.get("mark"))
        index = _ok_price(snap.fields.get("index"))
        if rec.observation is not None:
            last = rec.observation.last if rec.observation.last is not None else last
            mark = rec.observation.mark if rec.observation.mark is not None else mark
            index = rec.observation.index if rec.observation.index is not None else index
        # Executable mid only from a ready valid observation. Never carry the
        # previous mid onto a missing/corrupt BBO row.
        mid = None
        if rec.observation is not None:
            mid = rec.observation.executable_mid()
        if bid is not None:
            field_ok_counts["bid"] += 1
        if ask is not None:
            field_ok_counts["ask"] += 1
        if last is not None:
            field_ok_counts["last"] += 1
        if mark is not None:
            field_ok_counts["mark"] += 1
        if index is not None:
            field_ok_counts["index"] += 1
        if bid is not None and ask is not None and bid >= ask:
            n_bid_ge_ask += 1
        if bid is not None and ask is not None and mark is not None and index is not None:
            n_simultaneous_four += 1
        if (
            bid is not None
            and ask is not None
            and last is not None
            and mark is not None
            and index is not None
        ):
            n_simultaneous_five += 1
        if klass == CLASS_READY_VALID and mid is not None and bid is not None and ask is not None:
            spreads.append((ask - bid) / mid * 10_000.0)
        diag = snap.orderbook_diagnostics
        bbo_source[str(diag.get("chosen_bbo_source") or "none")] += 1
        reason_text = diag.get("ambiguity_reason")
        if reason_text:
            ambiguity[str(reason_text)] += 1
        heading_counts[int(diag.get("orderbook_heading_count") or 0)] += 1
        visible_heading_counts[int(diag.get("visible_orderbook_heading_count") or 0)] += 1
        if stamp is not None:
            rows.append(
                {
                    "t_ms": stamp,
                    "last": last,
                    "mid": mid,
                    "mark": mark,
                    "index": index,
                    "klass": klass,
                    "seq": snap.sequence,
                }
            )
            for name in snap.changed_fields:
                prev = last_change_at.get(name)
                if prev is not None:
                    change_times[name].append(stamp - prev)
                last_change_at[name] = stamp
        if klass in {CLASS_STARTUP_WARMUP, CLASS_DATA_INVALID}:
            if open_burst is None or open_burst["class"] != klass:
                closed = _close_burst(open_burst, end_seq=snap.sequence, end_t_ms=stamp)
                if closed is not None:
                    bursts.append(closed)
                open_burst = {
                    "class": klass,
                    "n": 1,
                    "start_seq": snap.sequence,
                    "start_t": _iso(stamp),
                    "start_t_ms": stamp,
                    "session_id": session_id,
                }
            else:
                open_burst["n"] += 1
        elif open_burst is not None:
            closed = _close_burst(open_burst, end_seq=snap.sequence, end_t_ms=stamp)
            if closed is not None:
                bursts.append(closed)
            open_burst = None
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
        for name in snap.changed_fields:
            changes[name] += 1
        for name, field in snap.fields.items():
            statuses[field.parse_status] += 1
            if field.parse_status == "missing":
                missing[name] += 1
            if field.age_ms is not None:
                ages[name].append(float(field.age_ms))

    if open_burst is not None and rows:
        closed = _close_burst(
            open_burst, end_seq=int(rows[-1]["seq"]), end_t_ms=float(rows[-1]["t_ms"])
        )
        if closed is not None:
            bursts.append(closed)

    for row in session_rows:
        end = row.get("end") or {}
        sid = row.get("session_id")
        seq_diag.extend(
            {**item, "source": "session_end", "session_id": sid}
            for item in (end.get("sequence_gaps") or [])
        )
        seq_diag.extend(
            {**item, "source": "client_sequence", "session_id": sid}
            for item in (end.get("client_sequence_mismatches") or [])
        )

    deltas = [arrivals[i] - arrivals[i - 1] for i in range(1, len(arrivals))]
    deltas.sort()
    if not deltas:
        interarrival: dict[str, float | int | None] = {
            "n": 0,
            "p50_ms": None,
            "p90_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "min_ms": None,
            "max_ms": None,
        }
    else:
        interarrival = {
            "n": len(deltas),
            "p50_ms": _percentile(deltas, 0.50),
            "p90_ms": _percentile(deltas, 0.90),
            "p95_ms": _percentile(deltas, 0.95),
            "p99_ms": _percentile(deltas, 0.99),
            "min_ms": deltas[0],
            "max_ms": deltas[-1],
        }
    duration_ms = None
    if len(arrivals) >= 2:
        duration_ms = arrivals[-1] - arrivals[0]
    n_chunks_total = 0
    storage_errors: list[str] = []
    compact_sessions: list[dict[str, Any]] = []
    for row in session_rows:
        end = row.get("end") or {}
        start = row.get("start") or {}
        chunks = end.get("n_chunks")
        if chunks is None:
            chunks = start.get("n_chunks")
        if chunks is not None:
            n_chunks_total += int(chunks)
        message = end.get("storage_error")
        if message:
            storage_errors.append(str(message))
        if str(end.get("status") or "") == "failed":
            storage_errors.append(f"session {row.get('session_id')} status=failed")
        compact_sessions.append(
            {
                "session_id": row.get("session_id"),
                "started_at": start.get("started_at"),
                "ended_at": end.get("ended_at"),
                "status": end.get("status") or start.get("status"),
                "page_host": start.get("page_host") or end.get("page_host"),
                "page_path": start.get("page_path") or end.get("page_path"),
                "interval_ms": start.get("interval_ms") or end.get("interval_ms"),
                "n_snapshots": end.get("n_snapshots"),
                "n_chunks": end.get("n_chunks"),
                "first_sequence": end.get("first_sequence"),
                "last_sequence": end.get("last_sequence"),
                "storage_error": end.get("storage_error"),
                "sequence_gaps": end.get("sequence_gaps") or [],
                "client_sequence_mismatches": end.get("client_sequence_mismatches") or [],
            }
        )

    n_rows = len(rows)
    availability: dict[str, dict[str, float | int | None]] = {}
    for name in QUOTE_FIELDS:
        n_ok = int(field_ok_counts.get(name, 0))
        n_missing = int(missing.get(name, 0))
        availability[name] = {
            "n_ok": n_ok,
            "n_missing": n_missing,
            "ok_pct": (n_ok / n_raw * 100.0) if n_raw else None,
        }

    duration_s = None if duration_ms is None else duration_ms / 1000.0
    update_rate_per_s: dict[str, float | None] = {}
    update_intervals = {
        name: _numeric_dist(values)
        for name, values in sorted(change_times.items())
        if name in QUOTE_FIELDS
    }
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

    digest = hashlib.sha256("\n".join(canonical).encode("utf-8")).hexdigest() if canonical else None
    warmup_n = int(class_counts.get(CLASS_STARTUP_WARMUP, 0))
    data_invalid_n = int(class_counts.get(CLASS_DATA_INVALID, 0))
    ready_n = int(class_counts.get(CLASS_READY_VALID, 0))
    in_window = (
        duration_ms is not None
        and PHASE_B_MIN_DURATION_MS <= float(duration_ms) <= PHASE_B_MAX_DURATION_MS
    )
    return {
        "n_raw": n_raw,
        "n_valid_for_replay": n_valid,
        "n_invalid": n_raw - n_valid,
        "n_startup_warmup": warmup_n,
        "n_data_invalid": data_invalid_n,
        "n_ready_valid": ready_n,
        "class_counts": dict(class_counts),
        "invalid_reasons": dict(invalid_reasons),
        "startup_warmup_reasons": dict(warmup_reasons),
        "data_invalid_reasons": dict(data_invalid_reasons),
        "missingness": dict(missing),
        "parse_status_counts": dict(statuses),
        "interarrival_ms": interarrival,
        "field_age_ms": {name: _age_stats(values) for name, values in sorted(ages.items())},
        "field_change_counts": dict(changes),
        "field_change_rate": {name: count / n_raw for name, count in changes.items()}
        if n_raw
        else {},
        "field_availability": availability,
        "update_interval_ms": update_intervals,
        "update_rate_per_s": update_rate_per_s,
        "spread_bps": _numeric_dist(spreads),
        "gaps_bps": gaps,
        "horizon_returns_bps": returns,
        "lead_lag_xcorr": _lead_lag_xcorr(rows),
        "mutation_interarrival_ms": _numeric_dist([gap for gap in mutation_gaps if gap >= 0]),
        "n_bid_ge_ask": n_bid_ge_ask,
        "n_simultaneous_bid_ask_mark_index": n_simultaneous_four,
        "n_simultaneous_bid_ask_last_mark_index": n_simultaneous_five,
        "coexistence_bid_ask_mark_index": coexist_valid,
        "selector_diagnostics": {
            "chosen_bbo_source": dict(bbo_source),
            "ambiguity_reason": dict(ambiguity),
            "orderbook_heading_count": {str(k): v for k, v in sorted(heading_counts.items())},
            "visible_orderbook_heading_count": {
                str(k): v for k, v in sorted(visible_heading_counts.items())
            },
        },
        "warmup_and_invalid_bursts": bursts,
        "first_ready": first_ready,
        "required_ready_fields": list(REQUIRED_READY_FIELDS),
        "sequence_diagnostics": seq_diag,
        "sessions": compact_sessions,
        "n_sessions": len(session_rows),
        "n_chunks_total": n_chunks_total,
        "storage_errors": storage_errors,
        "duration_ms": duration_ms,
        "duration_hours": None if duration_ms is None else duration_ms / 3_600_000.0,
        "in_8_to_12h_window": in_window,
        "interruptions": interruptions,
        "trigger_counts": dict(triggers),
        "replay_determinism_sha256": digest,
        "timing_adequacy": _timing_adequacy(
            n_raw=n_raw,
            interarrival=interarrival,
            n_simultaneous=n_simultaneous_four,
            n_valid=n_valid,
        ),
        "start_end": None
        if not rows
        else {
            "first_t": _iso(rows[0]["t_ms"]),
            "last_t": _iso(rows[-1]["t_ms"]),
            "first": {name: rows[0][name] for name in PRICE_FIELDS},
            "last": {name: rows[-1][name] for name in PRICE_FIELDS},
        },
        "notes": (
            "STARTUP_WARMUP is pre-readiness invalid after session start/reload; "
            "not a selector fail.",
            "DATA_INVALID is missing/corrupt executable BBO after readiness. Never forward-filled.",
            "Raw NDJSON is append-only; classification does not rewrite capture bytes.",
            "Descriptive returns/xcorr are not a trading rule.",
        ),
        "_series_mids_for_tests": [row["mid"] for row in rows],
    }


def _age_stats(values: list[float]) -> dict[str, float | int | None]:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "min_ms": ordered[0] if ordered else None,
        "p50_ms": _percentile(ordered, 0.50),
        "p90_ms": _percentile(ordered, 0.90),
        "p95_ms": _percentile(ordered, 0.95),
        "p99_ms": _percentile(ordered, 0.99),
        "max_ms": ordered[-1] if ordered else None,
    }


def _hypothesis_smoke_summary(path: Path) -> dict[str, Any]:
    report = replay_capture_smoke(path, "author_observed_v0", hypothesis_smoke=True)
    throttle = Counter(candidate.throttle for candidate in report.candidates)
    exits = Counter(trade.exit_reason for trade in report.trades)
    gross = [float(trade.gross_bps) for trade in report.trades]
    return {
        "label": "HYPOTHESIS_SMOKE",
        "not_performance_evidence": True,
        "not_strategy_evidence": True,
        "do_not_retune": True,
        "profile_id": report.profile_id,
        "observations": report.observations,
        "n_candidates": len(report.candidates),
        "n_accepted_for_shadow": sum(
            1 for candidate in report.candidates if candidate.accepted_for_shadow
        ),
        "n_trades": len(report.trades),
        "n_open": report.n_open,
        "throttle_counts": dict(throttle),
        "exit_reason_counts": dict(exits),
        "trade_gross_bps": _numeric_dist(gross),
        "cost_summaries": report.cost_summaries,
        "notes": list(report.notes),
        "pipeline_smoke_only": True,
    }


def build_long_observation_report(path: Path) -> dict[str, Any]:
    """Versioned milestone payload. Profiles stay frozen."""

    file_hash = sha256_file(path)
    scan = scan_long_capture(path)
    series_mids = scan.pop("_series_mids_for_tests")
    try:
        smoke = _hypothesis_smoke_summary(path)
        replay_ok = True
        replay_error = None
    except Exception as exc:  # noqa: BLE001 - report must still ship
        smoke = {"label": "HYPOTHESIS_SMOKE", "error": str(exc)}
        replay_ok = False
        replay_error = str(exc)
    duration_ms = scan["duration_ms"]
    in_window = bool(scan["in_8_to_12h_window"])
    seq_ok = not scan["sequence_diagnostics"]
    storage_ok = not scan["storage_errors"]
    data_invalid_n = int(scan["n_data_invalid"])
    availability = scan.get("field_availability") or {}
    mark_ok = float((availability.get("mark") or {}).get("ok_pct") or 0.0)
    index_ok = float((availability.get("index") or {}).get("ok_pct") or 0.0)
    thin_ref = mark_ok < 50.0 or index_ok < 50.0
    if scan["n_raw"] <= 0:
        status = "MEXC_TAO_LONG_OBSERVATION_BLOCKED"
    elif in_window and seq_ok and storage_ok and data_invalid_n == 0 and not thin_ref:
        status = "MEXC_TAO_LONG_OBSERVATION_READY"
    else:
        status = "MEXC_TAO_LONG_OBSERVATION_READY_WITH_FINDINGS"
    payload = {
        "STATUS": status,
        "DECISION": "STOP_FOR_LEAD_REVIEW",
        "ML_STATUS": "NOT_STARTED",
        "PAPER": False,
        "LIVE": False,
        "STRATEGY_TUNING": False,
        "profiles_frozen": ["author_observed_v0", "conservative_v0"],
        "milestone": "MEXC_TAO_LONG_OBSERVATION_V1",
        "prior_milestone": "MEXC_UI_EXTENSION_E2E_AND_LONG_CAPTURE_V1",
        "raw_rewritten": False,
        "bbo_forward_filled": False,
        "file": {
            "path": _repo_relative(path),
            "name": path.name,
            "sha256": file_hash,
            "bytes": path.stat().st_size,
        },
        "capture_quality": {
            k: v
            for k, v in scan.items()
            if k
            not in {
                "horizon_returns_bps",
                "lead_lag_xcorr",
                "gaps_bps",
                "spread_bps",
                "start_end",
                "update_interval_ms",
                "update_rate_per_s",
                "mutation_interarrival_ms",
                "field_availability",
                "interruptions",
            }
        },
        "descriptive_market": {
            "start_end": scan["start_end"],
            "spread_bps": scan["spread_bps"],
            "gaps_bps": scan["gaps_bps"],
            "horizon_returns_bps": scan["horizon_returns_bps"],
            "lead_lag_xcorr": scan["lead_lag_xcorr"],
            "field_availability": scan["field_availability"],
            "update_interval_ms": scan["update_interval_ms"],
            "update_rate_per_s": scan["update_rate_per_s"],
            "mutation_interarrival_ms": scan["mutation_interarrival_ms"],
            "notes": [
                "Descriptive only. Not a fitted trading rule and not performance evidence.",
                "Do not retune lookbacks, mom/gap, thresholds, target, stops, throttle, or sizing.",
                "Executable mid is omitted (None) on STARTUP_WARMUP and DATA_INVALID rows.",
            ],
        },
        "hypothesis_smoke": smoke,
        "interruptions": scan["interruptions"],
        "replay_ok": replay_ok,
        "replay_error": replay_error,
        "duration_window_ms": [PHASE_B_MIN_DURATION_MS, PHASE_B_MAX_DURATION_MS],
        "duration_ms": duration_ms,
        "in_8_to_12h_window": in_window,
        "_series_mids_for_tests": series_mids,
        "findings": _findings(scan, smoke),
    }
    return payload


def _repo_relative(path: Path) -> str:
    repo = Path(__file__).resolve().parents[5]
    try:
        return str(path.resolve().relative_to(repo)).replace("\\", "/")
    except ValueError:
        return path.name


def _findings(scan: dict[str, Any], smoke: dict[str, Any]) -> list[str]:
    n_raw = int(scan.get("n_raw") or 0)
    missing = scan.get("missingness") or {}
    notes: list[str] = []
    if int(missing.get("symbol") or 0) == n_raw and n_raw:
        notes.append(
            "Raw symbol field is missing on every snapshot. Replay identity was "
            "recovered from page_path /ru-RU/futures/TAO_USDT. NDJSON was not rewritten."
        )
    if int(missing.get("mark") or 0) == n_raw and n_raw:
        notes.append(
            "mark (Fair Price) is missing on every snapshot. English header labels "
            "likely did not match the ru-RU UI. mid-mark / mark-index stats are empty."
        )
    if int(missing.get("index") or 0) == n_raw and n_raw:
        notes.append(
            "index is missing on every snapshot. Same locale-label issue as mark."
        )
    if (scan.get("selector_diagnostics") or {}).get("orderbook_heading_count") == {"0": n_raw}:
        notes.append(
            "orderbook_heading_count=0 on every row; executable BBO is 100% "
            "live_asks_bids_wrapper."
        )
    if smoke.get("n_candidates") == 0 and n_raw:
        notes.append(
            "HYPOTHESIS_SMOKE produced 0 candidates and 0 trades. Frozen "
            "author_observed_v0 gap is mid_vs_mark and mark is absent. This is "
            "not a reason to retune mom/gap/exits."
        )
    first_mid = ((scan.get("start_end") or {}).get("first") or {}).get("mid")
    if isinstance(first_mid, int | float) and first_mid > 1000:
        notes.append(
            "Parsed last/mid sit near 21811–21875 while Phase A TAO was ~216. "
            "Wrapper bid/ask raw_text has no decimal; last uses a comma. bps stats "
            "are reported in native parsed units without rescaling."
        )
    return notes


def _fmt_pct(n: int, d: int) -> str:
    if d <= 0:
        return "n/a"
    return f"{n / d * 100.0:.4f}%"


def _fmt_dist(dist: dict[str, Any] | None, digits: int = 4) -> str:
    if not dist or not dist.get("n"):
        return "n=0"
    parts = [f"n={dist['n']}"]
    aliases = (
        ("mean", "mean"),
        ("std", "std"),
        ("p50", "p50"),
        ("p50_ms", "p50"),
        ("p90", "p90"),
        ("p90_ms", "p90"),
        ("p95", "p95"),
        ("p95_ms", "p95"),
        ("p99", "p99"),
        ("p99_ms", "p99"),
        ("min", "min"),
        ("min_ms", "min"),
        ("max", "max"),
        ("max_ms", "max"),
    )
    seen: set[str] = set()
    for src, label in aliases:
        if label in seen:
            continue
        value = dist.get(src)
        if value is None:
            continue
        seen.add(label)
        parts.append(f"{label}={value:.{digits}f}")
    return ", ".join(parts)


def render_long_observation_markdown(report: dict[str, Any]) -> str:
    """Human-readable lead report. Numbers come from the JSON payload."""

    q = report["capture_quality"]
    m = report["descriptive_market"]
    smoke = report.get("hypothesis_smoke") or {}
    file_info = report["file"]
    n_raw = int(q.get("n_raw") or 0)
    warmup = int(q.get("n_startup_warmup") or 0)
    invalid = int(q.get("n_data_invalid") or 0)
    ready = int(q.get("n_ready_valid") or q.get("n_valid_for_replay") or 0)
    hours = report.get("duration_ms")
    hours_s = "n/a" if hours is None else f"{float(hours) / 3_600_000.0:.4f}"
    bursts = q.get("warmup_and_invalid_bursts") or []
    burst_lines = []
    for burst in bursts:
        burst_lines.append(
            f"| `{burst.get('class')}` | {burst.get('n')} | "
            f"{burst.get('start_seq')}–{burst.get('end_seq')} | "
            f"{burst.get('duration_ms')} | {burst.get('start_t')} |"
        )
    if not burst_lines:
        burst_lines = ["| (none) | 0 | — | — | — |"]
    xcorr = m.get("lead_lag_xcorr") or {}
    xcorr_rows = []
    for pair, lags in (xcorr.get("pairs") or {}).items():
        peak_lag = None
        peak_val = None
        for lag, value in lags.items():
            if value is None:
                continue
            if peak_val is None or abs(value) > abs(peak_val):
                peak_lag = lag
                peak_val = value
        xcorr_rows.append(
            f"| `{pair}` | {peak_lag} | "
            f"{'n/a' if peak_val is None else f'{peak_val:.4f}'} |"
        )
    if not xcorr_rows:
        xcorr_rows = ["| (insufficient) | — | — |"]
    ret_lines = []
    for horizon in ("1s", "2s", "5s", "10s", "30s", "60s"):
        block = (m.get("horizon_returns_bps") or {}).get(horizon) or {}
        cells = [horizon]
        for name in PRICE_FIELDS:
            dist = block.get(name) or {}
            n = dist.get("n") or 0
            p50 = dist.get("p50")
            freq = dist.get("freq_abs_ge_bps") or {}
            cells.append(
                "n={} p50={} ≥1/2/3/5/10bps={}/{}/{}/{}/{}".format(
                    n,
                    "n/a" if p50 is None else f"{p50:.3f}",
                    freq.get("1", 0),
                    freq.get("2", 0),
                    freq.get("3", 0),
                    freq.get("5", 0),
                    freq.get("10", 0),
                )
            )
        ret_lines.append("| " + " | ".join(cells) + " |")
    sessions = q.get("sessions") or []
    session_lines = []
    for row in sessions:
        session_lines.append(
            f"| `{row.get('session_id')}` | {row.get('started_at')} | "
            f"{row.get('ended_at')} | {row.get('n_snapshots')} | "
            f"{row.get('n_chunks')} | {row.get('status')} | {row.get('storage_error')} |"
        )
    if not session_lines:
        session_lines = ["| (none) | — | — | — | — | — | — |"]
    interruptions = report.get("interruptions") or []
    inter_lines = (
        [
            f"| {item.get('kind')} | {item.get('from_session_id')} | "
            f"{item.get('to_session_id')} | {item.get('gap_ms')} |"
            for item in interruptions
        ]
        or ["| none | — | — | — |"]
    )
    bbo = (q.get("selector_diagnostics") or {}).get("chosen_bbo_source") or {}
    bbo_txt = ", ".join(f"{k}={v}" for k, v in bbo.items()) or "none"
    smoke_note = (
        "Candidate/trade counts and any PnL figures are **HYPOTHESIS_SMOKE** "
        "only. They are not strategy evidence and must not be used to retune "
        "mom/gap/exit/threshold/throttle/sizing."
    )
    findings = report.get("findings") or []
    finding_lines = "\n".join(f"- {item}" for item in findings) or "- none"
    return f"""# MEXC TAO long observation v1

STATUS: `{report["STATUS"]}`

DECISION: `STOP_FOR_LEAD_REVIEW`

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false** (frozen `author_observed_v0` / `conservative_v0`)

Prior milestone: `docs/mexc_ui_extension_e2e_and_long_capture_v1.md`

## Purpose

Describe the real 8–12 hour unpacked-extension `TAOUSDT` capture. Profiles
stay frozen. No ML, PAPER, live execution, or mom/gap/exit retune.

Invalid observations **before** the first executable market-data print after
each session start/reload are `STARTUP_WARMUP`, not selector failure. They
remain in the raw NDJSON. Readiness begins only when catalog-required fields
(symbol, bid, ask) normalize to a valid Observation. Any missing or corrupt
executable BBO **after** readiness is `DATA_INVALID` and is **not**
forward-filled.

## Capture file

- path: `{file_info.get("path")}`
- name: `{file_info.get("name")}`
- sha256: `{file_info.get("sha256")}`
- bytes: {file_info.get("bytes")}
- duration hours: {hours_s}
- 8–12h window (8h–12h30m): **{scan_window(report)}**

Raw capture is gitignored under `data/mexc_ui_capture/`. This report is the
lead artifact.

## Decision

**STOP_FOR_LEAD_REVIEW.** Do not start ML or PAPER. Do not retune frozen
profiles from this session. `STARTUP_WARMUP` is pre-readiness invalid, not a
selector fail. `DATA_INVALID` after readiness is not forward-filled.

### Findings

{finding_lines}

## Capture quality

| Metric | Value |
| --- | --- |
| snapshots | {n_raw} |
| READY_VALID | {ready} ({_fmt_pct(ready, n_raw)}) |
| STARTUP_WARMUP | {warmup} ({_fmt_pct(warmup, n_raw)}) |
| DATA_INVALID | {invalid} ({_fmt_pct(invalid, n_raw)}) |
| sessions | {q.get("n_sessions")} |
| chunks | {q.get("n_chunks_total")} |
| sequence diagnostics | {q.get("sequence_diagnostics") or "none"} |
| storage errors | {q.get("storage_errors") or "none"} |
| crossed BBO (`n_bid_ge_ask`) | {q.get("n_bid_ge_ask")} |
| simultaneous bid+ask+mark+index | {q.get("n_simultaneous_bid_ask_mark_index")} |
| simultaneous bid+ask+last+mark+index | {q.get("n_simultaneous_bid_ask_last_mark_index")} |
| timing_adequacy | `{q.get("timing_adequacy")}` |
| replay canonical sha256 | `{q.get("replay_determinism_sha256")}` |
| trigger mix | {q.get("trigger_counts")} |
| first ready | {q.get("first_ready")} |

Interarrival (all snapshots, including warmup): `{_fmt_dist(q.get("interarrival_ms"), 1)}`.

Selector `chosen_bbo_source`: {bbo_txt}.

### Sessions

| session_id | started_at | ended_at | n_snapshots | chunks | status | storage_error |
| --- | --- | --- | --- | --- | --- | --- |
{chr(10).join(session_lines)}

### Interruptions

| kind | from | to | gap_ms |
| --- | --- | --- | --- |
{chr(10).join(inter_lines)}

### Warmup / DATA_INVALID bursts

| class | n | seq | duration_ms | start_t |
| --- | --- | --- | --- | --- |
{chr(10).join(burst_lines)}

Warmup reasons: `{q.get("startup_warmup_reasons")}`

DATA_INVALID reasons: `{q.get("data_invalid_reasons")}`

Invalid reasons (unclassified mix of skipped_reason + snapshot flags):
`{q.get("invalid_reasons")}`

Field age (ms): see JSON `capture_quality.field_age_ms`.

## Descriptive market dynamics

Not a trading rule. Executable mid is present only on `READY_VALID` rows.
`STARTUP_WARMUP` / `DATA_INVALID` contribute `mid=None` so a missing BBO is
not replaced by the previous mid.

Start/end: `{m.get("start_end")}`

Spread (bps, ready executable): `{_fmt_dist(m.get("spread_bps"))}`

| gap | distribution |
| --- | --- |
| mid−mark | {_fmt_dist((m.get("gaps_bps") or {}).get("mid_minus_mark_bps"))} |
| mid−index | {_fmt_dist((m.get("gaps_bps") or {}).get("mid_minus_index_bps"))} |
| mark−index | {_fmt_dist((m.get("gaps_bps") or {}).get("mark_minus_index_bps"))} |

### Horizon returns (bps)

Pairs skipped when either endpoint lacks that field (no fill).

| H | last | mid | mark | index |
| --- | --- | --- | --- | --- |
{chr(10).join(ret_lines)}

### Lead/lag cross-correlation (1s grid, lags −5…+5 s)

Positive lag k is corr(x[t], y[t+k]). Peak |corr| per pair:

| pair | peak lag s | corr |
| --- | --- | --- |
{chr(10).join(xcorr_rows)}

Full lag maps: JSON `descriptive_market.lead_lag_xcorr`.

## HYPOTHESIS_SMOKE (not performance)

Frozen `author_observed_v0` only. {smoke_note}

| item | value |
| --- | --- |
| label | `{smoke.get("label")}` |
| observations | {smoke.get("observations")} |
| n_candidates | {smoke.get("n_candidates")} |
| n_accepted_for_shadow | {smoke.get("n_accepted_for_shadow")} |
| n_trades | {smoke.get("n_trades")} |
| n_open | {smoke.get("n_open")} |
| throttle | {smoke.get("throttle_counts")} |
| exits | {smoke.get("exit_reason_counts")} |
| trade gross_bps | {_fmt_dist(smoke.get("trade_gross_bps"))} |

Replay ok: **{report.get("replay_ok")}**. Export sha256 is of the NDJSON
bytes; replay sha256 is of canonical valid observations. Repeating the
canonical hash on the same file is deterministic.

## What was not done

- No mom/gap/exit/threshold/throttle/sizing change
- No ML, PAPER, or live orders
- No B2 upload of this capture
- Raw warmup/invalid rows were not deleted or rewritten
"""


def scan_window(report: dict[str, Any]) -> str:
    if report.get("in_8_to_12h_window") is True:
        return "yes"
    if report.get("duration_ms") is None:
        return "unknown"
    return "no"


def write_long_observation_reports(
    path: Path, *, out_json: Path, out_md: Path
) -> dict[str, Any]:
    report = build_long_observation_report(path)
    report.pop("_series_mids_for_tests", None)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    out_md.write_text(render_long_observation_markdown(report), encoding="utf-8")
    return report
