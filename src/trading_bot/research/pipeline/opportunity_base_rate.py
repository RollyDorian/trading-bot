"""Executable-move opportunity base rates (screening only, no ML).

Overlapping 1s rows are reported for reference but are dependent; prefer
non-overlapping stride statistics for decision framing.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

OPPORTUNITY_HORIZONS_SECONDS: tuple[int, ...] = (
    15,
    30,
    60,
    120,
    300,
    600,
    1_800,
    3_600,
)
MOVE_THRESHOLDS_BPS: tuple[float, ...] = (2.0, 5.0, 10.0, 15.0, 20.0, 30.0)


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    if q <= 0:
        return sorted_vals[0]
    if q >= 1:
        return sorted_vals[-1]
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    weight = pos - lo
    return sorted_vals[lo] * (1.0 - weight) + sorted_vals[hi] * weight


def absolute_executable_move_bps(
    mid_now: float, mid_future: float
) -> float | None:
    """Gross absolute mid move in bps (executable mid proxy from TOB)."""

    if mid_now <= 0 or mid_future <= 0:
        return None
    if not (math.isfinite(mid_now) and math.isfinite(mid_future)):
        return None
    return abs(mid_future / mid_now - 1.0) * 10_000.0


def _fraction_above(values: list[float], threshold: float) -> float | None:
    if not values:
        return None
    return sum(1 for value in values if value >= threshold) / len(values)


def summarize_abs_moves(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "mean_bps": (sum(ordered) / len(ordered)) if ordered else None,
        "p50_bps": _percentile(ordered, 0.50),
        "p90_bps": _percentile(ordered, 0.90),
        "p95_bps": _percentile(ordered, 0.95),
        "p99_bps": _percentile(ordered, 0.99),
        "frac_ge_threshold": {
            str(int(thr) if thr == int(thr) else thr): _fraction_above(ordered, thr)
            for thr in MOVE_THRESHOLDS_BPS
        },
    }


def opportunity_base_rate_report(
    rows: list[dict[str, Any]],
    *,
    horizons: tuple[int, ...] = OPPORTUNITY_HORIZONS_SECONDS,
) -> dict[str, Any]:
    """Compute overlapping and non-overlapping absolute executable mid moves."""

    by_time = {_dt(row["decision_time"]): row for row in rows}
    times = sorted(by_time)
    overlapping: dict[str, Any] = {}
    non_overlapping: dict[str, Any] = {}

    for horizon in horizons:
        key = f"{horizon}s"
        abs_moves: list[float] = []
        for ts in times:
            row = by_time[ts]
            mid = row.get("mid")
            future = by_time.get(ts + timedelta(seconds=horizon))
            if mid is None or future is None or future.get("mid") is None:
                continue
            move = absolute_executable_move_bps(float(mid), float(future["mid"]))
            if move is not None:
                abs_moves.append(move)
        overlapping[key] = {
            **summarize_abs_moves(abs_moves),
            "note": "1s cadence; heavily overlapping / dependent samples",
        }

        # Non-overlapping: stride by horizon seconds from the first timestamp.
        stride_moves: list[float] = []
        if times:
            cursor = times[0]
            end = times[-1]
            while cursor <= end:
                future_ts = cursor + timedelta(seconds=horizon)
                now_row = by_time.get(cursor)
                fut_row = by_time.get(future_ts)
                if (
                    now_row is not None
                    and fut_row is not None
                    and now_row.get("mid") is not None
                    and fut_row.get("mid") is not None
                ):
                    move = absolute_executable_move_bps(
                        float(now_row["mid"]), float(fut_row["mid"])
                    )
                    if move is not None:
                        stride_moves.append(move)
                cursor = future_ts
        non_overlapping[key] = {
            **summarize_abs_moves(stride_moves),
            "note": "non-overlapping stride by horizon; prefer for base-rate decisions",
        }

    return {
        "horizons_seconds": list(horizons),
        "thresholds_bps": list(MOVE_THRESHOLDS_BPS),
        "price_definition": "executable_tob_mid",
        "overlapping_1s": overlapping,
        "non_overlapping_stride": non_overlapping,
    }
