"""Unconditional executable TP-before-SL feasibility (no ML, no trading).

Frozen grids and first-touch semantics are fixed before any corpus scan.
Executable long is entry ask → future bid; executable short is entry bid →
future ask. Spread is already inside that gross path and must not be subtracted
again in net PnL.

1s data cannot order two barriers that both sit inside one consecutive-print
interval: that window is AMBIGUOUS, never an invented intra-second winner.
"""

from __future__ import annotations

import json
import math
from array import array
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trading_bot.research.collection_gaps import CollectionGap
from trading_bot.research.pipeline.cost_evidence import (
    funding_contribution_bps,
    hibachi_public_fee_schedule,
    round_trip_friction_bps,
)
from trading_bot.research.pipeline.executable_tob import is_executable_tob_source
from trading_bot.research.pipeline.first_passage_corpus import (
    V1_UNTOUCHED_OOS_UTC_DATES,
    parse_dataset_id_window,
)
from trading_bot.research.pipeline.first_passage_opportunity import (
    _percentile,
    executable_prices_ok,
    non_overlap_offsets,
    split_contiguous_1s_segments,
)

TP_SL_PROTOCOL_NAME = "eth_tp_sl_first_touch_feasibility_v1"
TP_SL_PROTOCOL_VERSION = 1

# Frozen before any TP×SL scan. Do not retune after seeing EV or hit rates.
PRIMARY_HORIZONS_SECONDS: tuple[int, ...] = (120, 180, 300)
CONTROL_HORIZONS_SECONDS: tuple[int, ...] = (60, 600)
ALL_HORIZONS_SECONDS: tuple[int, ...] = (60, 120, 180, 300, 600)
TP_THRESHOLDS_BPS: tuple[float, ...] = (20.0, 25.0, 30.0)
SL_THRESHOLDS_BPS: tuple[float, ...] = (5.0, 10.0, 15.0, 20.0)
EXECUTION_DELAYS_SECONDS: tuple[int, ...] = (0, 1, 2)
LATENCY_BPS_PER_SIDE_GRID: tuple[float, ...] = (0.0, 1.0, 2.0)
CURRENT_MODELED_LATENCY_BPS_PER_SIDE = 1.0
DIRECTIONS: tuple[str, ...] = ("long", "short")
OUTCOMES: tuple[str, ...] = (
    "TP_FIRST",
    "SL_FIRST",
    "TIMEOUT",
    "AMBIGUOUS",
    "DATA_INVALID",
)

# Forensic appendix only: not part of the TP×SL grid and not a retune.
FORENSIC_SUBMINUTE_HORIZONS_SECONDS: tuple[int, ...] = (5, 10, 15, 30, 60)
FORENSIC_EXCURSION_THRESHOLDS_BPS: tuple[float, ...] = (50.0, 75.0, 100.0)
FORENSIC_MAE_TP_BPS = 20.0
FORENSIC_MAE_TAIL_BPS = 50.0
# Predeclared: escalate if stale/quote-fallback resolutions exceed this share
# of TP_FIRST+SL_FIRST in any primary (H, TP, SL, direction) cell.
PRIMARY_STALE_RESOLUTION_MAX_FRACTION = 0.05
# Predeclared: escalate if this many of the top unique 50bps+ forensic peaks
# carry stale/quote-fallback/mark-divergence tags (not fitted from results).
FORENSIC_TOP_FLAGGED_ESCALATE_COUNT = 5
MARK_DIVERGENCE_BPS = 25.0
STALE_BOOK_SECONDS = 5.0
FORENSIC_BAD_TAGS = frozenset(
    {
        "QUOTE_FALLBACK_OR_INVALID_BOOK",
        "STALE_BOOK",
        "MARK_QUOTE_DIVERGENCE",
    }
)


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _utc_date(epoch_s: int) -> str:
    return datetime.fromtimestamp(int(epoch_s), UTC).date().isoformat()


def _pct(samples: array[float], q: float) -> float | None:
    if not samples:
        return None
    return _percentile(sorted(samples), q)


def _mean(samples: array[float]) -> float | None:
    return (sum(samples) / len(samples)) if samples else None


def _rate(count: int, n: int) -> float | None:
    return (count / n) if n else None


def discovery_dates_from_full_corpus_doc(path: Path) -> list[str]:
    """Reuse the accepted full-corpus v1 discovery split. Never add OOS dates."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    corpus = payload.get("corpus") or {}
    dates = [str(item) for item in corpus.get("discovery_utc_dates") or []]
    banned = set(V1_UNTOUCHED_OOS_UTC_DATES)
    leaked = [day for day in dates if day in banned]
    if leaked:
        raise ValueError(f"discovery dates include untouched OOS: {leaked}")
    if not dates:
        raise ValueError("full-corpus document has no discovery_utc_dates")
    return dates


def barrier_interval_hits(
    prev_exec_bps: float,
    curr_exec_bps: float,
    *,
    tp_bps: float,
    sl_bps: float,
) -> tuple[bool, bool]:
    """Whether consecutive 1s prints' interval contains +TP and/or -SL."""

    lo = prev_exec_bps if prev_exec_bps <= curr_exec_bps else curr_exec_bps
    hi = curr_exec_bps if prev_exec_bps <= curr_exec_bps else prev_exec_bps
    hit_tp = lo <= tp_bps <= hi
    hit_sl = lo <= -sl_bps <= hi
    return hit_tp, hit_sl


def resolve_step(
    prev_exec_bps: float,
    curr_exec_bps: float,
    *,
    tp_bps: float,
    sl_bps: float,
) -> str | None:
    """First-touch outcome for one 1s step, or None if neither barrier.

    Linear interpolation uses the closed interval between prints. If that
    interval contains both barriers, the step is AMBIGUOUS. If it contains
    only one barrier but ``|Δ| >= TP+SL``, the unidentified path could still
    have reached the other barrier first — also AMBIGUOUS. Never invent
    intra-second order.
    """

    hit_tp, hit_sl = barrier_interval_hits(
        prev_exec_bps, curr_exec_bps, tp_bps=tp_bps, sl_bps=sl_bps
    )
    if not hit_tp and not hit_sl:
        return None
    span = abs(curr_exec_bps - prev_exec_bps)
    if hit_tp and hit_sl:
        return "AMBIGUOUS"
    if span + 1e-12 >= float(tp_bps) + float(sl_bps):
        return "AMBIGUOUS"
    if hit_tp:
        return "TP_FIRST"
    return "SL_FIRST"


def entry_barrier_outcome(
    exec_at_entry_bps: float, *, tp_bps: float, sl_bps: float
) -> str | None:
    """Instantaneous fill vs TOB, or None if neither barrier is already through."""

    if not math.isfinite(exec_at_entry_bps):
        return "AMBIGUOUS"
    hit_tp = exec_at_entry_bps >= float(tp_bps)
    hit_sl = exec_at_entry_bps <= -float(sl_bps)
    if hit_tp and hit_sl:
        return "AMBIGUOUS"
    if hit_sl:
        return "SL_FIRST"
    if hit_tp:
        return "TP_FIRST"
    return None


def classify_executable_path(
    exec_at_entry_bps: float,
    exec_by_lag_bps: Sequence[float],
    *,
    tp_bps: float,
    sl_bps: float,
) -> tuple[str, int | None, float | None]:
    """Classify one direction on a stored executable path (lag 1 = first print).

    Returns (outcome, resolve_lag_or_none, realized_gross_bps_or_none).
    TIMEOUT realized is the executable return at H. AMBIGUOUS has no fill.
    """

    if not math.isfinite(exec_at_entry_bps):
        return "AMBIGUOUS", 0, None
    prev = float(exec_at_entry_bps)
    # Instantaneous mark-to-TOB at fill (the spread). This is not a 1s interval.
    entry = entry_barrier_outcome(prev, tp_bps=tp_bps, sl_bps=sl_bps)
    if entry == "AMBIGUOUS":
        return "AMBIGUOUS", 0, None
    if entry == "TP_FIRST":
        return "TP_FIRST", 0, prev
    if entry == "SL_FIRST":
        return "SL_FIRST", 0, prev
    last_finite: float | None = prev
    for lag, raw in enumerate(exec_by_lag_bps, start=1):
        curr = float(raw)
        if not math.isfinite(curr):
            return "AMBIGUOUS", lag, None
        last_finite = curr
        step = resolve_step(prev, curr, tp_bps=tp_bps, sl_bps=sl_bps)
        if step == "AMBIGUOUS":
            return "AMBIGUOUS", lag, None
        if step == "TP_FIRST":
            return "TP_FIRST", lag, curr
        if step == "SL_FIRST":
            return "SL_FIRST", lag, curr
        prev = curr
    return "TIMEOUT", None, last_finite


def audit_cost_decomposition(
    *,
    median_spread_bps: float | None,
    holding_seconds: float,
    latency_bps_per_side: float = CURRENT_MODELED_LATENCY_BPS_PER_SIDE,
) -> dict[str, Any]:
    """Split fee / spread / latency so ask→bid spread is not charged twice.

    Executable gross already uses entry ask and exit bid (or the short
    reverse). Adding ``spread_bps_round_trip`` on top of that gross would
    double-count the observed TOB spread.
    """

    fees = hibachi_public_fee_schedule()
    taker = float(fees["tier1_taker_fee_rate"])
    fee_rt = 2.0 * taker * 10_000.0
    latency_rt = 2.0 * float(latency_bps_per_side)
    funding = funding_contribution_bps(float(holding_seconds))
    spread = float(median_spread_bps or 0.0)
    extra = fee_rt + latency_rt + funding
    legacy = round_trip_friction_bps(
        taker_fee_rate=taker,
        slippage_bps_per_side=0.0,
        latency_bps_per_side=float(latency_bps_per_side),
        spread_bps_round_trip=spread,
        funding_bps=funding,
    )
    return {
        "fee_round_trip_bps": fee_rt,
        "spread_bps_observed_median": median_spread_bps,
        "spread_already_in_executable_gross": True,
        "subtract_spread_again_from_net": False,
        "latency_bps_per_side": float(latency_bps_per_side),
        "latency_round_trip_bps": latency_rt,
        "funding_bps": funding,
        "holding_seconds": float(holding_seconds),
        "extra_cost_bps_excluding_spread": extra,
        "legacy_round_trip_friction_bps": legacy,
        "legacy_round_trip_includes_spread": True,
        "tier1_taker_fee_rate": taker,
        "fee_source": fees["source_url"],
        "note": (
            "Executable ask→bid (long) or bid→ask (short) already embeds the "
            "round-trip TOB spread in gross bps. Net PnL subtracts fee, modeled "
            "latency, and funding only. Do not double-count spread by also "
            "subtracting the legacy ~11 bps all-in RT from executable gross."
        ),
    }


def extra_cost_bps(
    *,
    holding_seconds: float,
    latency_bps_per_side: float,
) -> float:
    """Fee + latency + funding. Spread stays in executable gross."""

    return float(
        audit_cost_decomposition(
            median_spread_bps=0.0,
            holding_seconds=holding_seconds,
            latency_bps_per_side=latency_bps_per_side,
        )["extra_cost_bps_excluding_spread"]
    )


def economics_for_cell(
    *,
    tp_bps: float,
    sl_bps: float,
    n_valid: int,
    n_tp_first: int,
    n_sl_first: int,
    n_timeout: int,
    n_ambiguous: int,
    mean_gross_tp: float | None,
    mean_gross_sl: float | None,
    mean_gross_timeout: float | None,
    extra_cost_bps: float,
) -> dict[str, Any]:
    """Unconditional EV, break-even TP-first probability, lift, payoff ratio.

    AMBIGUOUS starts are not traded (no causal fill). EV uses TP/SL/TIMEOUT
    only. This is a feasibility surface, not a selected strategy.
    """

    n_tradeable = n_tp_first + n_sl_first + n_timeout
    p_tp = _rate(n_tp_first, n_valid)
    p_sl = _rate(n_sl_first, n_valid)
    p_to = _rate(n_timeout, n_valid)
    p_amb = _rate(n_ambiguous, n_valid)
    p_tp_tradeable = _rate(n_tp_first, n_tradeable)
    p_to_tradeable = _rate(n_timeout, n_tradeable)
    e_tp = mean_gross_tp if mean_gross_tp is not None else float(tp_bps)
    e_sl = mean_gross_sl if mean_gross_sl is not None else -float(sl_bps)
    e_to = mean_gross_timeout if mean_gross_timeout is not None else 0.0
    if n_tradeable:
        gross_ev = (
            n_tp_first * e_tp + n_sl_first * e_sl + n_timeout * e_to
        ) / n_tradeable
        net_ev = gross_ev - extra_cost_bps
    else:
        gross_ev = None
        net_ev = None
    denom = float(tp_bps) + float(sl_bps)
    p_be_barrier = (
        (float(sl_bps) + extra_cost_bps) / denom if denom > 0 else None
    )
    p_be_timeout: float | None = None
    if n_tradeable and abs(e_tp - e_sl) > 1e-12:
        p_to_w = float(p_to_tradeable or 0.0)
        p_be_timeout = (
            extra_cost_bps - e_sl - p_to_w * (e_to - e_sl)
        ) / (e_tp - e_sl)
    payoff = (float(tp_bps) / float(sl_bps)) if sl_bps else None
    realized_payoff = (
        abs(e_tp / e_sl) if e_sl not in (None, 0.0) and e_tp is not None else None
    )
    p_star = p_be_barrier
    base_rate = p_tp_tradeable if p_tp_tradeable is not None else p_tp
    lift_abs = (
        (p_star - base_rate) if p_star is not None and base_rate is not None else None
    )
    lift_rel = (
        (p_star / base_rate)
        if p_star is not None and base_rate is not None and base_rate != 0.0
        else None
    )
    return {
        "n_valid_starts": n_valid,
        "n_tp_first": n_tp_first,
        "n_sl_first": n_sl_first,
        "n_timeout": n_timeout,
        "n_ambiguous": n_ambiguous,
        "n_tradeable_excludes_ambiguous": n_tradeable,
        "unconditional_tp_first_rate": p_tp,
        "unconditional_sl_first_rate": p_sl,
        "unconditional_timeout_rate": p_to,
        "unconditional_ambiguous_rate": p_amb,
        "tp_first_rate_among_tradeable": p_tp_tradeable,
        "mean_gross_tp_bps": mean_gross_tp,
        "mean_gross_sl_bps": mean_gross_sl,
        "mean_gross_timeout_bps": mean_gross_timeout,
        "extra_cost_bps_excluding_spread": extra_cost_bps,
        "unconditional_gross_ev_bps": gross_ev,
        "unconditional_net_ev_bps": net_ev,
        "break_even_tp_first_prob_two_outcome_barrier": p_be_barrier,
        "break_even_tp_first_prob_holding_timeout_mix": p_be_timeout,
        "required_precision_tp_first": p_star,
        "unconditional_tp_first_base_rate_tradeable": p_tp_tradeable,
        "required_lift_abs": lift_abs,
        "required_lift_rel": lift_rel,
        "payoff_ratio_barrier_tp_over_sl": payoff,
        "payoff_ratio_realized_abs": realized_payoff,
        "note": (
            "Unconditional EV trades every causal start (TP/SL/TIMEOUT). "
            "AMBIGUOUS starts are not filled. A future selector needs TP-first "
            "precision at least break_even_tp_first_prob_two_outcome_barrier "
            "among accepted starts. Lift is vs the unconditional tradeable "
            "TP-first base rate. Not an optimized rule."
        ),
    }


@dataclass
class _OutAcc:
    n_valid: int = 0
    n_tp: int = 0
    n_sl: int = 0
    n_to: int = 0
    n_amb: int = 0
    n_data_invalid: int = 0
    n_resolved_on_stale: int = 0
    tp_times: array[float] = field(default_factory=lambda: array("d"))
    sl_times: array[float] = field(default_factory=lambda: array("d"))
    tp_gross: array[float] = field(default_factory=lambda: array("d"))
    sl_gross: array[float] = field(default_factory=lambda: array("d"))
    to_gross: array[float] = field(default_factory=lambda: array("d"))

    def add(
        self,
        outcome: str,
        *,
        lag: int | None,
        realized: float | None,
        stale_resolve: bool,
    ) -> None:
        if outcome == "DATA_INVALID":
            self.n_data_invalid += 1
            return
        self.n_valid += 1
        if outcome == "TP_FIRST":
            self.n_tp += 1
            if lag is not None:
                self.tp_times.append(float(lag))
            if realized is not None:
                self.tp_gross.append(float(realized))
            if stale_resolve:
                self.n_resolved_on_stale += 1
        elif outcome == "SL_FIRST":
            self.n_sl += 1
            if lag is not None:
                self.sl_times.append(float(lag))
            if realized is not None:
                self.sl_gross.append(float(realized))
            if stale_resolve:
                self.n_resolved_on_stale += 1
        elif outcome == "TIMEOUT":
            self.n_to += 1
            if realized is not None:
                self.to_gross.append(float(realized))
        else:
            self.n_amb += 1

    def counts(self) -> dict[str, int]:
        return {
            "n_valid_starts": self.n_valid,
            "n_tp_first": self.n_tp,
            "n_sl_first": self.n_sl,
            "n_timeout": self.n_to,
            "n_ambiguous": self.n_amb,
            "n_data_invalid": self.n_data_invalid,
            "n_tp_or_sl_resolved_on_stale_or_quote_fallback": self.n_resolved_on_stale,
        }

    def time_block(self) -> dict[str, Any]:
        return {
            "tp_first_time_s": {
                "p25": _pct(self.tp_times, 0.25),
                "p50": _pct(self.tp_times, 0.50),
                "p75": _pct(self.tp_times, 0.75),
                "p90": _pct(self.tp_times, 0.90),
            },
            "sl_first_time_s": {
                "p25": _pct(self.sl_times, 0.25),
                "p50": _pct(self.sl_times, 0.50),
                "p75": _pct(self.sl_times, 0.75),
                "p90": _pct(self.sl_times, 0.90),
            },
            "mean_timeout_gross_bps": _mean(self.to_gross),
            "timeout_gross_bps": {
                "p25": _pct(self.to_gross, 0.25),
                "p50": _pct(self.to_gross, 0.50),
                "p75": _pct(self.to_gross, 0.75),
                "p90": _pct(self.to_gross, 0.90),
            },
        }


def _empty_acc_grid(
    horizons: tuple[int, ...],
    tps: tuple[float, ...],
    sls: tuple[float, ...],
) -> dict[int, dict[float, dict[float, dict[str, _OutAcc]]]]:
    return {
        h: {tp: {sl: {d: _OutAcc() for d in DIRECTIONS} for sl in sls} for tp in tps}
        for h in horizons
    }


def _thr_key(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)


def _mark_unresolved_invalid(
    resolved: dict[tuple[float, float, str], tuple[str, int, float | None] | None],
    open_count: int,
    lag: int,
) -> int:
    """Tag remaining barriers as DATA_INVALID. Returns the updated open count."""

    for key, val in list(resolved.items()):
        if val is None:
            resolved[key] = ("DATA_INVALID", lag, None)
            open_count -= 1
    return open_count


def _quality_stale(
    index: int,
    valid_book: Sequence[bool] | None,
    book_age: Sequence[float | None] | None,
    tob_source: Sequence[str | None] | None = None,
) -> bool:
    if tob_source is not None and index < len(tob_source):
        return not is_executable_tob_source(tob_source[index])
    if valid_book is not None and index < len(valid_book) and not valid_book[index]:
        return True
    if book_age is not None and index < len(book_age):
        age = book_age[index]
        if age is not None and math.isfinite(float(age)) and float(age) > STALE_BOOK_SECONDS:
            return True
    return False


def _record_horizon(
    acc: _OutAcc,
    *,
    resolved: tuple[str, int, float | None] | None,
    k: int,
    exec_now: float,
    stale: bool,
) -> None:
    if resolved is not None and resolved[1] <= k:
        outcome, lag, realized = resolved
        if outcome == "DATA_INVALID":
            acc.add("DATA_INVALID", lag=lag, realized=None, stale_resolve=False)
            return
        stale_hit = stale and outcome in {"TP_FIRST", "SL_FIRST"}
        acc.add(outcome, lag=lag, realized=realized, stale_resolve=stale_hit)
        return
    acc.add("TIMEOUT", lag=None, realized=exec_now, stale_resolve=False)


def analyze_tp_sl_first_touch(
    epoch_s: Sequence[int],
    bid: Sequence[float],
    ask: Sequence[float],
    mid: Sequence[float],
    *,
    horizons: tuple[int, ...] = ALL_HORIZONS_SECONDS,
    tp_grid: tuple[float, ...] = TP_THRESHOLDS_BPS,
    sl_grid: tuple[float, ...] = SL_THRESHOLDS_BPS,
    delays_seconds: tuple[int, ...] = (0,),
    rolling_delays: tuple[int, ...] | None = None,
    latency_bps_per_side_grid: tuple[float, ...] = LATENCY_BPS_PER_SIDE_GRID,
    median_spread_bps: float | None = None,
    valid_book: Sequence[bool] | None = None,
    book_age_seconds: Sequence[float | None] | None = None,
    tob_source: Sequence[str | None] | None = None,
    executable_tob: Sequence[bool] | None = None,
    connection_id: Sequence[str | None] | None = None,
) -> dict[str, Any]:
    """Scan contiguous 1s segments for executable TP/SL first-touch outcomes."""

    n = len(epoch_s)
    if not (len(bid) == len(ask) == len(mid) == n):
        raise ValueError("epoch_s, bid, ask, and mid must have equal length")
    horizons = tuple(sorted(int(h) for h in horizons))
    tps = tuple(float(x) for x in tp_grid)
    sls = tuple(float(x) for x in sl_grid)
    delays = tuple(int(d) for d in delays_seconds)
    rolling_delay_set = set(delays if rolling_delays is None else rolling_delays)
    min_h = horizons[0]
    max_h = horizons[-1]
    horizon_set = set(horizons)
    pairs = [(tp, sl) for tp in tps for sl in sls]

    rolling: dict[int, dict[int, dict[float, dict[float, dict[str, _OutAcc]]]]] = {
        d: _empty_acc_grid(horizons, tps, sls) for d in delays
    }
    offset_acc: dict[
        int, dict[int, dict[int, dict[float, dict[float, dict[str, _OutAcc]]]]]
    ] = {}
    day_acc: dict[
        str, dict[int, dict[float, dict[float, dict[str, _OutAcc]]]]
    ] = {}
    for delay in delays:
        offset_acc[delay] = {}
        for horizon in horizons:
            offs = non_overlap_offsets(horizon)
            offset_acc[delay][horizon] = {
                off: {
                    tp: {sl: {d: _OutAcc() for d in DIRECTIONS} for sl in sls}
                    for tp in tps
                }
                for off in offs
            }

    def _ensure_day(day: str, horizon: int) -> None:
        day_acc.setdefault(day, {})
        if horizon not in day_acc[day]:
            day_acc[day][horizon] = {
                tp0: {sl0: {d: _OutAcc() for d in DIRECTIONS} for sl0 in sls}
                for tp0 in tps
            }

    def _flush(
        *,
        delay: int,
        local_e: int,
        record_rolling: bool,
        day: str,
        resolved: dict[tuple[float, float, str], tuple[str, int, float | None] | None],
        stale_at: dict[int, bool],
        last_long: float,
        last_short: float,
        horizons_to_flush: Sequence[int],
    ) -> None:
        for horizon in horizons_to_flush:
            offs = non_overlap_offsets(horizon)
            is_nonoverlap = {
                off: local_e >= off and (local_e - off) % horizon == 0 for off in offs
            }
            for tp, sl in pairs:
                for direction in DIRECTIONS:
                    exec_now = last_long if direction == "long" else last_short
                    item = resolved[(tp, sl, direction)]
                    stale = False
                    if item is not None and item[0] in {"TP_FIRST", "SL_FIRST"}:
                        stale = bool(stale_at.get(item[1], False))
                    if record_rolling:
                        _record_horizon(
                            rolling[delay][horizon][tp][sl][direction],
                            resolved=item,
                            k=horizon,
                            exec_now=exec_now,
                            stale=stale,
                        )
                    for off, hit in is_nonoverlap.items():
                        if not hit:
                            continue
                        _record_horizon(
                            offset_acc[delay][horizon][off][tp][sl][direction],
                            resolved=item,
                            k=horizon,
                            exec_now=exec_now,
                            stale=stale,
                        )
                        if delay == 0 and off == 0:
                            _ensure_day(day, horizon)
                            _record_horizon(
                                day_acc[day][horizon][tp][sl][direction],
                                resolved=item,
                                k=horizon,
                                exec_now=exec_now,
                                stale=stale,
                            )

    def _row_ok(index: int) -> bool:
        if index < 0 or index >= n:
            return False
        if not executable_prices_ok(bid[index], ask[index], mid[index]):
            return False
        if executable_tob is not None:
            return bool(executable_tob[index])
        if tob_source is not None:
            return is_executable_tob_source(tob_source[index])
        return True

    def _conn_break(start: int, current: int) -> bool:
        if connection_id is None:
            return False
        left = connection_id[start]
        right = connection_id[current]
        if left is None or right is None:
            return False
        return left != right

    segments = split_contiguous_1s_segments(epoch_s)
    for lo, hi in segments:
        length = hi - lo
        if length < 2:
            continue
        for local in range(length):
            remain = length - 1 - local
            i = lo + local
            if not _row_ok(i):
                continue
            day = _utc_date(int(epoch_s[i]))
            for delay in delays:
                ie = i + delay
                if ie >= hi or not _row_ok(ie):
                    continue
                if delay and _conn_break(i, ie):
                    continue
                local_e = local + delay
                remain_e = remain - delay
                record_rolling = delay in rolling_delay_set
                if not record_rolling:
                    candidate = False
                    for horizon in horizons:
                        for off in non_overlap_offsets(horizon):
                            if local_e >= off and (local_e - off) % horizon == 0:
                                candidate = True
                                break
                        if candidate:
                            break
                    if not candidate:
                        continue
                bid0 = bid[ie]
                ask0 = ask[ie]
                prev_long = (bid0 / ask0 - 1.0) * 10_000.0
                prev_short = (bid0 / ask0 - 1.0) * 10_000.0
                resolved: dict[
                    tuple[float, float, str], tuple[str, int, float | None] | None
                ] = {(tp, sl, d): None for tp, sl in pairs for d in DIRECTIONS}
                open_count = len(resolved)
                scan_h = min(max_h, max(remain_e, 0))
                last_long = prev_long
                last_short = prev_short
                stale_at: dict[int, bool] = {
                    0: _quality_stale(
                        ie, valid_book, book_age_seconds, tob_source=tob_source
                    )
                }
                path_broke = remain_e < min_h
                if path_broke:
                    open_count = _mark_unresolved_invalid(
                        resolved, open_count, max(remain_e, 0) + 1
                    )
                    _flush(
                        delay=delay,
                        local_e=local_e,
                        record_rolling=record_rolling,
                        day=day,
                        resolved=resolved,
                        stale_at=stale_at,
                        last_long=last_long,
                        last_short=last_short,
                        horizons_to_flush=list(horizons),
                    )
                    continue
                for tp, sl in pairs:
                    for direction, px in (("long", prev_long), ("short", prev_short)):
                        entry = entry_barrier_outcome(px, tp_bps=tp, sl_bps=sl)
                        if entry is None:
                            continue
                        realized = None if entry == "AMBIGUOUS" else px
                        resolved[(tp, sl, direction)] = (entry, 0, realized)
                        open_count -= 1
                if open_count == 0:
                    _flush(
                        delay=delay,
                        local_e=local_e,
                        record_rolling=record_rolling,
                        day=day,
                        resolved=resolved,
                        stale_at=stale_at,
                        last_long=last_long,
                        last_short=last_short,
                        horizons_to_flush=[h for h in horizons if h <= scan_h],
                    )
                    unobs = [h for h in horizons if h > scan_h]
                    if unobs:
                        open_count = _mark_unresolved_invalid(
                            resolved, open_count, scan_h + 1
                        )
                        _flush(
                            delay=delay,
                            local_e=local_e,
                            record_rolling=record_rolling,
                            day=day,
                            resolved=resolved,
                            stale_at=stale_at,
                            last_long=last_long,
                            last_short=last_short,
                            horizons_to_flush=unobs,
                        )
                    continue
                broke = False
                for k in range(1, scan_h + 1):
                    j = ie + k
                    if (
                        not _row_ok(j)
                        or _conn_break(ie, j)
                    ):
                        # Gap, stale/fallback, or reconnect: not TIMEOUT.
                        open_count = _mark_unresolved_invalid(
                            resolved, open_count, k
                        )
                        _flush(
                            delay=delay,
                            local_e=local_e,
                            record_rolling=record_rolling,
                            day=day,
                            resolved=resolved,
                            stale_at=stale_at,
                            last_long=last_long,
                            last_short=last_short,
                            horizons_to_flush=[h for h in horizons if h >= k],
                        )
                        broke = True
                        break
                    exec_long = (bid[j] / ask0 - 1.0) * 10_000.0
                    exec_short = (bid0 / ask[j] - 1.0) * 10_000.0
                    last_long = exec_long
                    last_short = exec_short
                    stale_at[k] = _quality_stale(
                        j, valid_book, book_age_seconds, tob_source=tob_source
                    )
                    if open_count:
                        for tp, sl in pairs:
                            for direction, prev, curr in (
                                ("long", prev_long, exec_long),
                                ("short", prev_short, exec_short),
                            ):
                                key = (tp, sl, direction)
                                if resolved[key] is not None:
                                    continue
                                step = resolve_step(prev, curr, tp_bps=tp, sl_bps=sl)
                                if step is None:
                                    continue
                                realized = None if step == "AMBIGUOUS" else curr
                                resolved[key] = (step, k, realized)
                                open_count -= 1
                    prev_long = exec_long
                    prev_short = exec_short
                    emit_horizons: list[int] = []
                    if k in horizon_set:
                        emit_horizons.append(k)
                    if open_count == 0:
                        emit_horizons.extend(
                            h for h in horizons if h > k and h <= scan_h
                        )
                    if emit_horizons:
                        _flush(
                            delay=delay,
                            local_e=local_e,
                            record_rolling=record_rolling,
                            day=day,
                            resolved=resolved,
                            stale_at=stale_at,
                            last_long=last_long,
                            last_short=last_short,
                            horizons_to_flush=emit_horizons,
                        )
                    if open_count == 0:
                        break
                if not broke:
                    still_open = [h for h in horizons if h > scan_h]
                    if still_open:
                        open_count = _mark_unresolved_invalid(
                            resolved, open_count, scan_h + 1
                        )
                        _flush(
                            delay=delay,
                            local_e=local_e,
                            record_rolling=record_rolling,
                            day=day,
                            resolved=resolved,
                            stale_at=stale_at,
                            last_long=last_long,
                            last_short=last_short,
                            horizons_to_flush=still_open,
                        )

    delay_out: dict[str, Any] = {}
    for delay in delays:
        delay_out[f"delay_{delay}s"] = {
            "rolling_1s": _summarize_layer(
                rolling[delay],
                horizons=horizons,
                tps=tps,
                sls=sls,
                latency_grid=latency_bps_per_side_grid,
                kind="rolling",
            ),
            "non_overlapping": _summarize_nonoverlap(
                offset_acc[delay],
                horizons=horizons,
                tps=tps,
                sls=sls,
                latency_grid=latency_bps_per_side_grid,
            ),
        }

    hours = n / 3600.0 if n else 0.0
    return {
        "protocol": TP_SL_PROTOCOL_NAME,
        "protocol_version": TP_SL_PROTOCOL_VERSION,
        "grids_frozen_before_results": True,
        "not_a_strategy": True,
        "ml_status": "NOT_STARTED",
        "horizons_seconds": list(horizons),
        "primary_horizons_seconds": list(PRIMARY_HORIZONS_SECONDS),
        "control_horizons_seconds": list(CONTROL_HORIZONS_SECONDS),
        "tp_bps": [_thr_key(x) for x in tps],
        "sl_bps": [_thr_key(x) for x in sls],
        "delays_seconds": list(delays),
        "rolling_delays_seconds": sorted(rolling_delay_set),
        "latency_bps_per_side_grid": list(latency_bps_per_side_grid),
        "n_rows": n,
        "n_contiguous_segments": len(segments),
        "usable_hours": hours,
        "median_spread_bps": median_spread_bps,
        "executable_definition": {
            "long": "entry ask[t]; exit future bid; gross=(bid[tau]/ask[t]-1)*10000",
            "short": "entry bid[t]; cover future ask; gross=(bid[t]/ask[tau]-1)*10000",
            "ambiguous": (
                "consecutive 1s interval contains both +TP and -SL; "
                "no intra-second order is invented"
            ),
            "data_invalid": (
                "path became unobservable before TP/SL/timeout (gap, stale/"
                "fallback TOB, or reconnect). Not TIMEOUT."
            ),
        },
        "day_stability_nonoverlap_offset0": _summarize_days(day_acc, tps=tps, sls=sls),
        **delay_out,
    }


def _economics_bundle(
    acc: _OutAcc,
    *,
    horizon: int,
    tp: float,
    sl: float,
    latency_grid: tuple[float, ...],
) -> dict[str, Any]:
    block: dict[str, Any] = dict(acc.counts())
    block.update(acc.time_block())
    block["tp_first_rate"] = _rate(acc.n_tp, acc.n_valid)
    block["sl_first_rate"] = _rate(acc.n_sl, acc.n_valid)
    block["timeout_rate"] = _rate(acc.n_to, acc.n_valid)
    block["ambiguous_rate"] = _rate(acc.n_amb, acc.n_valid)
    block["data_invalid_rate_among_starts"] = _rate(
        acc.n_data_invalid, acc.n_valid + acc.n_data_invalid
    )
    econ: dict[str, Any] = {}
    for lat in latency_grid:
        extra = extra_cost_bps(holding_seconds=float(horizon), latency_bps_per_side=lat)
        payload = economics_for_cell(
            tp_bps=tp,
            sl_bps=sl,
            n_valid=acc.n_valid,
            n_tp_first=acc.n_tp,
            n_sl_first=acc.n_sl,
            n_timeout=acc.n_to,
            n_ambiguous=acc.n_amb,
            mean_gross_tp=_mean(acc.tp_gross),
            mean_gross_sl=_mean(acc.sl_gross),
            mean_gross_timeout=_mean(acc.to_gross),
            extra_cost_bps=extra,
        )
        econ[_thr_key(lat)] = payload
    block["economics_by_latency_bps_per_side"] = econ
    current = econ.get(_thr_key(CURRENT_MODELED_LATENCY_BPS_PER_SIDE)) or {}
    block["economics_current_modeled_latency"] = current
    return block


def _summarize_layer(
    grid: dict[int, dict[float, dict[float, dict[str, _OutAcc]]]],
    *,
    horizons: tuple[int, ...],
    tps: tuple[float, ...],
    sls: tuple[float, ...],
    latency_grid: tuple[float, ...],
    kind: str,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for horizon in horizons:
        h_key = f"{horizon}s"
        tp_out: dict[str, Any] = {}
        for tp in tps:
            sl_out: dict[str, Any] = {}
            for sl in sls:
                sl_out[_thr_key(sl)] = {
                    direction: _economics_bundle(
                        grid[horizon][tp][sl][direction],
                        horizon=horizon,
                        tp=tp,
                        sl=sl,
                        latency_grid=latency_grid,
                    )
                    for direction in DIRECTIONS
                }
            tp_out[_thr_key(tp)] = sl_out
        out[h_key] = tp_out
    out["_kind"] = kind
    return out


def _summarize_nonoverlap(
    grid: dict[int, dict[int, dict[float, dict[float, dict[str, _OutAcc]]]]],
    *,
    horizons: tuple[int, ...],
    tps: tuple[float, ...],
    sls: tuple[float, ...],
    latency_grid: tuple[float, ...],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for horizon in horizons:
        offs = non_overlap_offsets(horizon)
        h_key = f"{horizon}s"
        tp_out: dict[str, Any] = {}
        for tp in tps:
            sl_out: dict[str, Any] = {}
            for sl in sls:
                dir_out: dict[str, Any] = {}
                for direction in DIRECTIONS:
                    per_off: dict[str, Any] = {}
                    pooled = _OutAcc()
                    for off in offs:
                        acc = grid[horizon][off][tp][sl][direction]
                        per_off[str(off)] = {
                            **acc.counts(),
                            **acc.time_block(),
                            "tp_first_rate": _rate(acc.n_tp, acc.n_valid),
                            "sl_first_rate": _rate(acc.n_sl, acc.n_valid),
                            "timeout_rate": _rate(acc.n_to, acc.n_valid),
                            "ambiguous_rate": _rate(acc.n_amb, acc.n_valid),
                        }
                        # Pooled sums are dependent phases of the same path.
                        pooled.n_valid += acc.n_valid
                        pooled.n_tp += acc.n_tp
                        pooled.n_sl += acc.n_sl
                        pooled.n_to += acc.n_to
                        pooled.n_amb += acc.n_amb
                        pooled.n_data_invalid += acc.n_data_invalid
                        pooled.n_resolved_on_stale += acc.n_resolved_on_stale
                        pooled.tp_times.extend(acc.tp_times)
                        pooled.sl_times.extend(acc.sl_times)
                        pooled.tp_gross.extend(acc.tp_gross)
                        pooled.sl_gross.extend(acc.sl_gross)
                        pooled.to_gross.extend(acc.to_gross)
                    primary = grid[horizon][offs[0]][tp][sl][direction]
                    bundle = _economics_bundle(
                        primary,
                        horizon=horizon,
                        tp=tp,
                        sl=sl,
                        latency_grid=latency_grid,
                    )
                    bundle["per_offset"] = per_off
                    bundle["pooled_descriptive_dependent"] = {
                        **pooled.counts(),
                        "mean_timeout_gross_bps": _mean(pooled.to_gross),
                        "note": (
                            "Pooled offset sums are four phases of the same path; "
                            "they are dependent and not a larger independent sample."
                        ),
                    }
                    dir_out[direction] = bundle
                sl_out[_thr_key(sl)] = dir_out
            tp_out[_thr_key(tp)] = sl_out
        out[h_key] = {
            "offsets_seconds": list(offs),
            **tp_out,
        }
    return out


def _summarize_days(
    day_acc: dict[str, dict[int, dict[float, dict[float, dict[str, _OutAcc]]]]],
    *,
    tps: tuple[float, ...],
    sls: tuple[float, ...],
) -> dict[str, Any]:
    days = sorted(day_acc)
    per_day: list[dict[str, Any]] = []
    for day in days:
        cells: dict[str, Any] = {}
        for horizon, grid in day_acc[day].items():
            for tp in tps:
                if tp not in grid:
                    continue
                for sl in sls:
                    if sl not in grid[tp]:
                        continue
                    for direction in DIRECTIONS:
                        acc = grid[tp][sl][direction]
                        key = f"{horizon}s_tp{ _thr_key(tp)}_sl{_thr_key(sl)}_{direction}"
                        cells[key] = {
                            **acc.counts(),
                            "tp_first_rate": _rate(acc.n_tp, acc.n_valid),
                            "sl_first_rate": _rate(acc.n_sl, acc.n_valid),
                            "timeout_rate": _rate(acc.n_to, acc.n_valid),
                            "ambiguous_rate": _rate(acc.n_amb, acc.n_valid),
                            "mean_timeout_gross_bps": _mean(acc.to_gross),
                        }
        per_day.append({"utc_date": day, "cells": cells})
    dist: dict[str, Any] = {}
    keys = sorted({key for row in per_day for key in row["cells"]})
    for key in keys:
        rates = [
            float(row["cells"][key]["tp_first_rate"])
            for row in per_day
            if key in row["cells"]
            and row["cells"][key].get("tp_first_rate") is not None
        ]
        ordered = sorted(rates)
        dist[key] = {
            "n_days": len(ordered),
            "min": ordered[0] if ordered else None,
            "median": _percentile(ordered, 0.50) if ordered else None,
            "max": ordered[-1] if ordered else None,
        }
    return {"per_utc_day": per_day, "utc_dates": days, "tp_first_rate_across_days": dist}


def load_tp_sl_series_from_parquet(paths: Sequence[Path]) -> dict[str, Any]:
    """Load TOB plus forensic quality columns from market_state_1s files."""

    times: list[datetime] = []
    bid: list[float] = []
    ask: list[float] = []
    mid: list[float] = []
    valid: list[bool] = []
    book_age: list[float | None] = []
    quote_age: list[float | None] = []
    quote_fresh: list[bool] = []
    book_state: list[str | None] = []
    mark: list[float | None] = []
    spread: list[float | None] = []
    tob_src: list[str | None] = []
    exec_tob: list[bool | None] = []
    conn_ids: list[str | None] = []
    wanted = [
        "decision_time",
        "best_bid",
        "best_ask",
        "mid",
        "valid_book",
        "book_age_seconds",
        "quote_age_seconds",
        "quote_fresh",
        "mark_price",
        "spread_bps",
        "book_state",
        "tob_source",
        "executable_tob",
        "connection_id",
        "tob_source_event_id",
        "tob_age_seconds",
    ]
    for path in paths:
        schema_names = set(pq.read_schema(path).names)
        cols = [c for c in wanted if c in schema_names]
        for row in pq.read_table(path, columns=cols).to_pylist():
            b = row.get("best_bid")
            a = row.get("best_ask")
            m = row.get("mid")
            if b is None or a is None or m is None:
                continue
            times.append(_dt(row["decision_time"]))
            bid.append(float(b))
            ask.append(float(a))
            mid.append(float(m))
            valid.append(bool(row["valid_book"]) if row.get("valid_book") is not None else True)
            age = row.get("book_age_seconds")
            book_age.append(float(age) if age is not None else None)
            qage = row.get("quote_age_seconds")
            quote_age.append(float(qage) if qage is not None else None)
            qf = row.get("quote_fresh")
            quote_fresh.append(bool(qf) if qf is not None else False)
            book_state.append(
                str(row["book_state"]) if row.get("book_state") is not None else None
            )
            mk = row.get("mark_price")
            mark.append(float(mk) if mk is not None else None)
            sp = row.get("spread_bps")
            spread.append(float(sp) if sp is not None else None)
            src = row.get("tob_source")
            tob_src.append(str(src) if src is not None else None)
            ex_tob = row.get("executable_tob")
            exec_tob.append(bool(ex_tob) if ex_tob is not None else None)
            conn = row.get("connection_id")
            conn_ids.append(str(conn) if conn is not None else None)
    order = sorted(range(len(times)), key=lambda i: times[i])

    def _take(seq: list[Any]) -> list[Any]:
        return [seq[i] for i in order]

    times = _take(times)
    bid = _take(bid)
    ask = _take(ask)
    mid = _take(mid)
    valid = _take(valid)
    book_age = _take(book_age)
    quote_age = _take(quote_age)
    quote_fresh = _take(quote_fresh)
    book_state = _take(book_state)
    mark = _take(mark)
    spread = _take(spread)
    tob_src = _take(tob_src)
    exec_tob = _take(exec_tob)
    conn_ids = _take(conn_ids)
    # Duplicate seconds: keep the last row.
    out_t: list[datetime] = []
    out_b: list[float] = []
    out_a: list[float] = []
    out_m: list[float] = []
    out_v: list[bool] = []
    out_ba: list[float | None] = []
    out_qa: list[float | None] = []
    out_qf: list[bool] = []
    out_bs: list[str | None] = []
    out_mk: list[float | None] = []
    out_sp: list[float | None] = []
    out_src: list[str | None] = []
    out_ex: list[bool | None] = []
    out_conn: list[str | None] = []
    for i, ts in enumerate(times):
        if out_t and int(ts.timestamp()) == int(out_t[-1].timestamp()):
            out_t[-1] = ts
            out_b[-1] = bid[i]
            out_a[-1] = ask[i]
            out_m[-1] = mid[i]
            out_v[-1] = valid[i]
            out_ba[-1] = book_age[i]
            out_qa[-1] = quote_age[i]
            out_qf[-1] = quote_fresh[i]
            out_bs[-1] = book_state[i]
            out_mk[-1] = mark[i]
            out_sp[-1] = spread[i]
            out_src[-1] = tob_src[i]
            out_ex[-1] = exec_tob[i]
            out_conn[-1] = conn_ids[i]
            continue
        out_t.append(ts)
        out_b.append(bid[i])
        out_a.append(ask[i])
        out_m.append(mid[i])
        out_v.append(valid[i])
        out_ba.append(book_age[i])
        out_qa.append(quote_age[i])
        out_qf.append(quote_fresh[i])
        out_bs.append(book_state[i])
        out_mk.append(mark[i])
        out_sp.append(spread[i])
        out_src.append(tob_src[i])
        out_ex.append(exec_tob[i])
        out_conn.append(conn_ids[i])
    return {
        "times": out_t,
        "epoch_s": [int(ts.timestamp()) for ts in out_t],
        "bid": out_b,
        "ask": out_a,
        "mid": out_m,
        "valid_book": out_v,
        "book_age_seconds": out_ba,
        "quote_age_seconds": out_qa,
        "quote_fresh": out_qf,
        "book_state": out_bs,
        "mark_price": out_mk,
        "spread_bps": out_sp,
        "tob_source": out_src,
        "executable_tob": out_ex,
        "connection_id": out_conn,
    }


def filter_tp_sl_series(
    series: Mapping[str, Any],
    gaps: Sequence[CollectionGap],
) -> tuple[dict[str, Any], int]:
    """Drop documented collection holes and invalid TOB, keep quality columns."""

    times = list(series["times"])
    n = len(times)
    keep: list[int] = []
    dropped = 0
    for index in range(n):
        instant = _dt(times[index])
        if any(gap.contains_timestamp(instant) for gap in gaps):
            dropped += 1
            continue
        if not executable_prices_ok(
            float(series["bid"][index]),
            float(series["ask"][index]),
            float(series["mid"][index]),
        ):
            dropped += 1
            continue
        keep.append(index)

    def _pick(key: str) -> list[Any]:
        values = list(series[key])
        return [values[i] for i in keep]

    out = {
        "times": _pick("times"),
        "epoch_s": [int(_dt(ts).timestamp()) for ts in _pick("times")],
        "bid": _pick("bid"),
        "ask": _pick("ask"),
        "mid": _pick("mid"),
        "valid_book": _pick("valid_book"),
        "book_age_seconds": _pick("book_age_seconds"),
        "quote_age_seconds": _pick("quote_age_seconds"),
        "quote_fresh": _pick("quote_fresh") if "quote_fresh" in series else [],
        "book_state": _pick("book_state") if "book_state" in series else [],
        "mark_price": _pick("mark_price"),
        "spread_bps": _pick("spread_bps"),
        "tob_source": _pick("tob_source") if "tob_source" in series else [],
        "executable_tob": _pick("executable_tob") if "executable_tob" in series else [],
        "connection_id": _pick("connection_id") if "connection_id" in series else [],
    }
    return out, dropped


def _tag_quality(
    *,
    index: int,
    abs_jump: float,
    mid: float,
    valid_book: Sequence[bool] | None,
    book_age: Sequence[float | None] | None,
    quote_age: Sequence[float | None] | None,
    mark_price: Sequence[float | None] | None,
) -> list[str]:
    tags: list[str] = []
    if valid_book is not None and index < len(valid_book) and not valid_book[index]:
        tags.append("QUOTE_FALLBACK_OR_INVALID_BOOK")
    if book_age is not None and index < len(book_age):
        age = book_age[index]
        if age is not None and float(age) > STALE_BOOK_SECONDS:
            tags.append("STALE_BOOK")
    if quote_age is not None and index < len(quote_age):
        qage = quote_age[index]
        if qage is not None and float(qage) > STALE_BOOK_SECONDS:
            tags.append("STALE_QUOTE")
    if mark_price is not None and index < len(mark_price) and mark_price[index] is not None:
        mk = float(mark_price[index])  # type: ignore[arg-type]
        if mid > 0 and math.isfinite(mk):
            basis = abs(mk / mid - 1.0) * 10_000.0
            if basis >= MARK_DIVERGENCE_BPS:
                tags.append("MARK_QUOTE_DIVERGENCE")
    if abs_jump >= 50.0:
        tags.append("ONE_SECOND_JUMP_GE_50BPS")
    if not tags:
        tags.append("NO_QUALITY_FLAG")
    return tags


def scan_forensic_excursions(
    epoch_s: Sequence[int],
    bid: Sequence[float],
    ask: Sequence[float],
    mid: Sequence[float],
    *,
    horizons: tuple[int, ...] = FORENSIC_SUBMINUTE_HORIZONS_SECONDS,
    thresholds: tuple[float, ...] = FORENSIC_EXCURSION_THRESHOLDS_BPS,
    mae_tp_bps: float = FORENSIC_MAE_TP_BPS,
    mae_tail_bps: float = FORENSIC_MAE_TAIL_BPS,
    top_n: int = 15,
    valid_book: Sequence[bool] | None = None,
    book_age_seconds: Sequence[float | None] | None = None,
    quote_age_seconds: Sequence[float | None] | None = None,
    mark_price: Sequence[float | None] | None = None,
) -> dict[str, Any]:
    """Largest sub-minute 50/75/100 bps executable excursions and MAE tails."""

    min_h = min(horizons)
    max_h = max(horizons)
    horizon_set = set(horizons)
    excursions: list[dict[str, Any]] = []
    mae_tails: list[dict[str, Any]] = []
    n_ge: dict[str, int] = { _thr_key(t): 0 for t in thresholds }
    n_mae = 0
    segments = split_contiguous_1s_segments(epoch_s)
    for lo, hi in segments:
        length = hi - lo
        for local in range(length):
            remain = length - 1 - local
            if remain < min_h:
                break
            i = lo + local
            if not executable_prices_ok(bid[i], ask[i], mid[i]):
                continue
            ask0 = ask[i]
            bid0 = bid[i]
            max_abs = 0.0
            max_signed = 0.0
            max_lag = 0
            max_1s_jump = 0.0
            prev_long = (bid0 / ask0 - 1.0) * 10_000.0
            prev_short = prev_long
            mae_long = 0.0
            mae_short = 0.0
            hit_long = 0
            hit_short = 0
            scan_h = min(max_h, remain)
            for k in range(1, scan_h + 1):
                j = i + k
                if not executable_prices_ok(bid[j], ask[j], mid[j]):
                    break
                exec_long = (bid[j] / ask0 - 1.0) * 10_000.0
                exec_short = (bid0 / ask[j] - 1.0) * 10_000.0
                jump = max(abs(exec_long - prev_long), abs(exec_short - prev_short))
                if jump > max_1s_jump:
                    max_1s_jump = jump
                if abs(exec_long) > max_abs:
                    max_abs = abs(exec_long)
                    max_signed = exec_long
                    max_lag = k
                if abs(exec_short) > max_abs:
                    max_abs = abs(exec_short)
                    max_signed = -exec_short
                    max_lag = k
                if not hit_long:
                    adv = -exec_long if exec_long < 0 else 0.0
                    if adv > mae_long:
                        mae_long = adv
                    if exec_long >= mae_tp_bps:
                        hit_long = k
                if not hit_short:
                    adv_s = -exec_short if exec_short < 0 else 0.0
                    if adv_s > mae_short:
                        mae_short = adv_s
                    if exec_short >= mae_tp_bps:
                        hit_short = k
                prev_long = exec_long
                prev_short = exec_short
                if k in horizon_set:
                    for thr in thresholds:
                        if max_abs >= thr:
                            n_ge[_thr_key(thr)] += 1
                    if max_abs >= thresholds[0]:
                        tags = _tag_quality(
                            index=i + max_lag,
                            abs_jump=max_1s_jump,
                            mid=float(mid[i + max_lag]),
                            valid_book=valid_book,
                            book_age=book_age_seconds,
                            quote_age=quote_age_seconds,
                            mark_price=mark_price,
                        )
                        excursions.append(
                            {
                                "start_epoch_s": int(epoch_s[i]),
                                "start_utc": datetime.fromtimestamp(
                                    int(epoch_s[i]), UTC
                                ).isoformat(),
                                "horizon_seconds": k,
                                "abs_exec_mfe_bps": max_abs,
                                "signed_exec_mfe_bps": max_signed,
                                "mfe_lag_s": max_lag,
                                "max_1s_jump_bps": max_1s_jump,
                                "quality_tags": tags,
                            }
                        )
            if hit_long and mae_long >= mae_tail_bps:
                n_mae += 1
                mae_tails.append(
                    {
                        "direction": "long",
                        "start_epoch_s": int(epoch_s[i]),
                        "start_utc": datetime.fromtimestamp(int(epoch_s[i]), UTC).isoformat(),
                        "mae_before_tp_bps": mae_long,
                        "tp_lag_s": hit_long,
                        "quality_tags": _tag_quality(
                            index=i + hit_long,
                            abs_jump=max_1s_jump,
                            mid=float(mid[i + hit_long]),
                            valid_book=valid_book,
                            book_age=book_age_seconds,
                            quote_age=quote_age_seconds,
                            mark_price=mark_price,
                        ),
                    }
                )
            if hit_short and mae_short >= mae_tail_bps:
                n_mae += 1
                mae_tails.append(
                    {
                        "direction": "short",
                        "start_epoch_s": int(epoch_s[i]),
                        "start_utc": datetime.fromtimestamp(int(epoch_s[i]), UTC).isoformat(),
                        "mae_before_tp_bps": mae_short,
                        "tp_lag_s": hit_short,
                        "quality_tags": _tag_quality(
                            index=i + hit_short,
                            abs_jump=max_1s_jump,
                            mid=float(mid[i + hit_short]),
                            valid_book=valid_book,
                            book_age=book_age_seconds,
                            quote_age=quote_age_seconds,
                            mark_price=mark_price,
                        ),
                    }
                )

    excursions.sort(key=lambda row: -float(row["abs_exec_mfe_bps"]))
    mae_tails.sort(key=lambda row: -float(row["mae_before_tp_bps"]))
    # Dedup rolling clusters: keep the peak of each start-second bucket for the appendix.
    seen_exc: set[int] = set()
    unique_exc: list[dict[str, Any]] = []
    for row in excursions:
        exc_key = int(row["start_epoch_s"])
        if exc_key in seen_exc:
            continue
        seen_exc.add(exc_key)
        unique_exc.append(row)
        if len(unique_exc) >= top_n:
            break
    seen_mae: set[tuple[str, int]] = set()
    unique_mae: list[dict[str, Any]] = []
    for row in mae_tails:
        mae_key = (str(row["direction"]), int(row["start_epoch_s"]))
        if mae_key in seen_mae:
            continue
        seen_mae.add(mae_key)
        unique_mae.append(row)
        if len(unique_mae) >= top_n:
            break
    bad_tags = FORENSIC_BAD_TAGS
    n_bad = sum(
        1
        for row in unique_exc + unique_mae
        if bad_tags.intersection(row.get("quality_tags") or [])
    )
    return {
        "horizons_seconds": list(horizons),
        "thresholds_bps": list(thresholds),
        "n_subminute_windows_ge_50bps": n_ge.get("50", 0),
        "n_subminute_windows_ge_75bps": n_ge.get("75", 0),
        "n_subminute_windows_ge_100bps": n_ge.get("100", 0),
        "n_mae_tails_before_tp20": n_mae,
        "largest_excursions": unique_exc,
        "mae_tails": unique_mae,
        "n_quality_flagged_top_cases": n_bad,
        "note": (
            "Rolling windows of the same spike are dependent. Quality tags use "
            "market_state_1s valid_book, book/quote age, and mark vs mid. "
            "Raw-event spot checks belong in the runner appendix."
        ),
    }


def assess_primary_contamination(
    *,
    stats: Mapping[str, Any],
    forensic: Mapping[str, Any],
) -> dict[str, Any]:
    """STOP/escalate if stale data reaches primary 20–30 bps TP/SL cells."""

    alerts: list[str] = []
    layer = (stats.get("delay_0s") or {}).get("non_overlapping") or {}
    max_frac = 0.0
    worst: dict[str, Any] | None = None
    for horizon in PRIMARY_HORIZONS_SECONDS:
        h_block = layer.get(f"{horizon}s") or {}
        for tp in TP_THRESHOLDS_BPS:
            sl_block = h_block.get(_thr_key(tp)) or {}
            for sl in SL_THRESHOLDS_BPS:
                for direction in DIRECTIONS:
                    cell = (sl_block.get(_thr_key(sl)) or {}).get(direction) or {}
                    n_hit = int(cell.get("n_tp_first") or 0) + int(cell.get("n_sl_first") or 0)
                    n_stale = int(
                        cell.get("n_tp_or_sl_resolved_on_stale_or_quote_fallback") or 0
                    )
                    frac = (n_stale / n_hit) if n_hit else 0.0
                    if frac > max_frac:
                        max_frac = frac
                        worst = {
                            "horizon_seconds": horizon,
                            "tp_bps": tp,
                            "sl_bps": sl,
                            "direction": direction,
                            "n_tp_or_sl": n_hit,
                            "n_stale_resolves": n_stale,
                            "fraction": frac,
                        }
                    if n_hit and (n_stale > 0 or frac > PRIMARY_STALE_RESOLUTION_MAX_FRACTION):
                        alerts.append(
                            f"PRIMARY_STALE_RESOLVE {horizon}s tp{tp} sl{sl} {direction}: "
                            f"{n_stale}/{n_hit} ({frac:.1%})"
                        )
    flagged = int(forensic.get("n_quality_flagged_top_cases") or 0)
    top_exc = list(forensic.get("largest_excursions") or [])
    n_top_flagged = sum(
        1
        for row in top_exc
        if FORENSIC_BAD_TAGS.intersection(row.get("quality_tags") or [])
    )
    if n_top_flagged >= FORENSIC_TOP_FLAGGED_ESCALATE_COUNT:
        alerts.append(
            f"FORENSIC_TOP_PEAKS_FLAGGED:{n_top_flagged}/"
            f"{len(top_exc)} (threshold {FORENSIC_TOP_FLAGGED_ESCALATE_COUNT})"
        )
    if flagged:
        alerts.append(
            f"FORENSIC_QUALITY_FLAGS_ON_TOP_CASES:{flagged} "
            "(inspect appendix; not automatically a primary-cell fail)"
        )
    contaminated = any(
        item.startswith("PRIMARY_STALE_RESOLVE")
        or item.startswith("FORENSIC_TOP_PEAKS_FLAGGED")
        for item in alerts
    )
    return {
        "contamination_status": "FAIL" if contaminated else "PASS",
        "escalate": contaminated,
        "max_stale_resolve_fraction_primary": max_frac,
        "worst_primary_cell": worst,
        "n_top_excursions_flagged": n_top_flagged,
        "alerts": alerts,
        "threshold": PRIMARY_STALE_RESOLUTION_MAX_FRACTION,
        "forensic_top_flagged_escalate_count": FORENSIC_TOP_FLAGGED_ESCALATE_COUNT,
    }


def _public_price_fields(payload: Any) -> dict[str, Any]:
    """Pull public bid/ask/mark numbers only. Never keep account or key fields."""

    keep = (
        "bid",
        "ask",
        "bidPrice",
        "askPrice",
        "bid_price",
        "ask_price",
        "price",
        "markPrice",
        "mark_price",
    )
    found: dict[str, Any] = {}
    if isinstance(payload, dict):
        for key in keep:
            if key in payload and payload[key] is not None:
                found[key] = payload[key]
        for value in payload.values():
            if len(found) >= 4:
                break
            if isinstance(value, dict):
                found.update(
                    {k: v for k, v in _public_price_fields(value).items() if k not in found}
                )
    return found


def index_restored_raw_windows(restore_root: Path) -> list[tuple[datetime, datetime, Path]]:
    """Map restored RAW events.parquet files by [start, end) without reading payloads."""

    indexed: list[tuple[datetime, datetime, Path]] = []
    if not restore_root.is_dir():
        return indexed
    for child in restore_root.iterdir():
        if not child.is_dir():
            continue
        start, end = parse_dataset_id_window(child.name)
        parquet = child / "events.parquet"
        if start is None or end is None or not parquet.is_file():
            continue
        indexed.append((start, end, parquet))
    indexed.sort(key=lambda item: item[0])
    return indexed


def spot_check_restored_raw_events(
    *,
    epoch_s: int,
    restore_index: Sequence[tuple[datetime, datetime, Path]],
    window_seconds: int = 2,
) -> dict[str, Any]:
    """Bounded public RAW check around a spike. No secrets, no B2 mutation."""

    target = datetime.fromtimestamp(int(epoch_s), UTC)
    notes: dict[str, Any] = {
        "target_utc": target.isoformat(),
        "status": "NO_RESTORED_WINDOW",
        "dataset_id": None,
        "event_type_counts": {},
        "n_events": 0,
        "max_abs_latency_ms": None,
        "sample_public_quote": None,
        "sample_public_mark": None,
    }
    parquet: Path | None = None
    dataset_id = None
    for start, end, path in restore_index:
        if start <= target < end:
            parquet = path
            dataset_id = path.parent.name
            break
    if parquet is None:
        return notes
    notes["dataset_id"] = dataset_id
    lo = target.timestamp() - window_seconds
    hi = target.timestamp() + window_seconds
    counts: dict[str, int] = {}
    n_events = 0
    max_lat: float | None = None
    sample_quote: dict[str, Any] | None = None
    sample_mark: dict[str, Any] | None = None
    handle = pq.ParquetFile(parquet)
    cols = [
        name
        for name in ("received_at", "event_type", "topic", "latency_ms", "payload_json")
        if name in handle.schema_arrow.names
    ]
    for batch in handle.iter_batches(batch_size=8_000, columns=cols):
        for row in batch.to_pylist():
            raw_ts = row.get("received_at")
            if raw_ts is None:
                continue
            ts = _dt(raw_ts)
            unix = ts.timestamp()
            if unix < lo or unix > hi:
                continue
            n_events += 1
            etype = str(row.get("topic") or row.get("event_type") or "unknown")
            counts[etype] = counts.get(etype, 0) + 1
            lat = row.get("latency_ms")
            if lat is not None and math.isfinite(float(lat)):
                abs_lat = abs(float(lat))
                if max_lat is None or abs_lat > max_lat:
                    max_lat = abs_lat
            payload_raw = row.get("payload_json")
            if isinstance(payload_raw, str):
                try:
                    payload = json.loads(payload_raw)
                except json.JSONDecodeError:
                    payload = {}
                public = _public_price_fields(payload)
                if etype in {"ask_bid_price", "orderbook"} and sample_quote is None:
                    sample_quote = public
                if etype == "mark_price" and sample_mark is None:
                    sample_mark = public
    notes["n_events"] = n_events
    notes["event_type_counts"] = counts
    notes["max_abs_latency_ms"] = max_lat
    notes["sample_public_quote"] = sample_quote
    notes["sample_public_mark"] = sample_mark
    notes["status"] = "OK" if n_events else "NO_EVENTS_IN_PM2S_WINDOW"
    return notes


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{100.0 * value:.2f}%"


def _fmt_num(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def render_tp_sl_markdown(report: Mapping[str, Any]) -> str:
    """Lead-facing feasibility surface. Does not pick a strategy cell."""

    cost = report.get("cost_audit") or {}
    forensic = report.get("forensic_qa") or {}
    corpus = report.get("corpus") or {}
    lines = [
        str(report.get("report_title") or "# ETH TP×SL first-touch feasibility v1"),
        "",
        f"STATUS: `{report.get('STATUS')}`",
        f"ML_STATUS: `{report.get('ML_STATUS', 'NOT_STARTED')}`",
        f"DECISION: `{report.get('DECISION')}`",
        "",
        "## Method",
        "",
        "Unconditional executable TP-before-SL on the accepted full-corpus v1",
        "discovery dates. Grids were frozen before this scan. Long enters at",
        "ask and exits at future bid; short enters at bid and covers at future",
        "ask. Each valid start is TP_FIRST, SL_FIRST, TIMEOUT, or AMBIGUOUS.",
        "DATA_INVALID means the path became unobservable before TP/SL/timeout",
        "(gap, stale/fallback, or reconnect) and is not counted as TIMEOUT.",
        "AMBIGUOUS means the 1s interval contains both barriers; intra-second",
        "order is never invented. TIMEOUT records the executable return at H",
        "because an opened position must close only when the full path was",
        "observed. This is a feasibility surface: do not optimize a strategy",
        "from these cells. No ML and no feature selection.",
        "",
        "## Freeze",
        "",
        f"- primary H: `{report.get('primary_horizons_seconds')}`",
        f"- control H: `{report.get('control_horizons_seconds')}`",
        f"- TP bps: `{report.get('tp_bps')}`",
        f"- SL bps: `{report.get('sl_bps')}`",
        f"- discovery dates: `{corpus.get('discovery_utc_dates')}`",
        f"- discovery hours: `{corpus.get('discovery_usable_hours')}`",
        f"- untouched OOS (not scanned): `{list(V1_UNTOUCHED_OOS_UTC_DATES)}`",
        "",
        "## Cost audit (spread not double-counted)",
        "",
        f"- fee round-trip: `{cost.get('fee_round_trip_bps')}` bps",
        f"- observed median spread (already in executable gross): "
        f"`{cost.get('spread_bps_observed_median')}` bps",
        f"- subtract spread again from net: "
        f"`{cost.get('subtract_spread_again_from_net')}`",
        f"- modeled latency / side: `{cost.get('latency_bps_per_side')}` bps",
        f"- latency round-trip: `{cost.get('latency_round_trip_bps')}` bps",
        f"- funding (at audit holding): `{cost.get('funding_bps')}` bps",
        f"- extra cost excluding spread: "
        f"`{cost.get('extra_cost_bps_excluding_spread')}` bps",
        "- legacy all-in RT (includes spread; do not subtract from executable "
        f"gross): `{cost.get('legacy_round_trip_friction_bps')}` bps",
        "",
        str(cost.get("note") or ""),
        "",
        "## Non-overlap offset-0 rates and net EV (delay 0s, current latency)",
        "",
        "Exact counts are per offset-0 start. Pooled four-offset sums in JSON",
        "are dependent descriptive stats only.",
        "",
        "| H | TP | SL | dir | n | TP_FIRST | SL_FIRST | TIMEOUT | AMB | DATA_INVALID | "
        "timeout mean bps | net EV bps | p* TP-first | lift abs | payoff TP/SL |",
        "|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    layer = (report.get("delay_0s") or {}).get("non_overlapping") or {}
    for horizon in report.get("horizons_seconds") or ALL_HORIZONS_SECONDS:
        h_block = layer.get(f"{horizon}s") or {}
        for tp_key in report.get("tp_bps") or ["20", "25", "30"]:
            for sl_key in report.get("sl_bps") or ["5", "10", "15", "20"]:
                sl_block = (h_block.get(str(tp_key)) or {}).get(str(sl_key)) or {}
                for direction in DIRECTIONS:
                    cell = sl_block.get(direction) or {}
                    econ = cell.get("economics_current_modeled_latency") or {}
                    tp_n = f"{cell.get('n_tp_first') or 0}/"
                    tp_n += _fmt_pct(cell.get("tp_first_rate"))
                    sl_n = f"{cell.get('n_sl_first') or 0}/"
                    sl_n += _fmt_pct(cell.get("sl_first_rate"))
                    to_n = f"{cell.get('n_timeout') or 0}/"
                    to_n += _fmt_pct(cell.get("timeout_rate"))
                    p_be = econ.get("break_even_tp_first_prob_two_outcome_barrier")
                    lines.append(
                        "| "
                        + " | ".join(
                            [
                                f"{horizon}s",
                                str(tp_key),
                                str(sl_key),
                                direction,
                                str(cell.get("n_valid_starts") or 0),
                                tp_n,
                                sl_n,
                                to_n,
                                str(cell.get("n_ambiguous") or 0),
                                str(cell.get("n_data_invalid") or 0),
                                _fmt_num(cell.get("mean_timeout_gross_bps")),
                                _fmt_num(econ.get("unconditional_net_ev_bps")),
                                _fmt_pct(p_be),
                                _fmt_num(econ.get("required_lift_abs"), 3),
                                _fmt_num(
                                    econ.get("payoff_ratio_barrier_tp_over_sl"), 2
                                ),
                            ]
                        )
                        + " |"
                    )
    lines.extend(
        [
            "",
            "## Latency sensitivity (additive bps / side; same gross outcomes)",
            "",
            "Path delay 0/1/2s is a separate tree in JSON (`delay_1s`, `delay_2s`).",
            "Additive latency does not change TP/SL/TIMEOUT counts.",
            "",
        ]
    )
    # Compact sensitivity for 120s TP20 SL10; every cell is in JSON.
    sample = ((layer.get("120s") or {}).get("20") or {}).get("10") or {}
    if sample.get("long"):
        lines.append("| latency bps/side | long 120s TP20 SL10 net EV | short net EV |")
        lines.append("|---:|---:|---:|")
        lat_map = sample["long"].get("economics_by_latency_bps_per_side") or {}
        for lat_key, payload in lat_map.items():
            short_e = (
                (sample.get("short") or {})
                .get("economics_by_latency_bps_per_side") or {}
            ).get(lat_key) or {}
            lines.append(
                f"| {lat_key} | {_fmt_num(payload.get('unconditional_net_ev_bps'))} | "
                f"{_fmt_num(short_e.get('unconditional_net_ev_bps'))} |"
            )
        lines.append("")
    lines.extend(
        [
            "## Path delay 1s / 2s (non-overlap offset 0, 120s TP20 SL10)",
            "",
            "Separate from additive latency bps. Entry uses ask/bid at t+delay.",
            "",
        ]
    )
    delay_rows = ["| delay | dir | n | TP_FIRST | SL_FIRST | TIMEOUT | AMB | net EV |"]
    delay_rows.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for delay_key in ("delay_0s", "delay_1s", "delay_2s"):
        sample_d = (
            ((report.get(delay_key) or {}).get("non_overlapping") or {})
            .get("120s")
            or {}
        ).get("20") or {}
        sample_d = sample_d.get("10") or {}
        for direction in DIRECTIONS:
            cell = sample_d.get(direction) or {}
            econ = cell.get("economics_current_modeled_latency") or {}
            delay_rows.append(
                "| "
                + " | ".join(
                    [
                        delay_key,
                        direction,
                        str(cell.get("n_valid_starts") or 0),
                        str(cell.get("n_tp_first") or 0),
                        str(cell.get("n_sl_first") or 0),
                        str(cell.get("n_timeout") or 0),
                        str(cell.get("n_ambiguous") or 0),
                        _fmt_num(econ.get("unconditional_net_ev_bps")),
                    ]
                )
                + " |"
            )
    lines.extend(delay_rows)
    lines.append("")
    lines.extend(
        [
            "## Day stability (non-overlap offset 0)",
            "",
            "Per UTC day TP-first rate for primary H × TP 20 / SL 10 (long).",
            "",
        ]
    )
    day_block = report.get("day_stability_nonoverlap_offset0") or {}
    day_rows = list(day_block.get("per_utc_day") or [])
    dist = day_block.get("tp_first_rate_across_days") or {}
    keys = [f"{h}s_tp20_sl10_long" for h in PRIMARY_HORIZONS_SECONDS]
    if dist:
        lines.append("| cell | min | median | max | n days |")
        lines.append("|---|---:|---:|---:|---:|")
        for key in keys:
            row = dist.get(key) or {}
            lines.append(
                "| "
                + " | ".join(
                    [
                        key,
                        _fmt_pct(row.get("min")),
                        _fmt_pct(row.get("median")),
                        _fmt_pct(row.get("max")),
                        str(row.get("n_days") or 0),
                    ]
                )
                + " |"
            )
        lines.append("")
        lines.append("| UTC date | " + " | ".join(keys) + " |")
        lines.append("|---|" + "|".join(["---:"] * len(keys)) + "|")
        for row in day_rows:
            cells = row.get("cells") or {}
            vals = [
                _fmt_pct((cells.get(k) or {}).get("tp_first_rate")) for k in keys
            ]
            lines.append("| " + str(row.get("utc_date")) + " | " + " | ".join(vals) + " |")
        lines.append("")
    contam = forensic.get("contamination") or forensic
    lines.extend(
        [
            "## Forensic QA appendix",
            "",
            f"- contamination status: `{contam.get('contamination_status')}`",
            f"- sub-minute windows ≥50/75/100 bps (rolling, dependent): "
            f"`{forensic.get('n_subminute_windows_ge_50bps')}` / "
            f"`{forensic.get('n_subminute_windows_ge_75bps')}` / "
            f"`{forensic.get('n_subminute_windows_ge_100bps')}`",
            f"- MAE tails before executable TP 20 (≥{FORENSIC_MAE_TAIL_BPS} bps): "
            f"`{forensic.get('n_mae_tails_before_tp20')}`",
            "",
        ]
    )
    cases = list(forensic.get("largest_excursions") or [])[:8]
    if cases:
        lines.append("| start UTC | H | |MFE| bps | 1s jump | tags |")
        lines.append("|---|---:|---:|---:|---|")
        for case in cases:
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(case.get("start_utc")),
                        str(case.get("horizon_seconds")),
                        _fmt_num(case.get("abs_exec_mfe_bps"), 2),
                        _fmt_num(case.get("max_1s_jump_bps"), 2),
                        ",".join(case.get("quality_tags") or []),
                    ]
                )
                + " |"
            )
        lines.append("")
    raw_notes = forensic.get("raw_event_checks") or []
    if raw_notes:
        lines.append("### Raw-event / book-validity spot checks")
        lines.append("")
        for note in raw_notes:
            lines.append(f"- {note}")
        lines.append("")
    lines.extend(
        [
            "## Lead decision",
            "",
            "STOP_FOR_LEAD_REVIEW. Do not start ML, feature selection, PAPER, or",
            "live trading. Do not pick a (H, TP, SL) cell as a strategy from this",
            "surface. If contamination_status is FAIL, the primary 20–30 bps cells",
            "are not usable until the data issue is reviewed.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"
