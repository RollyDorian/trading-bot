"""Capture-quality statistics. No profitability."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from trading_bot.research.mexc_shadow.ui_capture.durable import (
    diagnose_sequence,
    is_session_record,
)
from trading_bot.research.mexc_shadow.ui_capture.normalize import (
    observation_from_snapshot,
    snapshot_from_mapping,
)
from trading_bot.research.mexc_shadow.ui_capture.schema import CaptureQualityReport
from trading_bot.research.mexc_shadow.ui_capture.store import iter_all_mappings

_VALUE_STATUSES = frozenset({"ok", "ok_redundant"})


def _ms(stamp: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() * 1000.0


def _percentile(sorted_vals: list[float], fraction: float) -> float | None:
    if not sorted_vals:
        return None
    index = min(len(sorted_vals) - 1, int(len(sorted_vals) * fraction))
    return sorted_vals[index]


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


def _ok_price(field: Any) -> float | None:
    if field is None or field.parse_status not in _VALUE_STATUSES:
        return None
    if isinstance(field.value, int | float) and not isinstance(field.value, bool):
        number = float(field.value)
        return number if number > 0 else None
    return None


def _timing_adequacy(
    *,
    n_raw: int,
    interarrival: dict[str, float | int | None],
    n_simultaneous: int,
    n_valid: int,
) -> str:
    """Whether the sample could support later few-bps reconstruction. Not a claim it works."""

    if n_raw < 20:
        return "INSUFFICIENT_SAMPLE"
    p95 = interarrival.get("p95_ms")
    if p95 is None:
        return "UNKNOWN"
    coexist_frac = n_simultaneous / n_raw if n_raw else 0.0
    if n_valid == 0 or coexist_frac < 0.5:
        return "MISSING_MARK_INDEX_OR_BBO"
    if float(p95) > 2000:
        return "COARSE_FOR_FEW_BPS"
    if float(p95) <= 1000 and coexist_frac >= 0.8:
        return "ADEQUATE_FOR_REVIEW_NOT_PROOF"
    return "MARGINAL_FOR_FEW_BPS"


def _diagnose_by_session(raw_snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sequence continuity is per capture_id; stop/start resets sequence to 1."""

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for payload in raw_snapshots:
        session_id = str(payload.get("capture_id") or "_unknown")
        grouped[session_id].append(payload)
    out: list[dict[str, Any]] = []
    for session_id, rows in grouped.items():
        for item in diagnose_sequence(rows):
            out.append({**item, "session_id": session_id})
    return out


def _attach_session_end(
    session_rows: list[dict[str, Any]], payload: dict[str, Any]
) -> None:
    session_id = str(payload.get("session_id") or "")
    for row in reversed(session_rows):
        if row.get("session_id") == session_id and row.get("end") is None:
            row["end"] = payload
            return
    session_rows.append(
        {"session_id": session_id or None, "start": None, "end": payload}
    )


def summarize_capture(path: Path) -> CaptureQualityReport:
    missing: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    invalid_reasons: Counter[str] = Counter()
    triggers: Counter[str] = Counter()
    changes: Counter[str] = Counter()
    ages: dict[str, list[float]] = defaultdict(list)
    arrivals: list[float] = []
    raw_snapshots: list[dict[str, Any]] = []
    session_rows: list[dict[str, Any]] = []
    n_raw = 0
    n_valid = 0
    coexist_valid = 0
    n_simultaneous = 0
    n_bid_ge_ask = 0
    capture_ids: set[str] = set()
    canonical: list[str] = []
    for payload in iter_all_mappings(path):
        if is_session_record(payload):
            if payload.get("record_type") == "session_start":
                session_rows.append(
                    {
                        "session_id": payload.get("session_id"),
                        "start": payload,
                        "end": None,
                    }
                )
            elif payload.get("record_type") == "session_end":
                _attach_session_end(session_rows, payload)
            continue
        raw_snapshots.append(payload)
        n_raw += 1
        snap = snapshot_from_mapping(payload)
        if snap.capture_id:
            capture_ids.add(snap.capture_id)
        triggers[str(snap.trigger)] += 1
        rec = observation_from_snapshot(snap)
        if rec.observation is None:
            invalid_reasons[rec.skipped_reason or "unknown"] += 1
            for reason in snap.invalid_reasons:
                invalid_reasons[reason] += 1
        else:
            n_valid += 1
            obs = rec.observation
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
        stamp = _ms(snap.received_at_local)
        if stamp is not None:
            arrivals.append(stamp)
        bid = _ok_price(snap.fields.get("bid"))
        ask = _ok_price(snap.fields.get("ask"))
        mark = _ok_price(snap.fields.get("mark"))
        index = _ok_price(snap.fields.get("index"))
        if bid is not None and ask is not None and mark is not None and index is not None:
            n_simultaneous += 1
        if bid is not None and ask is not None and bid >= ask:
            n_bid_ge_ask += 1
        for name in snap.changed_fields:
            changes[name] += 1
        for name, field in snap.fields.items():
            statuses[field.parse_status] += 1
            if field.parse_status == "missing":
                missing[name] += 1
            if field.age_ms is not None:
                ages[name].append(float(field.age_ms))
    deltas = [arrivals[i] - arrivals[i - 1] for i in range(1, len(arrivals))]
    deltas.sort()
    interarrival: dict[str, float | int | None]
    if not deltas:
        interarrival = {
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
    change_rate = {name: count / n_raw for name, count in changes.items()} if n_raw else {}
    field_age = {name: _age_stats(values) for name, values in sorted(ages.items())}
    digest = hashlib.sha256("\n".join(canonical).encode("utf-8")).hexdigest() if canonical else None
    capture_id = next(iter(capture_ids)) if len(capture_ids) == 1 else None
    if capture_id is None and not capture_ids and session_rows:
        last_id = session_rows[-1].get("session_id")
        capture_id = str(last_id) if last_id else None
    session = None
    if session_rows:
        session = {
            "start": session_rows[-1].get("start"),
            "end": session_rows[-1].get("end"),
        }
    seq_diag = _diagnose_by_session(raw_snapshots)
    for row in session_rows:
        end = row.get("end") or {}
        session_id = row.get("session_id")
        seq_diag.extend(
            {**item, "source": "session_end", "session_id": session_id}
            for item in (end.get("sequence_gaps") or [])
        )
        seq_diag.extend(
            {**item, "source": "client_sequence", "session_id": session_id}
            for item in (end.get("client_sequence_mismatches") or [])
        )
    n_chunks_total = 0
    for row in session_rows:
        end = row.get("end") or {}
        start = row.get("start") or {}
        chunks = end.get("n_chunks")
        if chunks is None:
            chunks = start.get("n_chunks")
        if chunks is not None:
            n_chunks_total += int(chunks)
    adequacy = _timing_adequacy(
        n_raw=n_raw,
        interarrival=interarrival,
        n_simultaneous=n_simultaneous,
        n_valid=n_valid,
    )
    return CaptureQualityReport(
        n_raw=n_raw,
        n_valid_for_replay=n_valid,
        n_invalid=n_raw - n_valid,
        invalid_reasons=dict(invalid_reasons),
        missingness=dict(missing),
        parse_status_counts=dict(statuses),
        interarrival_ms=interarrival,
        coexistence_bid_ask_mark_index=coexist_valid,
        replay_determinism_sha256=digest,
        capture_id=capture_id,
        duration_ms=duration_ms,
        trigger_counts=dict(triggers),
        field_change_counts=dict(changes),
        field_change_rate=change_rate,
        field_age_ms=field_age,
        n_bid_ge_ask=n_bid_ge_ask,
        n_simultaneous_bid_ask_mark_index=n_simultaneous,
        sequence_diagnostics=seq_diag,
        session=session,
        sessions=session_rows,
        n_sessions=len(session_rows),
        n_chunks_total=n_chunks_total,
        timing_adequacy=adequacy,
        notes=(
            "No profitability is computed from capture quality.",
            "coexistence_bid_ask_mark_index counts valid replay rows with mark and index.",
            "n_simultaneous_bid_ask_mark_index counts raw rows with bid, ask, mark, and index.",
            "timing_adequacy is a sample-timing review flag, not mom/gap proof.",
            "Frozen-profile replay PnL is pipeline smoke only.",
            "sequence_diagnostics are per capture_id; a new session restarts sequence at 1.",
        ),
    )


def quality_as_dict(report: CaptureQualityReport) -> dict[str, Any]:
    return {
        "n_raw": report.n_raw,
        "n_valid_for_replay": report.n_valid_for_replay,
        "n_invalid": report.n_invalid,
        "invalid_reasons": report.invalid_reasons,
        "missingness": report.missingness,
        "parse_status_counts": report.parse_status_counts,
        "interarrival_ms": report.interarrival_ms,
        "coexistence_bid_ask_mark_index": report.coexistence_bid_ask_mark_index,
        "replay_determinism_sha256": report.replay_determinism_sha256,
        "capture_id": report.capture_id,
        "duration_ms": report.duration_ms,
        "trigger_counts": report.trigger_counts,
        "field_change_counts": report.field_change_counts,
        "field_change_rate": report.field_change_rate,
        "field_age_ms": report.field_age_ms,
        "n_bid_ge_ask": report.n_bid_ge_ask,
        "n_simultaneous_bid_ask_mark_index": report.n_simultaneous_bid_ask_mark_index,
        "sequence_diagnostics": report.sequence_diagnostics,
        "session": report.session,
        "sessions": report.sessions,
        "n_sessions": report.n_sessions,
        "n_chunks_total": report.n_chunks_total,
        "timing_adequacy": report.timing_adequacy,
        "notes": list(report.notes),
    }
