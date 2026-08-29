"""Intrawindow first-passage opportunity protocol v1 (no ML, no trading).

Previous opportunity screens in ``opportunity_base_rate.py`` used endpoint
``mid[t+h] / mid[t]`` only. A path that reaches +20 bps and returns to 0 at
the horizon was counted as no move. This module measures maximum excursion
and first passage inside the window instead.

Grids are frozen before results. Do not retune horizons or thresholds after
seeing hit rates. Mid excursion is not tradeable PnL; executable TOB paths
and the ~11 bps taker round-trip reference are reported as separate layers.

Memory: scan each 1s-contiguous segment once up to max horizon. Do not build
an N×H Python object matrix of full windows.
"""

from __future__ import annotations

import math
from array import array
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trading_bot.research.collection_gaps import CollectionGap
from trading_bot.research.pipeline.executable_tob import is_executable_tob_source

FIRST_PASSAGE_PROTOCOL_VERSION = 1
FIRST_PASSAGE_PROTOCOL_NAME = "eth_first_passage_opportunity_v1"

# Frozen before any price-movement results. Do not edit after inspection.
FIRST_PASSAGE_HORIZONS_SECONDS: tuple[int, ...] = (
    5,
    10,
    15,
    30,
    60,
    120,
    180,
    300,
    600,
)
FIRST_PASSAGE_THRESHOLDS_BPS: tuple[float, ...] = (
    5.0,
    10.0,
    15.0,
    20.0,
    25.0,
    30.0,
    40.0,
    50.0,
    75.0,
    100.0,
)
# Phase offsets as fractions of H: 0, H/4, H/2, 3H/4 (integer seconds).
NON_OVERLAP_OFFSET_FRACTIONS: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75)
TAKER_RT_BPS_REFERENCE = 11.0
# Display-only commentary band, frozen before results. Not a fitted gate.
PREDECLARED_FREQUENCY_COMMENTARY_HITS_PER_24H = 1.0
OOS_RESERVED_UTC_DAYS = 3
MIN_OOS_RESERVED_UTC_DAYS = 2
# Expansion-only slice of the frozen grids (lead-requested day/block check).
# Do not edit FIRST_PASSAGE_HORIZONS_SECONDS or FIRST_PASSAGE_THRESHOLDS_BPS.
DAY_STABILITY_HORIZONS_SECONDS: tuple[int, ...] = (60, 120, 300, 600)
DAY_STABILITY_THRESHOLDS_BPS: tuple[float, ...] = (10.0, 15.0, 20.0, 25.0, 30.0)
MOVEMENT_EPISODE_VERSION = "movement_episode_v1"


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _percentile(sorted_vals: Sequence[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    if q <= 0:
        return float(sorted_vals[0])
    if q >= 1:
        return float(sorted_vals[-1])
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(sorted_vals[lo])
    weight = pos - lo
    return float(sorted_vals[lo]) * (1.0 - weight) + float(sorted_vals[hi]) * weight


def _pct_block(samples: array[float], quantiles: tuple[float, ...]) -> dict[str, float | None]:
    ordered = sorted(samples)
    return {f"p{int(q * 100)}": _percentile(ordered, q) for q in quantiles}


def executable_prices_ok(bid: float, ask: float, mid: float) -> bool:
    """Require a usable TOB for both mid and hypothetical executable fills."""

    if not (math.isfinite(bid) and math.isfinite(ask) and math.isfinite(mid)):
        return False
    return bid > 0.0 and ask > 0.0 and mid > 0.0 and bid < ask


def non_overlap_offsets(horizon: int) -> tuple[int, ...]:
    """Integer phase offsets 0, H/4, H/2, 3H/4 for one horizon."""

    if horizon < 1:
        raise ValueError("horizon must be positive")
    return tuple(int(horizon * frac) for frac in NON_OVERLAP_OFFSET_FRACTIONS)


def non_overlap_starts(length: int, horizon: int, offset: int) -> list[int]:
    """Local indices i in a contiguous segment of ``length`` points.

    Window ``i .. i+horizon`` inclusive needs ``horizon + 1`` samples, so
    ``i + horizon < length``. Next start is ``i + horizon`` (non-overlapping).
    """

    if horizon < 1 or offset < 0 or length <= 0:
        return []
    starts: list[int] = []
    index = offset
    while index + horizon < length:
        starts.append(index)
        index += horizon
    return starts


def _path_row_executable(
    index: int,
    bid: Sequence[float],
    ask: Sequence[float],
    mid: Sequence[float],
    *,
    tob_source: Sequence[str | None] | None,
    executable_tob: Sequence[bool] | None,
) -> bool:
    """Executable TOB for one 1s row. Numerical bid/ask alone is not enough."""

    if not executable_prices_ok(bid[index], ask[index], mid[index]):
        return False
    if executable_tob is not None:
        return bool(executable_tob[index])
    if tob_source is not None:
        return is_executable_tob_source(tob_source[index])
    return True


def _connection_boundary(
    start: int,
    current: int,
    connection_id: Sequence[str | None] | None,
) -> bool:
    """True when the open path crosses a reconnect/session change."""

    if connection_id is None:
        return False
    left = connection_id[start]
    right = connection_id[current]
    if left is None or right is None:
        return False
    return left != right


def split_contiguous_1s_segments(epoch_s: Sequence[int]) -> list[tuple[int, int]]:
    """Return half-open [start, end) index ranges with consecutive 1-second steps."""

    if not epoch_s:
        return []
    segments: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(epoch_s)):
        if epoch_s[index] != epoch_s[index - 1] + 1:
            segments.append((start, index))
            start = index
    segments.append((start, len(epoch_s)))
    return segments


@dataclass
class _SampleAcc:
    """Streaming sample store (array.array, not a Python float object per row)."""

    values: array[float] = field(default_factory=lambda: array("d"))

    def add(self, value: float) -> None:
        self.values.append(value)

    def percentiles(self, quantiles: tuple[float, ...]) -> dict[str, float | None]:
        return _pct_block(self.values, quantiles)


@dataclass
class _HitAcc:
    n_valid: int = 0
    n_data_invalid: int = 0
    long_hits: int = 0
    short_hits: int = 0
    either_hits: int = 0
    long_first_s: array[float] = field(default_factory=lambda: array("d"))
    short_first_s: array[float] = field(default_factory=lambda: array("d"))
    either_first_s: array[float] = field(default_factory=lambda: array("d"))
    mae_long: array[float] = field(default_factory=lambda: array("d"))
    mae_short: array[float] = field(default_factory=lambda: array("d"))
    plus_before_minus: int = 0
    minus_before_plus: int = 0
    first_touch_tie: int = 0
    first_touch_neither: int = 0
    # Rolling hit-start epochs for movement_episode_v1 (not dumped in summary).
    long_hit_starts: array[int] = field(default_factory=lambda: array("q"))
    short_hit_starts: array[int] = field(default_factory=lambda: array("q"))

    def add_window(
        self,
        *,
        long_lag: int,
        short_lag: int,
        mae_long: float,
        mae_short: float,
        plus_lag: int,
        minus_lag: int,
        record_first_touch: bool,
        start_epoch_s: int | None = None,
    ) -> None:
        self.n_valid += 1
        long_hit = long_lag > 0
        short_hit = short_lag > 0
        if long_hit:
            self.long_hits += 1
            self.long_first_s.append(float(long_lag))
            self.mae_long.append(mae_long)
            if start_epoch_s is not None:
                self.long_hit_starts.append(int(start_epoch_s))
        if short_hit:
            self.short_hits += 1
            self.short_first_s.append(float(short_lag))
            self.mae_short.append(mae_short)
            if start_epoch_s is not None:
                self.short_hit_starts.append(int(start_epoch_s))
        if long_hit or short_hit:
            self.either_hits += 1
            either_lag = min(x for x in (long_lag, short_lag) if x > 0)
            self.either_first_s.append(float(either_lag))
        if record_first_touch:
            if plus_lag == 0 and minus_lag == 0:
                self.first_touch_neither += 1
            elif plus_lag == 0:
                self.minus_before_plus += 1
            elif minus_lag == 0:
                self.plus_before_minus += 1
            elif plus_lag == minus_lag:
                self.first_touch_tie += 1
            elif plus_lag < minus_lag:
                self.plus_before_minus += 1
            else:
                self.minus_before_plus += 1

    def add_data_invalid(self) -> None:
        """Path became unobservable before first touch or horizon. Not a no-hit."""

        self.n_data_invalid += 1

    def summary(self) -> dict[str, Any]:
        n = self.n_valid
        return {
            "n_valid_starts": n,
            "n_data_invalid": self.n_data_invalid,
            "long_hit_count": self.long_hits,
            "long_hit_fraction": (self.long_hits / n) if n else None,
            "short_hit_count": self.short_hits,
            "short_hit_fraction": (self.short_hits / n) if n else None,
            "either_side_hit_count": self.either_hits,
            "either_side_hit_fraction": (self.either_hits / n) if n else None,
            "first_hit_time_s": {
                "long": _pct_block(self.long_first_s, (0.25, 0.50, 0.75, 0.90)),
                "short": _pct_block(self.short_first_s, (0.25, 0.50, 0.75, 0.90)),
                "either": _pct_block(self.either_first_s, (0.25, 0.50, 0.75, 0.90)),
            },
            "mae_before_first_tp_bps": {
                "long": _pct_block(self.mae_long, (0.50, 0.75, 0.90)),
                "short": _pct_block(self.mae_short, (0.50, 0.75, 0.90)),
            },
            "first_touch_diagnostic": {
                "plus_before_minus": self.plus_before_minus,
                "minus_before_plus": self.minus_before_plus,
                "tie": self.first_touch_tie,
                "neither": self.first_touch_neither,
                "note": (
                    "Diagnostic only, not a strategy. plus_before_minus is +X "
                    "before -X (long-side first touch); minus_before_plus is "
                    "-X before +X (short-side first touch)."
                ),
            },
        }


@dataclass
class _MfeAcc:
    up: array[float] = field(default_factory=lambda: array("d"))
    down: array[float] = field(default_factory=lambda: array("d"))
    abs_: array[float] = field(default_factory=lambda: array("d"))

    def add(self, up: float, down: float, abs_exc: float) -> None:
        self.up.append(up)
        self.down.append(down)
        self.abs_.append(abs_exc)

    def summary(self) -> dict[str, Any]:
        qs = (0.50, 0.75, 0.90, 0.95, 0.99)
        return {
            "up_bps": _pct_block(self.up, qs),
            "down_bps": _pct_block(self.down, qs),
            "abs_bps": _pct_block(self.abs_, qs),
        }


def _empty_hit_grid(
    horizons: tuple[int, ...], thresholds: tuple[float, ...]
) -> dict[int, dict[float, _HitAcc]]:
    return {
        horizon: {threshold: _HitAcc() for threshold in thresholds} for horizon in horizons
    }


def _empty_mfe_grid(horizons: tuple[int, ...]) -> dict[int, _MfeAcc]:
    return {horizon: _MfeAcc() for horizon in horizons}


def cluster_adjacent_1s_starts(starts: Sequence[int]) -> list[dict[str, int]]:
    """Merge rolling hit-starts of one direction into movement_episode_v1.

    Predeclared rule, not fitted: successive start epochs join the same episode
    iff they differ by exactly 1 second (market_state cadence). A 2-second or
    larger gap is a new episode. No extra clustering gap or cooldown.
    """

    ordered = sorted({int(value) for value in starts})
    if not ordered:
        return []
    episodes: list[dict[str, int]] = []
    first = last = ordered[0]
    count = 1
    for stamp in ordered[1:]:
        if stamp == last + 1:
            last = stamp
            count += 1
            continue
        episodes.append(
            {
                "first_start_epoch_s": first,
                "last_start_epoch_s": last,
                "n_rolling_starts": count,
                "span_seconds": last - first + 1,
            }
        )
        first = last = stamp
        count = 1
    episodes.append(
        {
            "first_start_epoch_s": first,
            "last_start_epoch_s": last,
            "n_rolling_starts": count,
            "span_seconds": last - first + 1,
        }
    )
    return episodes


def _episode_side_block(
    episodes: list[dict[str, int]], *, usable_hours: float
) -> dict[str, Any]:
    count = len(episodes)
    lengths = [item["n_rolling_starts"] for item in episodes]
    spans = [item["span_seconds"] for item in episodes]
    usable_days = usable_hours / 24.0 if usable_hours > 0 else 0.0
    ordered_len = sorted(float(value) for value in lengths)
    ordered_span = sorted(float(value) for value in spans)
    return {
        "episode_count": count,
        "episodes_per_usable_day": (count / usable_days) if usable_days > 0 else None,
        "n_rolling_hit_starts": int(sum(lengths)),
        "episode_n_starts": {
            "p50": _percentile(ordered_len, 0.50) if ordered_len else None,
            "p90": _percentile(ordered_len, 0.90) if ordered_len else None,
        },
        "episode_span_seconds": {
            "p50": _percentile(ordered_span, 0.50) if ordered_span else None,
            "p90": _percentile(ordered_span, 0.90) if ordered_span else None,
        },
    }


def _scan_segment(
    bid: Sequence[float],
    ask: Sequence[float],
    mid: Sequence[float],
    epoch_s: Sequence[int],
    *,
    lo: int,
    hi: int,
    horizons: tuple[int, ...],
    thresholds: tuple[float, ...],
    rolling_mid: dict[int, dict[float, _HitAcc]],
    rolling_exec: dict[int, dict[float, _HitAcc]],
    rolling_mfe_mid: dict[int, _MfeAcc],
    rolling_mfe_exec: dict[int, _MfeAcc],
    offset_mid: dict[int, dict[int, dict[float, _HitAcc]]],
    offset_exec: dict[int, dict[int, dict[float, _HitAcc]]],
    offset_mfe_mid: dict[int, dict[int, _MfeAcc]],
    offset_mfe_exec: dict[int, dict[int, _MfeAcc]],
    tob_source: Sequence[str | None] | None = None,
    executable_tob: Sequence[bool] | None = None,
    connection_id: Sequence[str | None] | None = None,
) -> int:
    """Scan one 1s-contiguous segment. Returns number of local starts that had any valid H."""

    length = hi - lo
    if length < 2:
        return 0
    max_h = horizons[-1]
    n_thr = len(thresholds)
    horizon_set = {int(h): True for h in horizons}
    starts_used = 0

    def _ok(index: int) -> bool:
        return _path_row_executable(
            index,
            bid,
            ask,
            mid,
            tob_source=tob_source,
            executable_tob=executable_tob,
        )

    def _invalidate_from(local: int, min_horizon: int) -> None:
        for horizon in horizons:
            if horizon < min_horizon:
                continue
            offsets = non_overlap_offsets(horizon)
            for threshold in thresholds:
                rolling_mid[horizon][threshold].add_data_invalid()
                rolling_exec[horizon][threshold].add_data_invalid()
                for offset in offsets:
                    if local >= offset and (local - offset) % horizon == 0:
                        offset_mid[horizon][offset][threshold].add_data_invalid()
                        offset_exec[horizon][offset][threshold].add_data_invalid()

    for local in range(length):
        i = lo + local
        remain = length - 1 - local
        if not _ok(i):
            continue
        if remain < horizons[0]:
            _invalidate_from(local, horizons[0])
            continue
        starts_used += 1

        max_up = 0.0
        max_down = 0.0
        max_exec_long = 0.0
        max_exec_short = 0.0
        up_lag = [0] * n_thr
        down_lag = [0] * n_thr
        exec_long_lag = [0] * n_thr
        exec_short_lag = [0] * n_thr
        mae_mid_long = [0.0] * n_thr
        mae_mid_short = [0.0] * n_thr
        mae_ex_long = [0.0] * n_thr
        mae_ex_short = [0.0] * n_thr
        locked_mid_long = [False] * n_thr
        locked_mid_short = [False] * n_thr
        locked_ex_long = [False] * n_thr
        locked_ex_short = [False] * n_thr
        hits_open = True
        scan_h = min(max_h, remain)
        broke_at: int | None = None

        for k in range(1, scan_h + 1):
            j = i + k
            if not _ok(j) or _connection_boundary(i, j, connection_id):
                broke_at = k
                break
            mid_j = mid[j]
            bid_j = bid[j]
            ask_j = ask[j]
            # Signed mid excursions vs entry mid (bps).
            up = (mid_j / mid[i] - 1.0) * 10_000.0
            down = (1.0 - mid_j / mid[i]) * 10_000.0
            if up > max_up:
                max_up = up
            if down > max_down:
                max_down = down
            # Long: buy ask[t], sell future bid. Short: sell bid[t], cover future ask.
            exec_long = (bid_j / ask[i] - 1.0) * 10_000.0
            exec_short = (bid[i] / ask_j - 1.0) * 10_000.0
            if exec_long > max_exec_long:
                max_exec_long = exec_long
            if exec_short > max_exec_short:
                max_exec_short = exec_short
            adv_mid_long = down if down > 0.0 else 0.0
            adv_mid_short = up if up > 0.0 else 0.0
            adv_ex_long = -exec_long if exec_long < 0.0 else 0.0
            adv_ex_short = -exec_short if exec_short < 0.0 else 0.0

            if hits_open:
                for ti, threshold in enumerate(thresholds):
                    if not locked_mid_long[ti]:
                        if adv_mid_long > mae_mid_long[ti]:
                            mae_mid_long[ti] = adv_mid_long
                        if up >= threshold:
                            up_lag[ti] = k
                            locked_mid_long[ti] = True
                    if not locked_mid_short[ti]:
                        if adv_mid_short > mae_mid_short[ti]:
                            mae_mid_short[ti] = adv_mid_short
                        if down >= threshold:
                            down_lag[ti] = k
                            locked_mid_short[ti] = True
                    if not locked_ex_long[ti]:
                        if adv_ex_long > mae_ex_long[ti]:
                            mae_ex_long[ti] = adv_ex_long
                        if exec_long >= threshold:
                            exec_long_lag[ti] = k
                            locked_ex_long[ti] = True
                    if not locked_ex_short[ti]:
                        if adv_ex_short > mae_ex_short[ti]:
                            mae_ex_short[ti] = adv_ex_short
                        if exec_short >= threshold:
                            exec_short_lag[ti] = k
                            locked_ex_short[ti] = True
                hits_open = not (
                    all(locked_mid_long)
                    and all(locked_mid_short)
                    and all(locked_ex_long)
                    and all(locked_ex_short)
                )

            if k not in horizon_set:
                continue
            # Window [t, t+k] is complete only if we actually scanned k steps.
            max_abs = max_up if max_up > max_down else max_down
            max_exec_abs = (
                max_exec_long if max_exec_long > max_exec_short else max_exec_short
            )
            rolling_mfe_mid[k].add(max_up, max_down, max_abs)
            rolling_mfe_exec[k].add(max_exec_long, max_exec_short, max_exec_abs)
            offsets = non_overlap_offsets(k)
            start_epoch = int(epoch_s[i])
            for ti, threshold in enumerate(thresholds):
                rolling_mid[k][threshold].add_window(
                    long_lag=up_lag[ti],
                    short_lag=down_lag[ti],
                    mae_long=mae_mid_long[ti],
                    mae_short=mae_mid_short[ti],
                    plus_lag=up_lag[ti],
                    minus_lag=down_lag[ti],
                    record_first_touch=True,
                    start_epoch_s=start_epoch,
                )
                rolling_exec[k][threshold].add_window(
                    long_lag=exec_long_lag[ti],
                    short_lag=exec_short_lag[ti],
                    mae_long=mae_ex_long[ti],
                    mae_short=mae_ex_short[ti],
                    plus_lag=0,
                    minus_lag=0,
                    record_first_touch=False,
                    start_epoch_s=start_epoch,
                )
                for offset in offsets:
                    if local >= offset and (local - offset) % k == 0:
                        offset_mfe_mid[k][offset].add(max_up, max_down, max_abs)
                        offset_mfe_exec[k][offset].add(
                            max_exec_long, max_exec_short, max_exec_abs
                        )
                        offset_mid[k][offset][threshold].add_window(
                            long_lag=up_lag[ti],
                            short_lag=down_lag[ti],
                            mae_long=mae_mid_long[ti],
                            mae_short=mae_mid_short[ti],
                            plus_lag=up_lag[ti],
                            minus_lag=down_lag[ti],
                            record_first_touch=True,
                        )
                        offset_exec[k][offset][threshold].add_window(
                            long_lag=exec_long_lag[ti],
                            short_lag=exec_short_lag[ti],
                            mae_long=mae_ex_long[ti],
                            mae_short=mae_ex_short[ti],
                            plus_lag=0,
                            minus_lag=0,
                            record_first_touch=False,
                        )

        completed_through = scan_h if broke_at is None else broke_at - 1
        for horizon in horizons:
            if horizon <= completed_through:
                continue
            _invalidate_from(local, horizon)
            break
    return starts_used


def _frequency_block(
    *,
    hit_fraction: float | None,
    hit_count: int,
    n_valid: int,
    horizon: int,
    usable_hours: float,
    kind: str,
) -> dict[str, Any]:
    hits_per_hour_rolling = (
        (hit_count / usable_hours) if usable_hours > 0 else None
    )
    slots_per_hour = 3600.0 / float(horizon)
    hits_per_hour_nonoverlap = (
        hit_fraction * slots_per_hour if hit_fraction is not None else None
    )
    return {
        "kind": kind,
        "note": (
            "Rolling 1s starts are dependent and are not trades/day. "
            "Non-overlapping base-rate frequency uses stride H with four "
            "phase offsets; report mean/min/max hit fraction. "
            "hits/24 usable hours extrapolate the sample rate; they are not "
            "a count of events inside a shorter discovery window."
        ),
        "rolling_candidate_hits_per_hour": (
            hits_per_hour_rolling if kind == "rolling" else None
        ),
        "rolling_candidate_hits_per_24_usable_hours": (
            hits_per_hour_rolling * 24.0
            if hits_per_hour_rolling is not None and kind == "rolling"
            else None
        ),
        "nonoverlap_hits_per_hour": hits_per_hour_nonoverlap if kind == "non_overlapping" else None,
        "nonoverlap_hits_per_24_usable_hours": (
            hits_per_hour_nonoverlap * 24.0
            if hits_per_hour_nonoverlap is not None and kind == "non_overlapping"
            else None
        ),
        "n_valid_starts": n_valid,
        "observed_hit_count": hit_count,
        "observed_n_valid_starts": n_valid,
        "usable_hours_denominator": usable_hours,
    }


def _summarize_layer(
    *,
    rolling_hits: dict[int, dict[float, _HitAcc]],
    rolling_mfe: dict[int, _MfeAcc],
    offset_hits: dict[int, dict[int, dict[float, _HitAcc]]],
    offset_mfe: dict[int, dict[int, _MfeAcc]],
    horizons: tuple[int, ...],
    thresholds: tuple[float, ...],
    usable_hours: float,
    include_first_touch: bool,
) -> dict[str, Any]:
    rolling_out: dict[str, Any] = {}
    nonoverlap_out: dict[str, Any] = {}
    for horizon in horizons:
        h_key = f"{horizon}s"
        mfe = rolling_mfe[horizon].summary()
        thr_out: dict[str, Any] = {}
        for threshold in thresholds:
            cell = rolling_hits[horizon][threshold].summary()
            if not include_first_touch:
                cell.pop("first_touch_diagnostic", None)
            cell["mfe_bps"] = mfe
            cell["frequency"] = _frequency_block(
                hit_fraction=cell["either_side_hit_fraction"],
                hit_count=cell["either_side_hit_count"],
                n_valid=cell["n_valid_starts"],
                horizon=horizon,
                usable_hours=usable_hours,
                kind="rolling",
            )
            thr_out[str(int(threshold))] = cell
        rolling_out[h_key] = {
            "n_valid_starts": rolling_hits[horizon][thresholds[0]].n_valid,
            "n_data_invalid": rolling_hits[horizon][thresholds[0]].n_data_invalid,
            "mfe_bps": mfe,
            "thresholds": thr_out,
        }

        offsets = non_overlap_offsets(horizon)
        thr_off: dict[str, Any] = {}
        for threshold in thresholds:
            fracs_either: list[float] = []
            fracs_long: list[float] = []
            fracs_short: list[float] = []
            offset_rows: dict[str, Any] = {}
            mae_long_all: array[float] = array("d")
            mae_short_all: array[float] = array("d")
            first_long_all: array[float] = array("d")
            first_short_all: array[float] = array("d")
            n_sum = 0
            long_sum = 0
            short_sum = 0
            either_sum = 0
            for offset in offsets:
                acc = offset_hits[horizon][offset][threshold]
                row = acc.summary()
                if not include_first_touch:
                    row.pop("first_touch_diagnostic", None)
                offset_rows[str(offset)] = {
                    "n_valid_starts": acc.n_valid,
                    "long_hit_count": acc.long_hits,
                    "short_hit_count": acc.short_hits,
                    "either_side_hit_count": acc.either_hits,
                    "long_hit_fraction": row["long_hit_fraction"],
                    "short_hit_fraction": row["short_hit_fraction"],
                    "either_side_hit_fraction": row["either_side_hit_fraction"],
                }
                if acc.n_valid:
                    if row["either_side_hit_fraction"] is not None:
                        fracs_either.append(float(row["either_side_hit_fraction"]))
                    if row["long_hit_fraction"] is not None:
                        fracs_long.append(float(row["long_hit_fraction"]))
                    if row["short_hit_fraction"] is not None:
                        fracs_short.append(float(row["short_hit_fraction"]))
                n_sum += acc.n_valid
                long_sum += acc.long_hits
                short_sum += acc.short_hits
                either_sum += acc.either_hits
                mae_long_all.extend(acc.mae_long)
                mae_short_all.extend(acc.mae_short)
                first_long_all.extend(acc.long_first_s)
                first_short_all.extend(acc.short_first_s)
            mean_either = (sum(fracs_either) / len(fracs_either)) if fracs_either else None
            thr_off[str(int(threshold))] = {
                "offsets_seconds": list(offsets),
                "per_offset": offset_rows,
                "n_valid_starts_mean": (n_sum / len(offsets)) if offsets else None,
                "pooled_descriptive_dependent": {
                    "note": (
                        "Sum across four phase offsets. Offsets share the same "
                        "path and are not independent samples. Do not treat "
                        "pooled n as a larger independent sample, and do not "
                        "hide small per-offset counts behind a pooled percent."
                    ),
                    "n_valid_starts_sum": n_sum,
                    "long_hit_count_sum": long_sum,
                    "short_hit_count_sum": short_sum,
                    "either_side_hit_count_sum": either_sum,
                },
                "long_hit_fraction": {
                    "mean": (sum(fracs_long) / len(fracs_long)) if fracs_long else None,
                    "min": min(fracs_long) if fracs_long else None,
                    "max": max(fracs_long) if fracs_long else None,
                },
                "short_hit_fraction": {
                    "mean": (sum(fracs_short) / len(fracs_short)) if fracs_short else None,
                    "min": min(fracs_short) if fracs_short else None,
                    "max": max(fracs_short) if fracs_short else None,
                },
                "either_side_hit_fraction": {
                    "mean": mean_either,
                    "min": min(fracs_either) if fracs_either else None,
                    "max": max(fracs_either) if fracs_either else None,
                },
                "first_hit_time_s": {
                    "long": _pct_block(first_long_all, (0.25, 0.50, 0.75, 0.90)),
                    "short": _pct_block(first_short_all, (0.25, 0.50, 0.75, 0.90)),
                },
                "mae_before_first_tp_bps": {
                    "long": _pct_block(mae_long_all, (0.50, 0.75, 0.90)),
                    "short": _pct_block(mae_short_all, (0.50, 0.75, 0.90)),
                },
                "frequency": _frequency_block(
                    hit_fraction=mean_either,
                    hit_count=either_sum,
                    n_valid=n_sum,
                    horizon=horizon,
                    usable_hours=usable_hours,
                    kind="non_overlapping",
                ),
                "mfe_bps": offset_mfe[horizon][offsets[0]].summary(),
            }
        nonoverlap_out[h_key] = {
            "offsets_seconds": list(offsets),
            "thresholds": thr_off,
        }
    return {
        "rolling_1s": rolling_out,
        "non_overlapping": nonoverlap_out,
    }


def analyze_first_passage(
    epoch_s: Sequence[int],
    bid: Sequence[float],
    ask: Sequence[float],
    mid: Sequence[float],
    *,
    horizons: tuple[int, ...] = FIRST_PASSAGE_HORIZONS_SECONDS,
    thresholds: tuple[float, ...] = FIRST_PASSAGE_THRESHOLDS_BPS,
    usable_hours: float | None = None,
    tob_source: Sequence[str | None] | None = None,
    executable_tob: Sequence[bool] | None = None,
    connection_id: Sequence[str | None] | None = None,
) -> dict[str, Any]:
    """Compute mid and executable first-passage stats on a 1s series.

    Missing seconds break continuity: a window that spans a hole is invalid
    and is not treated as zero movement.
    """

    n = len(epoch_s)
    if not (len(bid) == len(ask) == len(mid) == n):
        raise ValueError("epoch_s, bid, ask, and mid must have equal length")
    horizons = tuple(sorted(int(h) for h in horizons))
    thresholds = tuple(float(t) for t in thresholds)
    rolling_mid = _empty_hit_grid(horizons, thresholds)
    rolling_exec = _empty_hit_grid(horizons, thresholds)
    rolling_mfe_mid = _empty_mfe_grid(horizons)
    rolling_mfe_exec = _empty_mfe_grid(horizons)
    offset_mid: dict[int, dict[int, dict[float, _HitAcc]]] = {}
    offset_exec: dict[int, dict[int, dict[float, _HitAcc]]] = {}
    offset_mfe_mid: dict[int, dict[int, _MfeAcc]] = {}
    offset_mfe_exec: dict[int, dict[int, _MfeAcc]] = {}
    for horizon in horizons:
        offs = non_overlap_offsets(horizon)
        offset_mid[horizon] = {o: {t: _HitAcc() for t in thresholds} for o in offs}
        offset_exec[horizon] = {o: {t: _HitAcc() for t in thresholds} for o in offs}
        offset_mfe_mid[horizon] = {o: _MfeAcc() for o in offs}
        offset_mfe_exec[horizon] = {o: _MfeAcc() for o in offs}

    segments = split_contiguous_1s_segments(epoch_s)
    for lo, hi in segments:
        _scan_segment(
            bid,
            ask,
            mid,
            epoch_s,
            lo=lo,
            hi=hi,
            horizons=horizons,
            thresholds=thresholds,
            rolling_mid=rolling_mid,
            rolling_exec=rolling_exec,
            rolling_mfe_mid=rolling_mfe_mid,
            rolling_mfe_exec=rolling_mfe_exec,
            offset_mid=offset_mid,
            offset_exec=offset_exec,
            offset_mfe_mid=offset_mfe_mid,
            offset_mfe_exec=offset_mfe_exec,
            tob_source=tob_source,
            executable_tob=executable_tob,
            connection_id=connection_id,
        )

    hours = float(usable_hours) if usable_hours is not None else (n / 3600.0 if n else 0.0)
    return {
        "protocol": FIRST_PASSAGE_PROTOCOL_NAME,
        "protocol_version": FIRST_PASSAGE_PROTOCOL_VERSION,
        "previous_opportunity_definition": {
            "module": "trading_bot.research.pipeline.opportunity_base_rate",
            "price_path": "endpoint_only",
            "formula": "abs(mid[t+h] / mid[t] - 1) * 10000",
            "intrawindow_maximum_excursion": False,
        },
        "this_protocol": {
            "price_path": "intrawindow_maximum_excursion_and_first_passage",
            "mid_is_not_tradeable_pnl": True,
            "grids_frozen_before_results": True,
        },
        "horizons_seconds": list(horizons),
        "thresholds_bps": [int(t) if t == int(t) else t for t in thresholds],
        "non_overlap_offset_fractions": list(NON_OVERLAP_OFFSET_FRACTIONS),
        "n_rows": n,
        "n_contiguous_segments": len(segments),
        "usable_hours": hours,
        "mid": _summarize_layer(
            rolling_hits=rolling_mid,
            rolling_mfe=rolling_mfe_mid,
            offset_hits=offset_mid,
            offset_mfe=offset_mfe_mid,
            horizons=horizons,
            thresholds=thresholds,
            usable_hours=hours,
            include_first_touch=True,
        ),
        "executable_tob": _summarize_layer(
            rolling_hits=rolling_exec,
            rolling_mfe=rolling_mfe_exec,
            offset_hits=offset_exec,
            offset_mfe=offset_mfe_exec,
            horizons=horizons,
            thresholds=thresholds,
            usable_hours=hours,
            include_first_touch=False,
        ),
        "executable_definition": {
            "long": (
                "entry at ask[t]; exit at future bid; "
                "favorable bps = (bid[tau]/ask[t]-1)*10000"
            ),
            "short": (
                "entry at bid[t]; cover at future ask; "
                "favorable bps = (bid[t]/ask[tau]-1)*10000"
            ),
        },
        "movement_episode_v1": _summarize_movement_episodes(
            rolling_mid=rolling_mid,
            rolling_exec=rolling_exec,
            horizons=horizons,
            thresholds=thresholds,
            usable_hours=hours,
        ),
    }


def _summarize_movement_episodes(
    *,
    rolling_mid: dict[int, dict[float, _HitAcc]],
    rolling_exec: dict[int, dict[float, _HitAcc]],
    horizons: tuple[int, ...],
    thresholds: tuple[float, ...],
    usable_hours: float,
) -> dict[str, Any]:
    """Diagnostic: unique 1s-adjacent rolling-hit clusters. Not a trade count."""

    def layer_block(grid: dict[int, dict[float, _HitAcc]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for horizon in horizons:
            thr_out: dict[str, Any] = {}
            for threshold in thresholds:
                acc = grid[horizon][threshold]
                thr_out[str(int(threshold))] = {
                    "long": _episode_side_block(
                        cluster_adjacent_1s_starts(acc.long_hit_starts),
                        usable_hours=usable_hours,
                    ),
                    "short": _episode_side_block(
                        cluster_adjacent_1s_starts(acc.short_hit_starts),
                        usable_hours=usable_hours,
                    ),
                }
            out[f"{horizon}s"] = {"thresholds": thr_out}
        return out

    return {
        "version": MOVEMENT_EPISODE_VERSION,
        "replaces_non_overlap_metric": False,
        "algorithm": {
            "unit": "rolling_1s_hit_start_epoch",
            "merge_iff_successive_starts_differ_by_seconds": 1,
            "gap_of_2s_or_more_is_new_episode": True,
            "directions_not_merged": True,
            "horizons_reported_independently": True,
            "clustering_gap_not_fitted": True,
            "note": (
                "One episode is a run of neighboring rolling starts of the "
                "same direction that all hit the TP inside the frozen horizon. "
                "This approximates distinct excursions better than overlapping "
                "window hits, but it is still window-based (not unique first-touch "
                "timestamps). No extra cooldown was introduced."
            ),
        },
        "mid": layer_block(rolling_mid),
        "executable_tob": layer_block(rolling_exec),
    }


def filter_known_gap_rows(
    times: Sequence[datetime],
    bid: Sequence[float],
    ask: Sequence[float],
    mid: Sequence[float],
    gaps: Sequence[CollectionGap],
) -> tuple[list[int], list[float], list[float], list[float], int]:
    """Drop rows whose decision_time falls in a documented collection hole."""

    epoch: list[int] = []
    out_bid: list[float] = []
    out_ask: list[float] = []
    out_mid: list[float] = []
    dropped = 0
    for ts, b, a, m in zip(times, bid, ask, mid, strict=True):
        instant = _dt(ts)
        if any(gap.contains_timestamp(instant) for gap in gaps):
            dropped += 1
            continue
        if not executable_prices_ok(float(b), float(a), float(m)):
            dropped += 1
            continue
        epoch.append(int(instant.timestamp()))
        out_bid.append(float(b))
        out_ask.append(float(a))
        out_mid.append(float(m))
    return epoch, out_bid, out_ask, out_mid, dropped


def load_executable_series_from_parquet(
    paths: Sequence[Path],
) -> tuple[list[datetime], list[float], list[float], list[float]]:
    """Load decision_time / TOB columns from one or more market_state_1s files."""

    times: list[datetime] = []
    bid: list[float] = []
    ask: list[float] = []
    mid: list[float] = []
    for path in paths:
        table = pq.read_table(
            path, columns=["decision_time", "best_bid", "best_ask", "mid"]
        )
        for row in table.to_pylist():
            ts = _dt(row["decision_time"])
            b = row.get("best_bid")
            a = row.get("best_ask")
            m = row.get("mid")
            if b is None or a is None or m is None:
                continue
            times.append(ts)
            bid.append(float(b))
            ask.append(float(a))
            mid.append(float(m))
    order = sorted(range(len(times)), key=lambda i: times[i])
    times = [times[i] for i in order]
    bid = [bid[i] for i in order]
    ask = [ask[i] for i in order]
    mid = [mid[i] for i in order]
    # Duplicate seconds: keep the last row (later raw id in a concatenated run).
    dedup_t: list[datetime] = []
    dedup_b: list[float] = []
    dedup_a: list[float] = []
    dedup_m: list[float] = []
    for ts, b, a, m in zip(times, bid, ask, mid, strict=True):
        if dedup_t and int(ts.timestamp()) == int(dedup_t[-1].timestamp()):
            dedup_t[-1] = ts
            dedup_b[-1] = b
            dedup_a[-1] = a
            dedup_m[-1] = m
            continue
        dedup_t.append(ts)
        dedup_b.append(b)
        dedup_a.append(a)
        dedup_m.append(m)
    return dedup_t, dedup_b, dedup_a, dedup_m


def cost_overlay(*, taker_rt_bps: float, median_spread_bps: float | None) -> dict[str, Any]:
    """Separate friction layer. Never subtract fees from raw movement stats."""

    return {
        "layer": "cost_reference_only",
        "not_mixed_into_raw_movement_statistics": True,
        "taker_rt_break_even_bps_reference": TAKER_RT_BPS_REFERENCE,
        "taker_rt_break_even_bps_observed": taker_rt_bps,
        "median_spread_bps": median_spread_bps,
        "note": (
            "Current ~11 bps taker round-trip is a friction reference "
            "(tier-1 taker both sides + modeled latency + observed spread). "
            "Mid MFE is not net PnL. Executable TOB excursion is still gross."
        ),
    }


def commentary_cells(
    report: Mapping[str, Any],
    *,
    taker_rt_bps: float = TAKER_RT_BPS_REFERENCE,
    min_hits_per_24h: float = PREDECLARED_FREQUENCY_COMMENTARY_HITS_PER_24H,
) -> list[dict[str, Any]]:
    """List predeclared executable cells with non-overlap frequency above the frozen band.

    Not a claim that the moves are predictable or tradeable.
    """

    out: list[dict[str, Any]] = []
    layer = report.get("executable_tob") or {}
    non_ov = layer.get("non_overlapping") or {}
    for h_key, payload in non_ov.items():
        thresholds = payload.get("thresholds") or {}
        for thr_key, cell in thresholds.items():
            threshold = float(thr_key)
            if threshold + 1e-12 < taker_rt_bps:
                continue
            freq = (cell.get("frequency") or {}).get("nonoverlap_hits_per_24_usable_hours")
            if freq is None or float(freq) < min_hits_per_24h:
                continue
            either = cell.get("either_side_hit_fraction") or {}
            out.append(
                {
                    "horizon": h_key,
                    "threshold_bps": threshold,
                    "nonoverlap_either_hit_fraction_mean": either.get("mean"),
                    "nonoverlap_hits_per_24_usable_hours": freq,
                    "predictability_claimed": False,
                    "note": (
                        "Frequency only: a perfect timer would see this many "
                        "non-overlapping windows/24h reach the TP. No signal."
                    ),
                }
            )
    return out


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{100.0 * value:.2f}%"


def _fmt_num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def _mean_either_frac(thresholds: Mapping[str, Any], key: str) -> str:
    frac = (thresholds.get(key) or {}).get("either_side_hit_fraction") or {}
    return _fmt_pct(frac.get("mean"))


def render_first_passage_markdown(report: Mapping[str, Any]) -> str:
    """Concise lead-facing Markdown. Grids stay frozen; no post-hoc threshold edits."""

    corpus = report.get("corpus") or {}
    cost = report.get("cost_layer") or {}
    lines: list[str] = [
        "# ETH first-passage opportunity v1",
        "",
        f"STATUS: `{report.get('STATUS', 'ETH_FIRST_PASSAGE_OPPORTUNITY_REASSESSMENT_READY')}`",
        f"ML_STATUS: `{report.get('ML_STATUS', 'NOT_STARTED')}`",
        "",
        "## Method",
        "",
        "Previous opportunity analysis (`opportunity_base_rate.py`) used **endpoint**",
        "`mid[t+h] / mid[t]` only. A path that reached +20 bps and returned to 0 at",
        "the horizon was recorded as no move. This protocol measures **intrawindow",
        "maximum excursion** and **threshold first passage**. Mid numbers are not",
        "tradeable PnL. Executable TOB and the ~11 bps taker RT reference are",
        "separate layers. Horizons and thresholds were frozen before results.",
        "",
        "## Corpus",
        "",
        f"- discovery UTC dates: `{corpus.get('discovery_utc_dates')}`",
        f"- discovery usable hours: `{corpus.get('discovery_usable_hours')}`",
        (
            "- discovery usable days (hours/24): `"
            f"{_fmt_num((corpus.get('discovery_usable_hours') or 0) / 24.0, 3)}`"
        ),
        f"- untouched OOS UTC dates: `{corpus.get('oos_utc_dates')}`",
        f"- untouched OOS rows: `{corpus.get('oos_rows_untouched')}`",
        f"- untouched OOS hours: `{corpus.get('oos_usable_hours_untouched')}`",
        f"- lead alerts: `{corpus.get('lead_alerts')}`",
        "",
        "## Cost layer (not mixed into movement stats)",
        "",
        f"- taker RT reference: `{cost.get('taker_rt_break_even_bps_reference')}` bps",
        f"- taker RT observed: `{cost.get('taker_rt_break_even_bps_observed')}` bps",
        f"- median spread: `{cost.get('median_spread_bps')}` bps",
        "",
        "## Mid either-side hit fraction (rolling 1s, dependent)",
        "",
        "| H | n | 5bps | 10 | 15 | 20 | 50 | 100 | MFE p50/p95 abs |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    mid_roll = ((report.get("mid") or {}).get("rolling_1s")) or {}
    for horizon in report.get("horizons_seconds") or FIRST_PASSAGE_HORIZONS_SECONDS:
        row = mid_roll.get(f"{horizon}s") or {}
        thr = row.get("thresholds") or {}
        mfe = (row.get("mfe_bps") or {}).get("abs_bps") or {}
        lines.append(
            "| "
            + " | ".join(
                [
                    f"{horizon}s",
                    str(row.get("n_valid_starts", "—")),
                    _fmt_pct((thr.get("5") or {}).get("either_side_hit_fraction")),
                    _fmt_pct((thr.get("10") or {}).get("either_side_hit_fraction")),
                    _fmt_pct((thr.get("15") or {}).get("either_side_hit_fraction")),
                    _fmt_pct((thr.get("20") or {}).get("either_side_hit_fraction")),
                    _fmt_pct((thr.get("50") or {}).get("either_side_hit_fraction")),
                    _fmt_pct((thr.get("100") or {}).get("either_side_hit_fraction")),
                    f"{_fmt_num(mfe.get('p50'))}/{_fmt_num(mfe.get('p95'))}",
                ]
            )
            + " |"
        )
    lines += [
        "",
        "## Executable TOB either-side hit fraction (non-overlap mean of 4 offsets)",
        "",
        "| H | 5bps | 10 | 15 | 20 | 50 | 100 | hits/24h @20bps |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    ex_non = ((report.get("executable_tob") or {}).get("non_overlapping")) or {}
    for horizon in report.get("horizons_seconds") or FIRST_PASSAGE_HORIZONS_SECONDS:
        payload = ex_non.get(f"{horizon}s") or {}
        thr = payload.get("thresholds") or {}

        hits_20 = (
            (thr.get("20") or {}).get("frequency") or {}
        ).get("nonoverlap_hits_per_24_usable_hours")
        lines.append(
            "| "
            + " | ".join(
                [
                    f"{horizon}s",
                    _mean_either_frac(thr, "5"),
                    _mean_either_frac(thr, "10"),
                    _mean_either_frac(thr, "15"),
                    _mean_either_frac(thr, "20"),
                    _mean_either_frac(thr, "50"),
                    _mean_either_frac(thr, "100"),
                    _fmt_num(hits_20, 2),
                ]
            )
            + " |"
        )
    lines += [
        "",
        "## Time-to-hit (mid, rolling, either-side, seconds)",
        "",
        "| H | TP | p25 | p50 | p75 | p90 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for horizon in (15, 60, 300, 600):
        row = mid_roll.get(f"{horizon}s") or {}
        for tp in ("10", "20", "50"):
            tth = ((row.get("thresholds") or {}).get(tp) or {}).get("first_hit_time_s") or {}
            either = tth.get("either") or {}
            if not (row.get("thresholds") or {}).get(tp):
                continue
            lines.append(
                f"| {horizon}s | {tp} | {_fmt_num(either.get('p25'))} | "
                f"{_fmt_num(either.get('p50'))} | {_fmt_num(either.get('p75'))} | "
                f"{_fmt_num(either.get('p90'))} |"
            )
    lines += [
        "",
        "## MAE before first TP (executable, non-overlap pooled, bps)",
        "",
        "| H | TP | long p50/p75/p90 | short p50/p75/p90 |",
        "|---|---:|---|---|",
    ]
    for horizon in (15, 60, 300, 600):
        payload = ex_non.get(f"{horizon}s") or {}
        for tp in ("10", "20", "50"):
            cell = (payload.get("thresholds") or {}).get(tp) or {}
            mae = cell.get("mae_before_first_tp_bps") or {}
            lng = mae.get("long") or {}
            sh = mae.get("short") or {}
            if not cell:
                continue
            long_mae = "/".join(
                _fmt_num(lng.get(name)) for name in ("p50", "p75", "p90")
            )
            short_mae = "/".join(
                _fmt_num(sh.get(name)) for name in ("p50", "p75", "p90")
            )
            lines.append(f"| {horizon}s | {tp} | {long_mae} | {short_mae} |")
    cells = report.get("economic_frequency_commentary") or []
    lines += [
        "",
        "## Rolling vs non-overlap",
        "",
        "Rolling hit counts are overlapping 1s candidates. Do not convert them to",
        "trades/day. Non-overlap uses stride H with offsets 0, H/4, H/2, 3H/4;",
        "tables above use the mean hit fraction across offsets (min/max in JSON).",
        "hits/24 usable hours extrapolate the observed sample rate.",
        "",
        "## Frequency vs friction (not predictability)",
        "",
        "Predeclared display band: executable TP ≥ taker RT reference and",
        f"non-overlap mean hits/24h ≥ {PREDECLARED_FREQUENCY_COMMENTARY_HITS_PER_24H}.",
        "This is not a fitted threshold and not a trading signal.",
        "",
    ]
    if cells:
        for item in cells:
            lines.append(
                f"- {item.get('horizon')} TP {item.get('threshold_bps')} bps: "
                f"~{_fmt_num(item.get('nonoverlap_hits_per_24_usable_hours'))} "
                "non-overlap windows/24h (frequency only)."
            )
    else:
        lines.append("- No predeclared commentary cells cleared the frozen frequency band.")
    runtime = report.get("runtime") or {}
    blockers = report.get("data_quality_blockers") or []
    lines += [
        "",
        "## Runtime",
        "",
        f"- wall seconds: `{runtime.get('wall_seconds')}`",
        f"- peak RSS MiB: `{runtime.get('peak_rss_mib')}`",
        f"- tracemalloc peak MiB: `{runtime.get('tracemalloc_peak_mib')}`",
        "",
        "## Data-quality blockers",
        "",
    ]
    if blockers:
        for item in blockers:
            lines.append(f"- {item}")
    else:
        lines.append("- none recorded")
    lines += [
        "",
        "## Stop",
        "",
        "No ML, no signal selection, no Binance lead-lag, no PAPER. Report only.",
        "",
    ]
    return "\n".join(lines) + "\n"


def slice_series_by_time(
    times: Sequence[datetime],
    bid: Sequence[float],
    ask: Sequence[float],
    mid: Sequence[float],
    *,
    start: datetime | None,
    end: datetime | None,
) -> tuple[list[datetime], list[float], list[float], list[float]]:
    """Keep rows with start <= t < end (end exclusive). None bound is open."""

    out_t: list[datetime] = []
    out_b: list[float] = []
    out_a: list[float] = []
    out_m: list[float] = []
    start_u = _dt(start) if start is not None else None
    end_u = _dt(end) if end is not None else None
    for ts, b, a, m in zip(times, bid, ask, mid, strict=True):
        instant = _dt(ts)
        if start_u is not None and instant < start_u:
            continue
        if end_u is not None and instant >= end_u:
            continue
        out_t.append(instant)
        out_b.append(b)
        out_a.append(a)
        out_m.append(m)
    return out_t, out_b, out_a, out_m


def _executable_nonoverlap_cell(
    report: Mapping[str, Any], horizon: int, threshold: float
) -> dict[str, Any]:
    payload = (
        ((report.get("executable_tob") or {}).get("non_overlapping") or {}).get(
            f"{horizon}s"
        )
        or {}
    )
    return (payload.get("thresholds") or {}).get(str(int(threshold))) or {}


def day_block_stability(
    times: Sequence[datetime],
    bid: Sequence[float],
    ask: Sequence[float],
    mid: Sequence[float],
    *,
    horizons: tuple[int, ...] = DAY_STABILITY_HORIZONS_SECONDS,
    thresholds: tuple[float, ...] = DAY_STABILITY_THRESHOLDS_BPS,
    tob_source: Sequence[str | None] | None = None,
    connection_id: Sequence[str | None] | None = None,
) -> dict[str, Any]:
    """Executable hit fractions per UTC day and per contiguous 1s block.

    Uses a predeclared subset of the frozen grids. Does not retune H or TP.
    """

    epoch = [int(_dt(ts).timestamp()) for ts in times]
    by_day: dict[str, list[int]] = {}
    for index, ts in enumerate(times):
        key = _dt(ts).date().isoformat()
        by_day.setdefault(key, []).append(index)

    def _slice(seq: Sequence[Any] | None, indices: Sequence[int]) -> list[Any] | None:
        if seq is None:
            return None
        return [seq[i] for i in indices]

    day_rows: list[dict[str, Any]] = []
    block_rows: list[dict[str, Any]] = []
    for day in sorted(by_day):
        indices = by_day[day]
        day_epoch = [epoch[i] for i in indices]
        day_bid = [bid[i] for i in indices]
        day_ask = [ask[i] for i in indices]
        day_mid = [mid[i] for i in indices]
        day_tob = _slice(tob_source, indices)
        day_conn = _slice(connection_id, indices)
        day_hours = len(day_epoch) / 3600.0
        day_stats = analyze_first_passage(
            day_epoch,
            day_bid,
            day_ask,
            day_mid,
            horizons=horizons,
            thresholds=thresholds,
            usable_hours=day_hours,
            tob_source=day_tob,
            connection_id=day_conn,
        )
        cells: dict[str, Any] = {}
        for horizon in horizons:
            for threshold in thresholds:
                cell = _executable_nonoverlap_cell(day_stats, horizon, threshold)
                either = cell.get("either_side_hit_fraction") or {}
                pooled = cell.get("pooled_descriptive_dependent") or {}
                cells[f"{horizon}s_{int(threshold)}bps"] = {
                    "either_side_hit_fraction_mean": either.get("mean"),
                    "either_side_hit_fraction_min": either.get("min"),
                    "either_side_hit_fraction_max": either.get("max"),
                    "per_offset": cell.get("per_offset"),
                    "pooled_descriptive_dependent": pooled,
                    "nonoverlap_hits_per_24_usable_hours": (
                        (cell.get("frequency") or {}).get(
                            "nonoverlap_hits_per_24_usable_hours"
                        )
                    ),
                    "observed_either_hit_count_sum_dependent": pooled.get(
                        "either_side_hit_count_sum"
                    ),
                    "observed_n_valid_starts_sum_dependent": pooled.get(
                        "n_valid_starts_sum"
                    ),
                }
        day_rows.append(
            {
                "utc_date": day,
                "n_rows": len(day_epoch),
                "usable_hours": day_hours,
                "n_contiguous_segments": day_stats["n_contiguous_segments"],
                "cells": cells,
            }
        )
        segments = split_contiguous_1s_segments(day_epoch)
        if len(segments) == 1 and segments[0] == (0, len(day_epoch)):
            # Identical to the UTC-day scan; do not pay for a second pass.
            block_rows.append(
                {
                    "utc_date": day,
                    "block_id": 0,
                    "n_rows": len(day_epoch),
                    "usable_hours": day_hours,
                    "skipped": None,
                    "copied_from_utc_day": True,
                    "cells": cells,
                }
            )
            continue
        for block_id, (lo, hi) in enumerate(segments):
            if hi - lo < horizons[-1] + 1:
                block_rows.append(
                    {
                        "utc_date": day,
                        "block_id": block_id,
                        "n_rows": hi - lo,
                        "usable_hours": (hi - lo) / 3600.0,
                        "skipped": "shorter_than_max_stability_horizon",
                    }
                )
                continue
            block_stats = analyze_first_passage(
                day_epoch[lo:hi],
                day_bid[lo:hi],
                day_ask[lo:hi],
                day_mid[lo:hi],
                horizons=horizons,
                thresholds=thresholds,
                usable_hours=(hi - lo) / 3600.0,
                tob_source=None if day_tob is None else day_tob[lo:hi],
                connection_id=None if day_conn is None else day_conn[lo:hi],
            )
            block_cells: dict[str, Any] = {}
            for horizon in horizons:
                for threshold in thresholds:
                    cell = _executable_nonoverlap_cell(block_stats, horizon, threshold)
                    either = cell.get("either_side_hit_fraction") or {}
                    block_cells[f"{horizon}s_{int(threshold)}bps"] = {
                        "either_side_hit_fraction_mean": either.get("mean"),
                        "per_offset": cell.get("per_offset"),
                    }
            block_rows.append(
                {
                    "utc_date": day,
                    "block_id": block_id,
                    "n_rows": hi - lo,
                    "usable_hours": (hi - lo) / 3600.0,
                    "skipped": None,
                    "cells": block_cells,
                }
            )

    distribution: dict[str, Any] = {}
    for horizon in horizons:
        for threshold in thresholds:
            key = f"{horizon}s_{int(threshold)}bps"
            values = [
                row["cells"][key]["either_side_hit_fraction_mean"]
                for row in day_rows
                if row.get("cells")
                and row["cells"].get(key)
                and row["cells"][key].get("either_side_hit_fraction_mean") is not None
            ]
            ordered = sorted(float(value) for value in values)
            distribution[key] = {
                "n_days": len(ordered),
                "min": ordered[0] if ordered else None,
                "median": _percentile(ordered, 0.50) if ordered else None,
                "max": ordered[-1] if ordered else None,
            }

    return {
        "horizons_seconds": list(horizons),
        "thresholds_bps": [int(t) for t in thresholds],
        "per_utc_day": day_rows,
        "per_contiguous_block": block_rows,
        "distribution_across_days": distribution,
        "aug6_question": (
            "Compare 2026-08-06 cells to min/median/max across discovery days "
            "to see whether that date is a typical regime or an outlier."
        ),
    }


def extract_exec_nonoverlap_snapshot(
    report: Mapping[str, Any],
    *,
    horizons: Sequence[int] = (60, 120, 300, 600),
    thresholds: Sequence[float] = (10.0, 15.0, 20.0, 25.0, 30.0),
) -> dict[str, Any]:
    """Small comparable slice of executable non-overlap cells (not a new grid)."""

    out: dict[str, Any] = {}
    for horizon in horizons:
        for threshold in thresholds:
            cell = _executable_nonoverlap_cell(report, int(horizon), float(threshold))
            either = cell.get("either_side_hit_fraction") or {}
            freq = cell.get("frequency") or {}
            pooled = cell.get("pooled_descriptive_dependent") or {}
            out[f"{int(horizon)}s_{int(threshold)}bps"] = {
                "either_side_hit_fraction_mean": either.get("mean"),
                "per_offset": cell.get("per_offset"),
                "observed_either_hit_count_sum_dependent": pooled.get(
                    "either_side_hit_count_sum"
                ),
                "observed_n_valid_starts_sum_dependent": pooled.get(
                    "n_valid_starts_sum"
                ),
                "nonoverlap_hits_per_24_usable_hours": freq.get(
                    "nonoverlap_hits_per_24_usable_hours"
                ),
            }
    return out


def _hits_over_n(offset_row: Mapping[str, Any] | None) -> str:
    if not offset_row:
        return "—"
    hits = offset_row.get("either_side_hit_count")
    n_valid = offset_row.get("n_valid_starts")
    if hits is None or n_valid is None:
        return "—"
    return f"{hits}/{n_valid}"


def _stability_cell_pct(cells: Mapping[str, Any], name: str) -> str:
    return _fmt_pct((cells.get(name) or {}).get("either_side_hit_fraction_mean"))


def _snapshot_hits_cell(block: Mapping[str, Any], cell_key: str) -> str:
    item = block.get(cell_key) or {}
    hits = item.get("nonoverlap_hits_per_24_usable_hours")
    n_hit = item.get("observed_either_hit_count_sum_dependent")
    frac = _fmt_pct(item.get("either_side_hit_fraction_mean"))
    return f"{frac} ({_fmt_num(hits, 2)}/24h, pooled_hits={n_hit})"


def render_full_corpus_markdown(report: Mapping[str, Any]) -> str:
    """Lead-facing expansion report. Does not overwrite the v1 opportunity doc."""

    corpus = report.get("corpus") or {}
    inventory = report.get("b2_inventory") or {}
    split = report.get("corpus_split") or {}
    cost = report.get("cost_layer") or {}
    stability = report.get("day_block_stability") or {}
    episodes = report.get("movement_episode_v1") or {}
    comparison = report.get("aug6_vs_expanded") or {}
    lines: list[str] = [
        str(report.get("report_title") or "# ETH first-passage full-corpus expansion v1"),
        "",
        f"STATUS: `{report.get('STATUS', 'ETH_FIRST_PASSAGE_FULL_CORPUS_EXPANSION_READY')}`",
        f"ML_STATUS: `{report.get('ML_STATUS', 'NOT_STARTED')}`",
        f"DECISION: `{report.get('DECISION', 'STOP_FOR_LEAD_REVIEW')}`",
        "",
        "## Method",
        "",
        "Repeats the frozen first-passage protocol from",
        "`docs/eth_first_passage_opportunity_v1.md` without changing horizons,",
        "thresholds, or path semantics. Mid MFE, executable TOB MFE, first",
        "passage, time-to-hit, MAE before first TP, rolling 1s, and four-offset",
        "non-overlap are unchanged. This expansion adds a live B2 COMPLETED",
        "inventory, an explicit discovery / untouched-OOS / future-holdout split,",
        "exact per-offset counts, UTC-day stability, and `movement_episode_v1`.",
        "OOS is not used to choose horizon or TP. No TP×SL grid.",
        "",
        "## B2 inventory (read-only)",
        "",
        f"- listing status: `{inventory.get('status')}`",
        f"- COMPLETED ETH windows: `{inventory.get('window_count')}`",
        f"- quarantined windows: `{inventory.get('quarantined_count')}`",
        f"- quality pass windows: `{inventory.get('quality_pass_count')}`",
        f"- UTC dates: `{inventory.get('utc_dates')}`",
        f"- B2 mutations: `{inventory.get('mutations')}`",
        f"- credential files loaded (names only): `{inventory.get('credential_filenames')}`",
        "",
        "Per-UTC-day eligible hours (non-quarantined COMPLETED overlap):",
        "",
        "| UTC date | windows | quarantined | pass | eligible hours | full (≥23h) |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for row in (inventory.get("utc_day_coverage") or {}).get("days") or []:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("utc_date")),
                    str(row.get("window_count")),
                    str(row.get("quarantined_window_count")),
                    str(row.get("quality_pass_window_count")),
                    _fmt_num(row.get("eligible_hours"), 2),
                    "yes" if row.get("full_utc_day") else "no",
                ]
            )
            + " |"
        )
    lines += [
        "",
        "## Corpus split (timestamps only; proposed to lead)",
        "",
        f"- v1 untouched OOS dates: `{split.get('v1_untouched_oos_utc_dates')}`",
        f"- expanded discovery dates: `{split.get('discovery_utc_dates')}`",
        (
            "- expanded discovery windows / hours est: `"
            f"{split.get('discovery_window_count')}` / `"
            f"{_fmt_num(split.get('discovery_covered_hours_est'), 2)}`"
        ),
        f"- new final holdout dates: `{split.get('new_holdout_utc_dates')}`",
        f"- new holdout applied: `{split.get('new_holdout_applied')}`",
        (
            "- thin holdout alternative (not applied): `"
            f"{((split.get('thin_holdout_alternative_not_applied') or {}).get('utc_dates'))}`"
        ),
        f"- lead alerts: `{split.get('lead_alerts')}`",
        "",
        split.get("note") or "",
        "",
        f"- materialized discovery usable hours: `{corpus.get('discovery_usable_hours')}`",
        f"- discovery rows: `{corpus.get('discovery_rows')}`",
        (
            "- first-passage materialized for holdout: `"
            f"{corpus.get('holdout_first_passage_materialized')}`"
        ),
        "",
        "## Cost layer (not mixed into movement stats)",
        "",
        f"- taker RT reference: `{cost.get('taker_rt_break_even_bps_reference')}` bps",
        f"- taker RT observed: `{cost.get('taker_rt_break_even_bps_observed')}` bps",
        f"- median spread: `{cost.get('median_spread_bps')}` bps",
        "",
        "## Mid either-side hit fraction (rolling 1s, dependent)",
        "",
        "| H | n | 5bps | 10 | 15 | 20 | 50 | 100 | MFE p50/p95 abs |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    mid_roll = ((report.get("mid") or {}).get("rolling_1s")) or {}
    for horizon in report.get("horizons_seconds") or FIRST_PASSAGE_HORIZONS_SECONDS:
        row = mid_roll.get(f"{horizon}s") or {}
        thr = row.get("thresholds") or {}
        mfe = (row.get("mfe_bps") or {}).get("abs_bps") or {}
        lines.append(
            "| "
            + " | ".join(
                [
                    f"{horizon}s",
                    str(row.get("n_valid_starts", "—")),
                    _fmt_pct((thr.get("5") or {}).get("either_side_hit_fraction")),
                    _fmt_pct((thr.get("10") or {}).get("either_side_hit_fraction")),
                    _fmt_pct((thr.get("15") or {}).get("either_side_hit_fraction")),
                    _fmt_pct((thr.get("20") or {}).get("either_side_hit_fraction")),
                    _fmt_pct((thr.get("50") or {}).get("either_side_hit_fraction")),
                    _fmt_pct((thr.get("100") or {}).get("either_side_hit_fraction")),
                    f"{_fmt_num(mfe.get('p50'))}/{_fmt_num(mfe.get('p95'))}",
                ]
            )
            + " |"
        )
    lines += [
        "",
        "## Executable TOB either-side (non-overlap): percent plus exact counts",
        "",
        "Each offset cell is `hits/n_valid`. Pooled sums are dependent (four",
        "phases of the same path) and are descriptive only. hits/24h is shown",
        "beside the observed offset-0 count, never instead of it.",
        "",
        "| H | TP | off0 | off H/4 | off H/2 | off 3H/4 | pooled hits/n (dependent) | hits/24h |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    ex_non = ((report.get("executable_tob") or {}).get("non_overlapping")) or {}
    for horizon in report.get("horizons_seconds") or FIRST_PASSAGE_HORIZONS_SECONDS:
        payload = ex_non.get(f"{horizon}s") or {}
        offsets = payload.get("offsets_seconds") or non_overlap_offsets(int(horizon))
        thr = payload.get("thresholds") or {}
        thresholds_bps = report.get("thresholds_bps") or FIRST_PASSAGE_THRESHOLDS_BPS
        for tp_key in (str(int(t)) for t in thresholds_bps):
            cell = thr.get(tp_key) or {}
            per = cell.get("per_offset") or {}
            pooled = cell.get("pooled_descriptive_dependent") or {}
            hits_24 = (cell.get("frequency") or {}).get(
                "nonoverlap_hits_per_24_usable_hours"
            )
            off_cells = [_hits_over_n(per.get(str(off))) for off in offsets]
            pooled_s = (
                f"{pooled.get('either_side_hit_count_sum')}/"
                f"{pooled.get('n_valid_starts_sum')}"
                if pooled
                else "—"
            )
            lines.append(
                "| "
                + " | ".join(
                    [f"{horizon}s", tp_key, *off_cells, pooled_s, _fmt_num(hits_24, 2)]
                )
                + " |"
            )
    lines += [
        "",
        "## Time-to-hit (mid, rolling, either-side, seconds)",
        "",
        "| H | TP | p25 | p50 | p75 | p90 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for horizon in (15, 60, 300, 600):
        row = mid_roll.get(f"{horizon}s") or {}
        for tp in ("10", "20", "50"):
            tth = ((row.get("thresholds") or {}).get(tp) or {}).get("first_hit_time_s") or {}
            either = tth.get("either") or {}
            if not (row.get("thresholds") or {}).get(tp):
                continue
            lines.append(
                f"| {horizon}s | {tp} | {_fmt_num(either.get('p25'))} | "
                f"{_fmt_num(either.get('p50'))} | {_fmt_num(either.get('p75'))} | "
                f"{_fmt_num(either.get('p90'))} |"
            )
    lines += [
        "",
        "## MAE before first TP (executable, non-overlap pooled, bps)",
        "",
        "| H | TP | long p50/p75/p90 | short p50/p75/p90 |",
        "|---|---:|---|---|",
    ]
    for horizon in (15, 60, 300, 600):
        payload = ex_non.get(f"{horizon}s") or {}
        for tp in ("10", "20", "50"):
            cell = (payload.get("thresholds") or {}).get(tp) or {}
            mae = cell.get("mae_before_first_tp_bps") or {}
            lng = mae.get("long") or {}
            sh = mae.get("short") or {}
            if not cell:
                continue
            long_mae = "/".join(_fmt_num(lng.get(name)) for name in ("p50", "p75", "p90"))
            short_mae = "/".join(_fmt_num(sh.get(name)) for name in ("p50", "p75", "p90"))
            lines.append(f"| {horizon}s | {tp} | {long_mae} | {short_mae} |")
    lines += [
        "",
        "## Day/block stability (executable non-overlap hit fraction)",
        "",
        "Predeclared slice: H ∈ {60,120,300,600}s and TP ∈ {10,15,20,25,30} bps.",
        "Question: is 2026-08-06 a typical regime or an anomaly?",
        "",
        "### Distribution across UTC days",
        "",
        "| cell | n_days | min | median | max |",
        "|---|---:|---:|---:|---:|",
    ]
    dist = stability.get("distribution_across_days") or {}
    for key in sorted(dist):
        item = dist[key]
        lines.append(
            f"| {key} | {item.get('n_days')} | {_fmt_pct(item.get('min'))} | "
            f"{_fmt_pct(item.get('median'))} | {_fmt_pct(item.get('max'))} |"
        )
    lines += [
        "",
        "### Per UTC day (executable either-side mean % @ TP 10/15/20/25/30)",
        "",
        "| date | hours | segs | 60s@20 | 120s@20 | 300s@20 | 600s@20 | 60s@10 | 600s@30 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in stability.get("per_utc_day") or []:
        cells = row.get("cells") or {}
        mark = " ← v1 discovery" if row.get("utc_date") == "2026-08-06" else ""
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("utc_date")) + mark,
                    _fmt_num(row.get("usable_hours"), 2),
                    str(row.get("n_contiguous_segments")),
                    _stability_cell_pct(cells, "60s_20bps"),
                    _stability_cell_pct(cells, "120s_20bps"),
                    _stability_cell_pct(cells, "300s_20bps"),
                    _stability_cell_pct(cells, "600s_20bps"),
                    _stability_cell_pct(cells, "60s_10bps"),
                    _stability_cell_pct(cells, "600s_30bps"),
                ]
            )
            + " |"
        )
    lines += [
        "",
        "Raw per-day cells for the full stability slice, including exact per-offset",
        "counts, are in the JSON under `day_block_stability.per_utc_day`.",
        "Contiguous 1s blocks are under `per_contiguous_block`.",
        "",
        "## movement_episode_v1 (diagnostic, does not replace non-overlap)",
        "",
        (episodes.get("algorithm") or {}).get("note")
        or "See JSON for the predeclared 1-second adjacency rule.",
        "",
        "| H | TP | mid long ep | mid short | exec long | exec short | mid long /day |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    mid_ep = (episodes.get("mid") or {})
    ex_ep = (episodes.get("executable_tob") or {})
    for horizon in (60, 120, 300, 600):
        for tp in ("10", "15", "20", "25", "30"):
            m_thr = ((mid_ep.get(f"{horizon}s") or {}).get("thresholds") or {}).get(tp) or {}
            e_thr = ((ex_ep.get(f"{horizon}s") or {}).get("thresholds") or {}).get(tp) or {}
            if not m_thr and not e_thr:
                continue
            m_long = m_thr.get("long") or {}
            m_short = m_thr.get("short") or {}
            e_long = e_thr.get("long") or {}
            e_short = e_thr.get("short") or {}
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"{horizon}s",
                        tp,
                        str(m_long.get("episode_count", "—")),
                        str(m_short.get("episode_count", "—")),
                        str(e_long.get("episode_count", "—")),
                        str(e_short.get("episode_count", "—")),
                        _fmt_num(m_long.get("episodes_per_usable_day"), 2),
                    ]
                )
                + " |"
            )
    lines += [
        "",
        "## Aug-6-only vs expanded discovery",
        "",
        comparison.get("narrative") or "",
        "",
        "| cell | v1 Aug-6-only (11.64h) | expanded Aug-6 subset | expanded discovery |",
        "|---|---:|---:|---:|",
    ]
    v1_cells = comparison.get("v1_aug6") or {}
    aug6_cells = comparison.get("expanded_aug6") or {}
    exp_cells = comparison.get("expanded_discovery") or {}
    for key in ("60s_20bps", "120s_20bps", "300s_20bps", "600s_20bps", "60s_10bps", "600s_30bps"):
        lines.append(
            "| "
            + " | ".join(
                [
                    key,
                    _snapshot_hits_cell(v1_cells, key),
                    _snapshot_hits_cell(aug6_cells, key),
                    _snapshot_hits_cell(exp_cells, key),
                ]
            )
            + " |"
        )
    lines += [
        "",
        "### What looks stable / changed / sample artifact",
        "",
    ]
    for item in comparison.get("findings") or []:
        lines.append(f"- {item}")
    if not comparison.get("findings"):
        lines.append("- See JSON `aug6_vs_expanded.findings` after the scan.")
    cells = report.get("economic_frequency_commentary") or []
    lines += [
        "",
        "## Frequency vs friction (not predictability)",
        "",
        "Predeclared display band unchanged: executable TP ≥ taker RT reference and",
        f"non-overlap mean hits/24h ≥ {PREDECLARED_FREQUENCY_COMMENTARY_HITS_PER_24H}.",
        "Not a fitted threshold and not a trading signal.",
        "",
    ]
    if cells:
        for item in cells:
            lines.append(
                f"- {item.get('horizon')} TP {item.get('threshold_bps')} bps: "
                f"~{_fmt_num(item.get('nonoverlap_hits_per_24_usable_hours'))} "
                "non-overlap windows/24h (frequency only)."
            )
    else:
        lines.append("- No predeclared commentary cells cleared the frozen frequency band.")
    runtime = report.get("runtime") or {}
    blockers = report.get("data_quality_blockers") or []
    lines += [
        "",
        "## Runtime",
        "",
        f"- wall seconds: `{runtime.get('wall_seconds')}`",
        f"- peak RSS MiB: `{runtime.get('peak_rss_mib')}`",
        f"- tracemalloc peak MiB: `{runtime.get('tracemalloc_peak_mib')}`",
        "",
        "## Data-quality blockers",
        "",
    ]
    if blockers:
        for item in blockers:
            lines.append(f"- {item}")
    else:
        lines.append("- none recorded")
    lines += [
        "",
        "## Stop",
        "",
        "STOP_FOR_LEAD_REVIEW. No ML, no feature selection, no PAPER, no live trading.",
        "Do not retune first-passage grids from these results.",
        "",
    ]
    return "\n".join(lines) + "\n"
