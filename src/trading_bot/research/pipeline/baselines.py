"""Cost-aware baseline strategies on market_state_1s (pre-ML)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trading_bot.research.replay import (
    CostConfig,
    SimulatedTrade,
    calculate_trade,
    maximum_drawdown,
)

StrategyName = Literal["momentum", "mean_reversion", "imbalance"]


@dataclass(frozen=True, slots=True)
class MarketStateBaselineConfig:
    name: StrategyName
    lookback_seconds: int = 5
    entry_threshold: float = 5.0  # bps or imbalance units*100 depending on strategy
    holding_seconds: int = 15
    cooldown_seconds: int = 5
    notional: float = 1_000.0
    require_valid_book: bool = True

    def __post_init__(self) -> None:
        if self.lookback_seconds < 1 or self.holding_seconds < 1:
            raise ValueError("lookback/holding must be positive")
        if self.cooldown_seconds < 0 or self.notional <= 0:
            raise ValueError("invalid cooldown/notional")


def _signal(
    rows: list[dict[str, Any]],
    index: int,
    cfg: MarketStateBaselineConfig,
) -> int | None:
    row = rows[index]
    if cfg.require_valid_book and not row.get("valid_book"):
        return None
    if cfg.name == "momentum":
        ret = row.get(f"ret_{cfg.lookback_seconds}s_bps")
        if ret is None:
            # fall back to ret_5s when lookback matches common column
            ret = row.get("ret_5s_bps")
        if ret is None or abs(ret) < cfg.entry_threshold:
            return None
        return 1 if ret > 0 else -1
    if cfg.name == "mean_reversion":
        ret = row.get("ret_5s_bps")
        if ret is None or abs(ret) < cfg.entry_threshold:
            return None
        return -1 if ret > 0 else 1
    if cfg.name == "imbalance":
        imb = row.get("imbalance")
        if imb is None:
            return None
        score = imb * 100.0
        if abs(score) < cfg.entry_threshold:
            return None
        return 1 if score > 0 else -1
    raise ValueError(f"unknown strategy {cfg.name}")


def _exec_price(row: dict[str, Any], direction: int) -> float:
    """Taker-style fill at ask for buys / bid for sells (never frictionless mid)."""

    if direction > 0:
        return float(row["best_ask"])
    return float(row["best_bid"])


def replay_market_state_baseline(
    market_state_path: Path,
    *,
    signal: MarketStateBaselineConfig,
    costs: CostConfig | None = None,
) -> dict[str, Any]:
    costs = costs or CostConfig()
    rows = pq.read_table(market_state_path).to_pylist()
    rows.sort(key=lambda r: r["decision_time"])
    trades: list[SimulatedTrade] = []
    position: tuple[int, datetime, float] | None = None
    cooldown_until: datetime | None = None

    for index, row in enumerate(rows):
        ts: datetime = row["decision_time"]
        if position is not None:
            direction, entry_time, entry_price = position
            held = (ts - entry_time).total_seconds()
            if held >= signal.holding_seconds:
                exit_price = _exec_price(row, -direction)
                trades.append(
                    calculate_trade(
                        direction=direction,
                        entry_time=entry_time,
                        exit_time=ts,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        notional=signal.notional,
                        costs=costs,
                    )
                )
                position = None
                cooldown_until = ts
            continue
        if cooldown_until is not None and (
            (ts - cooldown_until).total_seconds() < signal.cooldown_seconds
        ):
            continue
        maybe_direction = _signal(rows, index, signal)
        if maybe_direction is None:
            continue
        direction = maybe_direction
        exec_index = index + costs.execution_delay_seconds
        if exec_index >= len(rows):
            continue
        exec_row = rows[exec_index]
        if signal.require_valid_book and not exec_row.get("valid_book"):
            continue
        entry_price = _exec_price(exec_row, direction)
        position = (direction, exec_row["decision_time"], entry_price)

    pnls = [t.net_pnl for t in trades]
    return {
        "strategy": signal.name,
        "configuration_hash": _hash_cfg(signal, costs),
        "trades": len(trades),
        "gross_pnl": sum(t.gross_pnl for t in trades),
        "fees": sum(t.fees for t in trades),
        "funding": sum(t.funding for t in trades),
        "slippage": sum(t.slippage for t in trades),
        "net_pnl": sum(pnls),
        "max_drawdown": maximum_drawdown(pnls),
        "costs": asdict(costs),
        "signal": asdict(signal),
        "trade_details": [asdict(t) for t in trades],
    }


def _hash_cfg(signal: MarketStateBaselineConfig, costs: CostConfig) -> str:
    import hashlib
    import json

    payload = json.dumps(
        {"signal": asdict(signal), "costs": asdict(costs)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()
