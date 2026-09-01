"""Capture-quality statistics. No profitability."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from trading_bot.research.mexc_shadow.ui_capture.normalize import (
    observation_from_snapshot,
    snapshot_from_mapping,
)
from trading_bot.research.mexc_shadow.ui_capture.schema import CaptureQualityReport
from trading_bot.research.mexc_shadow.ui_capture.store import iter_raw_mappings


def _ms(stamp: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() * 1000.0


def summarize_capture(path: Path) -> CaptureQualityReport:
    missing: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    invalid_reasons: Counter[str] = Counter()
    arrivals: list[float] = []
    n_raw = 0
    n_valid = 0
    coexist = 0
    canonical: list[str] = []
    for payload in iter_raw_mappings(path):
        n_raw += 1
        snap = snapshot_from_mapping(payload)
        rec = observation_from_snapshot(snap)
        if rec.observation is None:
            invalid_reasons[rec.skipped_reason or "unknown"] += 1
            for reason in snap.invalid_reasons:
                invalid_reasons[reason] += 1
        else:
            n_valid += 1
            obs = rec.observation
            if obs.mark is not None and obs.index is not None:
                coexist += 1
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
        for name, field in snap.fields.items():
            statuses[field.parse_status] += 1
            if field.parse_status == "missing":
                missing[name] += 1
    deltas = [arrivals[i] - arrivals[i - 1] for i in range(1, len(arrivals))]
    deltas.sort()
    interarrival: dict[str, float | int | None]
    if not deltas:
        interarrival = {"n": 0, "p50_ms": None, "p95_ms": None, "min_ms": None, "max_ms": None}
    else:
        interarrival = {
            "n": len(deltas),
            "p50_ms": deltas[len(deltas) // 2],
            "p95_ms": deltas[min(len(deltas) - 1, int(len(deltas) * 0.95))],
            "min_ms": deltas[0],
            "max_ms": deltas[-1],
        }
    digest = hashlib.sha256("\n".join(canonical).encode("utf-8")).hexdigest() if canonical else None
    return CaptureQualityReport(
        n_raw=n_raw,
        n_valid_for_replay=n_valid,
        n_invalid=n_raw - n_valid,
        invalid_reasons=dict(invalid_reasons),
        missingness=dict(missing),
        parse_status_counts=dict(statuses),
        interarrival_ms=interarrival,
        coexistence_bid_ask_mark_index=coexist,
        replay_determinism_sha256=digest,
        notes=(
            "No profitability is computed from capture quality.",
            "coexistence_bid_ask_mark_index counts valid replay rows with mark and index.",
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
        "notes": list(report.notes),
    }
