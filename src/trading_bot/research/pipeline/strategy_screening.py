"""Coarse strategy-family screens on Hibachi-only exploratory market_state.

No threshold mining. Predeclared cutoffs only. Screening, not optimization.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from trading_bot.research.pipeline.cost_evidence import (
    funding_contribution_bps,
    hibachi_public_fee_schedule,
)
from trading_bot.research.pipeline.opportunity_base_rate import (
    absolute_executable_move_bps,
    summarize_abs_moves,
)


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    weight = pos - lo
    return sorted_vals[lo] * (1.0 - weight) + sorted_vals[hi] * weight


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _seconds_span(rows: list[dict[str, Any]]) -> float:
    if len(rows) < 2:
        return 0.0
    times = sorted(_dt(row["decision_time"]) for row in rows)
    return (times[-1] - times[0]).total_seconds()


def style_break_even_bps(
    *,
    holding_seconds: float,
    entry_taker: bool = True,
    exit_taker: bool = True,
    median_spread_bps: float = 0.05,
    latency_bps_per_taker_side: float = 1.0,
    funding_rate_per_8h: float | None = None,
) -> dict[str, Any]:
    """All-in break-even for a proposed execution structure (not always 11.05)."""

    fees = hibachi_public_fee_schedule()
    taker = float(fees["tier1_taker_fee_rate"]) * 10_000.0
    maker = float(fees["maker_fee_rate"]) * 10_000.0
    entry = taker if entry_taker else maker
    exit_ = taker if exit_taker else maker
    # Crossing pays ~full spread when both legs take; half when one takes.
    taker_legs = int(entry_taker) + int(exit_taker)
    spread = median_spread_bps * (taker_legs / 2.0)
    latency = latency_bps_per_taker_side * taker_legs
    rate = 0.0001 if funding_rate_per_8h is None else abs(float(funding_rate_per_8h))
    funding = funding_contribution_bps(
        holding_seconds, funding_rate_per_8h=rate
    )
    total = entry + exit_ + spread + latency + funding
    return {
        "entry_fee_bps": entry,
        "exit_fee_bps": exit_,
        "spread_bps": spread,
        "latency_bps": latency,
        "funding_bps": funding,
        "required_move_bps": total,
        "holding_seconds": holding_seconds,
    }


def screen_basis_dislocation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """A: mark/spot basis vs executable mid — do not trade mark as executable."""

    basis_mark = [
        v
        for row in rows
        if (v := _finite(row.get("basis_mark_bps"))) is not None
    ]
    basis_spot = [
        v
        for row in rows
        if (v := _finite(row.get("basis_spot_bps"))) is not None
    ]
    ordered_mark = sorted(abs(v) for v in basis_mark)
    thr = _percentile(ordered_mark, 0.99) or 0.0
    by_time = {_dt(row["decision_time"]): row for row in rows}
    horizons = (30, 60, 120, 300, 600)
    reversion: dict[str, Any] = {}
    for horizon in horizons:
        signed_changes: list[float] = []
        for ts, row in by_time.items():
            b0 = _finite(row.get("basis_mark_bps"))
            mid0 = _finite(row.get("mid"))
            if b0 is None or mid0 is None or abs(b0) < thr:
                continue
            future = by_time.get(ts + timedelta(seconds=horizon))
            if future is None:
                continue
            b1 = _finite(future.get("basis_mark_bps"))
            mid1 = _finite(future.get("mid"))
            if b1 is None or mid1 is None:
                continue
            # Reversion of basis toward 0 in the signed basis direction.
            signed_changes.append(-(b1 - b0) * (1.0 if b0 > 0 else -1.0))
            # Executable mid move in the trade direction that would fade basis:
            # if mark >> mid (positive basis_mark), fade by shorting perp.
        reversion[f"{horizon}s"] = {
            "n": len(signed_changes),
            "mean_basis_reversion_bps": _mean(signed_changes),
        }

    # Executable PnL proxy: after extreme |basis|, signed mid move fading basis.
    exec_fade: dict[str, Any] = {}
    for horizon in horizons:
        pnl: list[float] = []
        for ts, row in by_time.items():
            b0 = _finite(row.get("basis_mark_bps"))
            mid0 = _finite(row.get("mid"))
            if b0 is None or mid0 is None or abs(b0) < thr:
                continue
            future = by_time.get(ts + timedelta(seconds=horizon))
            if future is None or future.get("mid") is None:
                continue
            # Positive basis (mark>mid): short perp expects mid up toward mark? or
            # mark down. Executable fade: trade opposite to basis sign so profit if
            # mid moves toward mark (mid * sign(basis) rises).
            direction = 1.0 if b0 > 0 else -1.0
            move = (float(future["mid"]) / mid0 - 1.0) * 10_000.0
            pnl.append(direction * move)
        exec_fade[f"{horizon}s"] = {
            "n": len(pnl),
            "mean_signed_executable_mid_bps": _mean(pnl),
            "abs_move_summary": summarize_abs_moves([abs(x) for x in pnl]),
        }

    be = style_break_even_bps(holding_seconds=300.0, entry_taker=True, exit_taker=True)
    span = _seconds_span(rows)
    events = sum(
        1
        for row in rows
        if (v := _finite(row.get("basis_mark_bps"))) is not None and abs(v) >= thr
    )
    return {
        "family": "BASIS_DISLOCATION",
        "basis_mark_abs": {
            "n": len(ordered_mark),
            "p50": _percentile(ordered_mark, 0.50),
            "p95": _percentile(ordered_mark, 0.95),
            "p99": _percentile(ordered_mark, 0.99),
        },
        "basis_spot_abs": {
            "n": len(basis_spot),
            "p50": _percentile(sorted(abs(v) for v in basis_spot), 0.50),
            "p95": _percentile(sorted(abs(v) for v in basis_spot), 0.95),
            "p99": _percentile(sorted(abs(v) for v in basis_spot), 0.99),
        },
        "extreme_abs_threshold_bps": thr,
        "extreme_events": events,
        "events_per_day": events / max(span / 86400.0, 1e-9),
        "basis_reversion": reversion,
        "executable_fade_proxy": exec_fade,
        "break_even": be,
        "limitation": (
            "Mark/spot are references, not executable. PnL uses TOB mid only. "
            "Large basis may be reference mechanics rather than tradable dislocation."
        ),
    }


def screen_funding_carry(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """B: funding/carry feasibility — not arbitrage without hedge legs."""

    rates = [v for row in rows if (v := _finite(row.get("funding_rate"))) is not None]
    ordered = sorted(rates)
    abs_ordered = sorted(abs(v) for v in rates)
    # Persistence: correlation of consecutive non-null samples (1s cadence).
    pairs = []
    prev: float | None = None
    for row in sorted(rows, key=lambda r: _dt(r["decision_time"])):
        cur = _finite(row.get("funding_rate"))
        if cur is None:
            prev = None
            continue
        if prev is not None:
            pairs.append((prev, cur))
        prev = cur
    if len(pairs) > 10:
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        mx = sum(xs) / len(xs)
        my = sum(ys) / len(ys)
        num = sum((x - mx) * (y - my) for x, y in pairs)
        denx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        deny = math.sqrt(sum((y - my) ** 2 for y in ys))
        autocorr = num / (denx * deny) if denx > 0 and deny > 0 else None
    else:
        autocorr = None

    # Basis vs funding co-movement (coarse).
    joint = [
        (float(row["funding_rate"]), float(row["basis_mark_bps"]))
        for row in rows
        if _finite(row.get("funding_rate")) is not None
        and _finite(row.get("basis_mark_bps")) is not None
    ]
    if len(joint) > 10:
        fx = [p[0] for p in joint]
        bx = [p[1] for p in joint]
        mfx = sum(fx) / len(fx)
        mbx = sum(bx) / len(bx)
        num = sum((f - mfx) * (b - mbx) for f, b in joint)
        den = math.sqrt(sum((f - mfx) ** 2 for f in fx) * sum((b - mbx) ** 2 for b in bx))
        corr_fb = num / den if den > 0 else None
    else:
        corr_fb = None

    median_abs = _percentile(abs_ordered, 0.50) or 0.0
    # Hibachi funding_rate units: treat as fraction per 8h when |rate| is small.
    hold_hours = (1.0, 8.0, 24.0)
    carry = {}
    for hours in hold_hours:
        # Expected |carry| bps ≈ |rate| * hours/8 * 1e4
        expected_bps = median_abs * (hours / 8.0) * 10_000.0
        be = style_break_even_bps(
            holding_seconds=hours * 3600.0,
            entry_taker=True,
            exit_taker=True,
            funding_rate_per_8h=median_abs,
        )
        carry[f"{hours:g}h"] = {
            "median_abs_rate": median_abs,
            "expected_abs_carry_bps": expected_bps,
            "break_even": be,
            "covers_taker_rt_fees_alone": expected_bps
            > be["entry_fee_bps"] + be["exit_fee_bps"],
        }

    return {
        "family": "FUNDING_CARRY",
        "funding_rate": {
            "n": len(ordered),
            "mean": _mean(ordered),
            "p50": _percentile(ordered, 0.50),
            "p05": _percentile(ordered, 0.05),
            "p95": _percentile(ordered, 0.95),
            "abs_p50": _percentile(abs_ordered, 0.50),
            "abs_p95": _percentile(abs_ordered, 0.95),
        },
        "lag1_autocorr": autocorr,
        "corr_funding_basis_mark": corr_fb,
        "carry_vs_costs": carry,
        "hedge_note": (
            "One-sided perp carry is directional risk, not arb. True hedged carry "
            "needs an external spot/perp leg not available in Hibachi-only RAW."
        ),
        "break_even_8h": style_break_even_bps(
            holding_seconds=8 * 3600.0,
            entry_taker=True,
            exit_taker=True,
            funding_rate_per_8h=median_abs,
        ),
    }


def screen_liquidity_events(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """C: sparse causal liquidity/dislocation events and subsequent moves."""

    spreads = [
        v for row in rows if (v := _finite(row.get("spread_bps"))) is not None
    ]
    depths = [
        float(row["bid_size"]) + float(row["ask_size"])
        for row in rows
        if _finite(row.get("bid_size")) is not None
        and _finite(row.get("ask_size")) is not None
    ]
    ofi = [v for row in rows if (v := _finite(row.get("ofi_5s"))) is not None]
    micro = [
        v for row in rows if (v := _finite(row.get("microprice_dev_bps"))) is not None
    ]
    trade_counts = [
        v for row in rows if (v := _finite(row.get("trade_count"))) is not None
    ]
    spread_p99 = _percentile(sorted(spreads), 0.99) or 0.0
    depth_p01 = _percentile(sorted(depths), 0.01) or 0.0
    ofi_p99 = _percentile(sorted(abs(v) for v in ofi), 0.99) or 0.0
    micro_p99 = _percentile(sorted(abs(v) for v in micro), 0.99) or 0.0
    trade_p99 = _percentile(sorted(trade_counts), 0.99) or 0.0

    # Named predicates keep mypy happy and document causal definitions.
    def spread_widen(row: dict[str, Any]) -> bool:
        return (_finite(row.get("spread_bps")) or -1.0) >= spread_p99

    def depth_collapse(row: dict[str, Any]) -> bool:
        return (
            _finite(row.get("bid_size")) is not None
            and _finite(row.get("ask_size")) is not None
            and (float(row["bid_size"]) + float(row["ask_size"])) <= depth_p01
        )

    def ofi_shock(row: dict[str, Any]) -> bool:
        return abs(_finite(row.get("ofi_5s")) or 0.0) >= ofi_p99

    def microprice_extreme(row: dict[str, Any]) -> bool:
        return abs(_finite(row.get("microprice_dev_bps")) or 0.0) >= micro_p99

    def trade_burst(row: dict[str, Any]) -> bool:
        return (_finite(row.get("trade_count")) or 0.0) >= trade_p99

    definitions = {
        "spread_widen_p99": spread_widen,
        "depth_collapse_p01": depth_collapse,
        "ofi_shock_p99": ofi_shock,
        "microprice_extreme_p99": microprice_extreme,
        "trade_burst_p99": trade_burst,
    }

    by_time = {_dt(row["decision_time"]): row for row in rows}
    times = sorted(by_time)
    span = _seconds_span(rows)
    cooldown = 60  # non-overlapping event windows
    horizons = (15, 30, 60, 120, 300)
    out: dict[str, Any] = {}
    for name, pred in definitions.items():
        event_times: list[datetime] = []
        last: datetime | None = None
        for ts in times:
            if last is not None and (ts - last).total_seconds() < cooldown:
                continue
            if pred(by_time[ts]):
                event_times.append(ts)
                last = ts
        per_h: dict[str, Any] = {}
        for horizon in horizons:
            abs_moves: list[float] = []
            signed_rev: list[float] = []  # reversion after jump: -sign(ret_pre)*fut
            for ts in event_times:
                row = by_time[ts]
                mid0 = _finite(row.get("mid"))
                future = by_time.get(ts + timedelta(seconds=horizon))
                if mid0 is None or future is None or future.get("mid") is None:
                    continue
                move = absolute_executable_move_bps(mid0, float(future["mid"]))
                if move is not None:
                    abs_moves.append(move)
                # Mean-reversion proxy using prior 5s return if present.
                ret5 = _finite(row.get("ret_5s_bps"))
                if ret5 is not None and ret5 != 0:
                    fut = (float(future["mid"]) / mid0 - 1.0) * 10_000.0
                    signed_rev.append(-math.copysign(1.0, ret5) * fut)
            per_h[f"{horizon}s"] = {
                "abs": summarize_abs_moves(abs_moves),
                "mean_reversion_signed_bps": _mean(signed_rev),
                "reversion_n": len(signed_rev),
            }
        out[name] = {
            "events": len(event_times),
            "events_per_day": len(event_times) / max(span / 86400.0, 1e-9),
            "cooldown_s": cooldown,
            "thresholds": {
                "spread_p99": spread_p99,
                "depth_p01": depth_p01,
                "ofi_p99": ofi_p99,
                "micro_p99": micro_p99,
                "trade_p99": trade_p99,
            },
            "forward": per_h,
        }
    be = style_break_even_bps(holding_seconds=60.0)
    return {
        "family": "LIQUIDITY_EVENTS",
        "events": out,
        "break_even_60s_taker": be,
        "note": "Causal predeclared extremes with 60s cooldown; no combinatorial mining.",
    }


def screen_volatility_target(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """D/E: vol expansion and opportunity (|move|>cost) prevalence / precursors."""

    rvs = [v for row in rows if (v := _finite(row.get("rv_60s_bps"))) is not None]
    low = _percentile(sorted(rvs), 1 / 3) or 0.0
    high = _percentile(sorted(rvs), 2 / 3) or 0.0
    by_time = {_dt(row["decision_time"]): row for row in rows}
    times = sorted(by_time)
    transitions: list[datetime] = []
    last_fire: datetime | None = None
    for i, ts in enumerate(times[:-1]):
        cur = _finite(by_time[ts].get("rv_60s_bps"))
        nxt = _finite(by_time[times[i + 1]].get("rv_60s_bps"))
        if cur is None or nxt is None:
            continue
        if (
            cur <= low
            and nxt >= high
            and (last_fire is None or (ts - last_fire).total_seconds() >= 300)
        ):
            transitions.append(ts)
            last_fire = ts

    horizon = 300
    post_abs: list[float] = []
    for ts in transitions:
        mid0 = _finite(by_time[ts].get("mid"))
        future = by_time.get(ts + timedelta(seconds=horizon))
        if mid0 is None or future is None or future.get("mid") is None:
            continue
        move = absolute_executable_move_bps(mid0, float(future["mid"]))
        if move is not None:
            post_abs.append(move)

    # Stage-1 opportunity labels on non-overlapping 60s / 300s strides.
    cost = style_break_even_bps(holding_seconds=60.0)["required_move_bps"]
    safety = cost + 5.0
    stage1: dict[str, Any] = {}
    for horizon_s, thr in ((60, cost), (60, safety), (300, cost), (300, safety)):
        hits = 0
        total = 0
        if not times:
            stage1[f"{horizon_s}s_ge_{thr:.1f}"] = {"n": 0, "prevalence": None}
            continue
        cursor = times[0]
        end = times[-1]
        while cursor <= end:
            fut = by_time.get(cursor + timedelta(seconds=horizon_s))
            now = by_time.get(cursor)
            if (
                now is not None
                and fut is not None
                and now.get("mid") is not None
                and fut.get("mid") is not None
            ):
                move = absolute_executable_move_bps(float(now["mid"]), float(fut["mid"]))
                if move is not None:
                    total += 1
                    if move >= thr:
                        hits += 1
            cursor = cursor + timedelta(seconds=horizon_s)
        stage1[f"{horizon_s}s_ge_{thr:.1f}bps"] = {
            "n": total,
            "hits": hits,
            "prevalence": (hits / total) if total else None,
            "threshold_bps": thr,
        }

    # Coarse precursor: does high |ofi_5s| or |microprice| raise P(opportunity)?
    precursor_report: dict[str, Any] = {}
    for feature in ("ofi_5s", "microprice_dev_bps", "rv_60s_bps"):
        vals = [
            abs(float(row[feature]))
            for row in rows
            if _finite(row.get(feature)) is not None
        ]
        cut = _percentile(sorted(vals), 0.90) or 0.0
        hit_hi = 0
        n_hi = 0
        hit_lo = 0
        n_lo = 0
        for ts in times[::60]:  # ~non-overlapping minute samples
            row = by_time[ts]
            feat = _finite(row.get(feature))
            mid0 = _finite(row.get("mid"))
            fut = by_time.get(ts + timedelta(seconds=60))
            if feat is None or mid0 is None or fut is None or fut.get("mid") is None:
                continue
            move = absolute_executable_move_bps(mid0, float(fut["mid"]))
            if move is None:
                continue
            opportunity = move >= safety
            if abs(feat) >= cut:
                n_hi += 1
                hit_hi += int(opportunity)
            else:
                n_lo += 1
                hit_lo += int(opportunity)
        precursor_report[feature] = {
            "p90_abs": cut,
            "p_opp_high": (hit_hi / n_hi) if n_hi else None,
            "p_opp_low": (hit_lo / n_lo) if n_lo else None,
            "n_high": n_hi,
            "n_low": n_lo,
            "lift": (
                (hit_hi / n_hi) / (hit_lo / n_lo)
                if n_hi and n_lo and hit_lo > 0
                else None
            ),
        }

    span = _seconds_span(rows)
    return {
        "family": "VOLATILITY_AND_OPPORTUNITY_TARGET",
        "rv_60s_tertiles": {"low": low, "high": high},
        "low_to_high_transitions": {
            "events": len(transitions),
            "events_per_day": len(transitions) / max(span / 86400.0, 1e-9),
            "post_300s_abs_moves": summarize_abs_moves(post_abs),
        },
        "stage1_opportunity_prevalence_nonoverlap": stage1,
        "stage1_precursor_lift": precursor_report,
        "break_even_60s": style_break_even_bps(holding_seconds=60.0),
        "note": (
            "Stage-1 = P(|executable move| > cost[+safety]). No ML; coarse lifts only."
        ),
    }
