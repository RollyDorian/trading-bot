"""Conservative maker fill simulation from public market_state + trades.

No order IDs or exchange queue priority are available. Scenarios bound
fill outcomes under optimistic/base/conservative volume-through assumptions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

MakerScenario = Literal["optimistic", "base", "conservative"]
Side = Literal["buy", "sell"]

MAKER_DATA_SUPPORT = {
    "price_level_fill_bounds": True,
    "exact_queue_position": False,
    "order_ids": False,
    "exchange_sequence": False,
    "public_trades_and_tob": True,
    "assessment": (
        "Public depth-20 books, TOB quotes, and trades support only fill "
        "upper/lower bounds via volume-through / trade-through rules. Exact "
        "historical maker fills cannot be claimed without queue priority."
    ),
}


@dataclass(frozen=True, slots=True)
class MakerOrderIntent:
    decision_time: datetime
    side: Side
    limit_price: float
    notional_usd: float
    signal: float
    feature: str
    max_wait_seconds: int = 30


@dataclass(frozen=True, slots=True)
class MakerFillResult:
    filled: bool
    scenario: MakerScenario
    fill_time: datetime | None
    time_to_fill_seconds: float | None
    fill_price: float | None
    reason: str


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def join_best_quote_order(
    row: dict[str, Any],
    *,
    side: Side,
    feature: str,
    notional_usd: float = 1_000.0,
    max_wait_seconds: int = 30,
) -> MakerOrderIntent | None:
    """Place at best bid (buy) or best ask (sell); requires valid TOB."""

    bid = row.get("best_bid")
    ask = row.get("best_ask")
    signal = row.get(feature)
    if bid is None or ask is None or signal is None:
        return None
    if float(bid) <= 0 or float(ask) <= 0 or float(bid) >= float(ask):
        return None
    if not math.isfinite(float(signal)) or float(signal) == 0:
        return None
    # Direction follows signal: long when feature > 0.
    desired: Side = "buy" if float(signal) > 0 else "sell"
    if desired != side:
        return None
    limit = float(bid) if side == "buy" else float(ask)
    return MakerOrderIntent(
        decision_time=_dt(row["decision_time"]),
        side=side,
        limit_price=limit,
        notional_usd=notional_usd,
        signal=float(signal),
        feature=feature,
        max_wait_seconds=max_wait_seconds,
    )


def _required_volume_usd(scenario: MakerScenario, notional_usd: float) -> float:
    # Queue uncertainty: require more public opposing volume as conservatism rises.
    if scenario == "optimistic":
        return notional_usd
    if scenario == "base":
        return 2.0 * notional_usd
    return 5.0 * notional_usd


def simulate_maker_fill(
    rows_by_time: dict[datetime, dict[str, Any]],
    intent: MakerOrderIntent,
    *,
    scenario: MakerScenario,
) -> MakerFillResult:
    """Estimate whether a resting join-TOB order would fill under scenario rules.

    Buy at P:
      optimistic/base: accumulate sell_volume * mid while best_bid <= P + tiny eps
      conservative: require trade-through best_ask <= P
    Sell at P: symmetric with buy_volume / best_bid >= P.
    """

    times = sorted(t for t in rows_by_time if t > intent.decision_time)
    deadline = intent.decision_time + timedelta(seconds=intent.max_wait_seconds)
    needed = _required_volume_usd(scenario, intent.notional_usd)
    accumulated = 0.0
    eps = abs(intent.limit_price) * 1e-10

    for ts in times:
        if ts > deadline:
            break
        row = rows_by_time[ts]
        bid = row.get("best_bid")
        ask = row.get("best_ask")
        mid = row.get("mid")
        if bid is None or ask is None or mid is None or float(mid) <= 0:
            continue
        bid_f = float(bid)
        ask_f = float(ask)
        mid_f = float(mid)

        if intent.side == "buy":
            if scenario == "conservative":
                # Trade-through: market sells aggressively through our bid.
                if ask_f <= intent.limit_price + eps:
                    return MakerFillResult(
                        True,
                        scenario,
                        ts,
                        (ts - intent.decision_time).total_seconds(),
                        intent.limit_price,
                        "ask_trade_through_limit",
                    )
                continue
            # Volume-through while our bid is still at/above limit (still competitive).
            if bid_f <= intent.limit_price + eps:
                sell_vol = float(row.get("sell_volume") or 0.0)
                accumulated += max(0.0, sell_vol) * mid_f
                if accumulated >= needed:
                    return MakerFillResult(
                        True,
                        scenario,
                        ts,
                        (ts - intent.decision_time).total_seconds(),
                        intent.limit_price,
                        f"sell_volume_through_{scenario}",
                    )
        else:
            if scenario == "conservative":
                if bid_f >= intent.limit_price - eps:
                    return MakerFillResult(
                        True,
                        scenario,
                        ts,
                        (ts - intent.decision_time).total_seconds(),
                        intent.limit_price,
                        "bid_trade_through_limit",
                    )
                continue
            if ask_f >= intent.limit_price - eps:
                buy_vol = float(row.get("buy_volume") or 0.0)
                accumulated += max(0.0, buy_vol) * mid_f
                if accumulated >= needed:
                    return MakerFillResult(
                        True,
                        scenario,
                        ts,
                        (ts - intent.decision_time).total_seconds(),
                        intent.limit_price,
                        f"buy_volume_through_{scenario}",
                    )

    return MakerFillResult(
        False,
        scenario,
        None,
        None,
        None,
        "expired_unfilled",
    )


def post_fill_mid_moves_bps(
    rows_by_time: dict[datetime, dict[str, Any]],
    *,
    fill_time: datetime,
    fill_side: Side,
    horizons: tuple[int, ...] = (1, 5, 15, 30, 60),
) -> dict[str, float | None]:
    """Adverse for buys if mid falls; report signed move in position direction."""

    fill_row = rows_by_time.get(fill_time)
    if fill_row is None or fill_row.get("mid") in (None, 0):
        return {f"{h}s": None for h in horizons}
    fill_mid = float(fill_row["mid"])
    direction = 1.0 if fill_side == "buy" else -1.0
    out: dict[str, float | None] = {}
    for horizon in horizons:
        future = rows_by_time.get(fill_time + timedelta(seconds=horizon))
        if future is None or future.get("mid") in (None, 0):
            out[f"{horizon}s"] = None
            continue
        move = (float(future["mid"]) / fill_mid - 1.0) * 10_000.0
        out[f"{horizon}s"] = direction * move
    return out


def summarize_maker_campaign(
    rows: list[dict[str, Any]],
    *,
    feature: str,
    abs_threshold: float,
    scenarios: tuple[MakerScenario, ...] = ("optimistic", "base", "conservative"),
    notional_usd: float = 1_000.0,
    max_wait_seconds: int = 30,
    hold_seconds: int = 15,
) -> dict[str, Any]:
    """Run join-TOB maker intents for extreme |feature| events (exploratory)."""

    ordered = sorted(rows, key=lambda row: _dt(row["decision_time"]))
    by_time = {_dt(row["decision_time"]): row for row in ordered}
    intents: list[MakerOrderIntent] = []
    for row in ordered:
        value = row.get(feature)
        if value is None or not math.isfinite(float(value)):
            continue
        if abs(float(value)) < abs_threshold:
            continue
        side: Side = "buy" if float(value) > 0 else "sell"
        intent = join_best_quote_order(
            row,
            side=side,
            feature=feature,
            notional_usd=notional_usd,
            max_wait_seconds=max_wait_seconds,
        )
        if intent is not None:
            intents.append(intent)

    adverse_horizons = (1, 5, 15, 30, 60)
    by_scenario: dict[str, Any] = {}
    for scenario in scenarios:
        fills = 0
        ttfs: list[float] = []
        adverse_by_h: dict[str, list[float]] = {f"{h}s": [] for h in adverse_horizons}
        post_hold: list[float] = []
        for intent in intents:
            result = simulate_maker_fill(by_time, intent, scenario=scenario)
            if not result.filled or result.fill_time is None:
                continue
            fills += 1
            if result.time_to_fill_seconds is not None:
                ttfs.append(result.time_to_fill_seconds)
            moves = post_fill_mid_moves_bps(
                by_time,
                fill_time=result.fill_time,
                fill_side=intent.side,
                horizons=(*adverse_horizons, hold_seconds),
            )
            for key, bucket in adverse_by_h.items():
                move = moves.get(key)
                if move is not None:
                    bucket.append(move)
            hold_key = f"{hold_seconds}s"
            hold_move = moves.get(hold_key)
            if hold_move is not None:
                post_hold.append(hold_move)
        submitted = len(intents)
        fill_rate = fills / submitted if submitted else None
        by_scenario[scenario] = {
            "submitted": submitted,
            "fills": fills,
            "fill_rate": fill_rate,
            "unfilled_rate": (1.0 - fill_rate) if fill_rate is not None else None,
            "cancelled_or_expired_rate": (
                (1.0 - fill_rate) if fill_rate is not None else None
            ),
            "ttf_median_s": _median(ttfs),
            "ttf_p90_s": _percentile(sorted(ttfs), 0.90),
            "post_fill_signed_mid_mean_bps": {
                key: _mean(vals) for key, vals in adverse_by_h.items()
            },
            "post_fill_signed_mid_15s_mean_bps": _mean(adverse_by_h["15s"]),
            "post_fill_signed_mid_hold_mean_bps": _mean(post_hold),
            # Maker fee 0; unfilled are not trades.
            "note": (
                "gross post-fill mid move is not complete PnL; exits still required. "
                "Negative post-fill signed mid indicates adverse selection."
            ),
        }
    return {
        "feature": feature,
        "abs_threshold": abs_threshold,
        "notional_usd": notional_usd,
        "max_wait_seconds": max_wait_seconds,
        "hold_seconds": hold_seconds,
        "data_support": MAKER_DATA_SUPPORT,
        "scenarios": by_scenario,
    }


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


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
