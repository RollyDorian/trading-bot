"""Deterministic research signals and account-free PAPER execution."""

from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from math import sqrt
from statistics import fmean, pstdev
from typing import Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _signed_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _lookup(payload: dict[str, Any], names: tuple[str, ...]) -> float | None:
    containers = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        containers.append(data)
    for container in containers:
        for name in names:
            if name in container:
                found = _number(container[name])
                if found is not None:
                    return found
    return None


def _level_price(value: Any) -> float | None:
    if isinstance(value, dict):
        return _lookup(value, ("price", "p", "px"))
    if isinstance(value, list | tuple) and value:
        return _number(value[0])
    return None


def _best_level(payload: dict[str, Any], side_names: tuple[str, ...]) -> float | None:
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return None
    for name in side_names:
        side = data.get(name)
        if isinstance(side, dict):
            levels = side.get("levels", side.get("prices"))
            if isinstance(levels, list) and levels:
                return _level_price(levels[0])
            price = _level_price(side)
            if price is not None:
                return price
        if isinstance(side, list) and side:
            return _level_price(side[0])
    return None


@dataclass(frozen=True, slots=True)
class PaperConfig:
    initial_cash: float = 10_000.0
    order_notional: float = 1_000.0
    taker_fee_rate: float = 0.00045
    extra_slippage_bps: float = 1.0
    fast_window: int = 20
    slow_window: int = 60
    signal_threshold_bps: float = 2.0
    max_drawdown_fraction: float = 0.10

    def __post_init__(self) -> None:
        if self.initial_cash <= 0 or self.order_notional <= 0:
            raise ValueError("Cash and order notional must be positive.")
        if not 1 < self.fast_window < self.slow_window:
            raise ValueError("Signal windows must satisfy 1 < fast < slow.")
        if not 0 <= self.max_drawdown_fraction < 1:
            raise ValueError("Max drawdown must be in [0, 1).")


@dataclass(frozen=True, slots=True)
class PaperFill:
    timestamp: datetime
    side: str
    quantity: float
    price: float
    fee: float
    reason: str
    realized_pnl: float


class PaperEngine:
    """Single-position simulator using observable bid/ask plus explicit costs."""

    def __init__(self, config: PaperConfig | None = None) -> None:
        self.config = config or PaperConfig()
        self.cash = self.config.initial_cash
        self.position = 0.0
        self.entry_price: float | None = None
        self.mark: float | None = None
        self.bid: float | None = None
        self.ask: float | None = None
        self.funding_rate = 0.0
        self.funding_paid = 0.0
        self.fees_paid = 0.0
        self.realized_pnl = 0.0
        self.prices: deque[float] = deque(maxlen=self.config.slow_window)
        self.fills: list[PaperFill] = []
        self.equity_curve: list[tuple[datetime, float]] = []
        self._last_time: datetime | None = None
        self._peak_equity = self.config.initial_cash
        self._halted = False

    def _equity(self) -> float:
        if self.mark is None or self.entry_price is None:
            return self.cash
        return self.cash + self.position * (self.mark - self.entry_price)

    def _apply_funding(self, timestamp: datetime) -> None:
        if self._last_time is None or self.mark is None or self.position == 0:
            self._last_time = timestamp
            return
        seconds = max(0.0, (timestamp - self._last_time).total_seconds())
        payment = self.position * self.mark * self.funding_rate * seconds / 28_800
        self.cash -= payment
        self.funding_paid += payment
        self._last_time = timestamp

    def _fill(self, target: int, timestamp: datetime, reason: str) -> None:
        current = 1 if self.position > 0 else -1 if self.position < 0 else 0
        if target == current or self.bid is None or self.ask is None:
            return
        if current:
            side = "sell" if current > 0 else "buy"
            raw_price = self.bid if side == "sell" else self.ask
            slip = self.config.extra_slippage_bps / 10_000
            price = raw_price * (1 - slip if side == "sell" else 1 + slip)
            quantity = abs(self.position)
            pnl = self.position * (price - (self.entry_price or price))
            fee = quantity * price * self.config.taker_fee_rate
            self.cash += pnl - fee
            self.realized_pnl += pnl
            self.fees_paid += fee
            self.fills.append(PaperFill(timestamp, side, quantity, price, fee, reason, pnl))
            self.position = 0.0
            self.entry_price = None
        if target and not self._halted:
            side = "buy" if target > 0 else "sell"
            raw_price = self.ask if side == "buy" else self.bid
            slip = self.config.extra_slippage_bps / 10_000
            price = raw_price * (1 + slip if side == "buy" else 1 - slip)
            quantity = self.config.order_notional / price
            fee = quantity * price * self.config.taker_fee_rate
            self.cash -= fee
            self.fees_paid += fee
            self.position = quantity * target
            self.entry_price = price
            self.fills.append(PaperFill(timestamp, side, quantity, price, fee, reason, 0.0))

    def on_event(self, payload: dict[str, Any], timestamp: datetime | None = None) -> None:
        timestamp = timestamp or datetime.now(UTC)
        self._apply_funding(timestamp)
        topic = str(payload.get("topic", ""))
        if topic == "mark_price":
            self.mark = _lookup(payload, ("markPrice", "mark_price", "price", "value"))
        elif topic == "funding_rate_estimation":
            containers = [payload, payload.get("data")]
            for container in containers:
                if not isinstance(container, dict):
                    continue
                for name in ("fundingRate", "funding_rate", "rate", "value"):
                    rate = _signed_number(container.get(name))
                    if rate is not None:
                        self.funding_rate = rate
                        break
        elif topic == "ask_bid_price":
            self.bid = _lookup(payload, ("bidPrice", "bid_price", "bid"))
            self.ask = _lookup(payload, ("askPrice", "ask_price", "ask"))
        elif topic == "orderbook":
            self.bid = _best_level(payload, ("bid", "bids", "buy")) or self.bid
            self.ask = _best_level(payload, ("ask", "asks", "sell")) or self.ask
        elif topic == "trades" and self.mark is None:
            self.mark = _lookup(payload, ("price", "tradePrice", "trade_price"))
        if self.mark is None or self.bid is None or self.ask is None or self.bid > self.ask:
            return
        self.prices.append(self.mark)
        if len(self.prices) == self.config.slow_window and not self._halted:
            values = list(self.prices)
            fast = fmean(values[-self.config.fast_window :])
            slow = fmean(values)
            edge_bps = (fast / slow - 1) * 10_000
            if edge_bps > self.config.signal_threshold_bps:
                target = 1
            elif edge_bps < -self.config.signal_threshold_bps:
                target = -1
            else:
                target = 0
            self._fill(target, timestamp, "moving_average_signal")
        equity = self._equity()
        self._peak_equity = max(self._peak_equity, equity)
        if equity <= self._peak_equity * (1 - self.config.max_drawdown_fraction):
            self._halted = True
            self._fill(0, timestamp, "max_drawdown")
        self.equity_curve.append((timestamp, self._equity()))

    def close(self, timestamp: datetime | None = None) -> None:
        self._fill(0, timestamp or datetime.now(UTC), "end_of_run")

    def report(self) -> dict[str, Any]:
        equities = [value for _, value in self.equity_curve]
        returns = [b / a - 1 for a, b in zip(equities, equities[1:], strict=False) if a]
        sharpe = 0.0
        if len(returns) > 1 and pstdev(returns) > 0:
            sharpe = fmean(returns) / pstdev(returns) * sqrt(len(returns))
        closed = [fill for fill in self.fills if fill.realized_pnl != 0]
        wins = sum(fill.realized_pnl > 0 for fill in closed)
        equity = self._equity()
        return {
            "initial_cash": self.config.initial_cash,
            "final_equity": equity,
            "net_pnl": equity - self.config.initial_cash,
            "return_fraction": equity / self.config.initial_cash - 1,
            "realized_pnl_before_costs": self.realized_pnl,
            "fees_paid": self.fees_paid,
            "funding_paid": self.funding_paid,
            "fills": len(self.fills),
            "closed_trades": len(closed),
            "win_rate": wins / len(closed) if closed else 0.0,
            "max_drawdown_fraction": 1 - min(equities, default=equity) / max(self._peak_equity, 1),
            "sample_sharpe": sharpe,
            "halted_by_risk": self._halted,
            "open_position": self.position,
            "fill_log": [asdict(fill) for fill in self.fills],
        }
