from collections import deque
from dataclasses import dataclass
from datetime import datetime
from statistics import fmean
from typing import Any


def _positive(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _lookup(payload: dict[str, Any], names: tuple[str, ...]) -> float | None:
    containers = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        containers.append(data)
    for container in containers:
        for name in names:
            value = _positive(container.get(name))
            if value is not None:
                return value
    return None


def mid_price(payload: dict[str, Any]) -> float | None:
    bid = _lookup(payload, ("bidPrice", "bid_price", "bid"))
    ask = _lookup(payload, ("askPrice", "ask_price", "ask"))
    if bid is not None and ask is not None and bid <= ask:
        return (bid + ask) / 2
    return _lookup(payload, ("markPrice", "mark_price", "price", "tradePrice"))


@dataclass(frozen=True, slots=True)
class MomentumConfig:
    window: int = 20
    threshold_bps: float = 5.0

    def __post_init__(self) -> None:
        if self.window < 2 or self.threshold_bps <= 0:
            raise ValueError("Momentum window must be >= 2 and threshold must be positive.")


@dataclass(frozen=True, slots=True)
class MomentumSignal:
    timestamp: datetime
    direction: int
    mid_price: float
    momentum_bps: float


class MomentumSignalCallback:
    def __init__(self, config: MomentumConfig | None = None) -> None:
        self.config = config or MomentumConfig()
        self._prices: deque[float] = deque(maxlen=self.config.window)
        self._state = 0
        self.last_timestamp: datetime | None = None
        self.last_price: float | None = None

    def __call__(self, event: dict[str, Any]) -> MomentumSignal | None:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return None
        price = mid_price(payload)
        if price is None:
            return None
        timestamp = event["exchange_at"] or event["received_at"]
        self.last_timestamp = timestamp
        self.last_price = price
        self._prices.append(price)
        if len(self._prices) < self.config.window:
            return None
        momentum_bps = (price / fmean(self._prices) - 1) * 10_000
        state = 1 if momentum_bps >= self.config.threshold_bps else (
            -1 if momentum_bps <= -self.config.threshold_bps else 0
        )
        if state == 0:
            self._state = 0
            return None
        if state == self._state:
            return None
        self._state = state
        return MomentumSignal(timestamp, state, price, momentum_bps)
