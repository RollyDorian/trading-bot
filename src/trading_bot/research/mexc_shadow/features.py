"""Pluggable momentum and gap features. Definitions are hypotheses, not facts."""

from __future__ import annotations

from collections import deque
from datetime import timedelta

from trading_bot.research.mexc_shadow.config import SignalParams
from trading_bot.research.mexc_shadow.types import (
    GAP_LAST_VS_MARK,
    GAP_MID_VS_INDEX,
    GAP_MID_VS_MARK,
    MOM_MID_RETURN_LOOKBACK,
    MOM_MID_VS_SMA,
    FeatureSnapshot,
    Observation,
)


class FeatureEngine:
    """Causal rolling features. A print only sees observations at or before it."""

    def __init__(self, params: SignalParams) -> None:
        self._params = params
        # Bounded causal buffer per symbol. Seconds-based lookback needs depth.
        self._by_symbol: dict[str, deque[Observation]] = {}

    def update(self, observation: Observation) -> FeatureSnapshot:
        history = self._by_symbol.setdefault(observation.symbol, deque(maxlen=4096))
        history.append(observation)
        return FeatureSnapshot(
            observed_at=observation.observed_at,
            symbol=observation.symbol,
            mom_bps=self._momentum(history),
            gap_bps=self._gap(observation),
            mid=observation.executable_mid(),
            momentum_definition=self._params.momentum_definition,
            gap_definition=self._params.gap_definition,
        )

    def _momentum(self, history: deque[Observation]) -> float | None:
        now = history[-1]
        now_mid = now.executable_mid()
        if now_mid is None:
            return None
        definition = self._params.momentum_definition
        if definition == MOM_MID_VS_SMA:
            mids = [item.executable_mid() for item in history]
            window = [
                price
                for price in mids[-self._params.momentum_lookback :]
                if price is not None and price > 0
            ]
            if len(window) < self._params.momentum_lookback:
                return None
            mean = sum(window) / len(window)
            return (now_mid / mean - 1.0) * 10_000.0
        then = self._reference(history)
        if then is None:
            return None
        then_mid = then.executable_mid()
        if then_mid is None or then_mid <= 0:
            return None
        if definition == MOM_MID_RETURN_LOOKBACK:
            return (now_mid / then_mid - 1.0) * 10_000.0
        return None

    def _reference(self, history: deque[Observation]) -> Observation | None:
        now = history[-1]
        seconds = self._params.momentum_lookback_seconds
        if seconds is not None:
            target = now.observed_at - timedelta(seconds=seconds)
            chosen: Observation | None = None
            for item in history:
                if item.observed_at <= target:
                    chosen = item
            if chosen is None or chosen.observed_at == now.observed_at:
                return None
            return chosen
        index = len(history) - 1 - self._params.momentum_lookback
        if index < 0:
            return None
        return history[index]

    def _gap(self, observation: Observation) -> float | None:
        mid = observation.executable_mid()
        definition = self._params.gap_definition
        if definition == GAP_MID_VS_MARK:
            ref = observation.mark
        elif definition == GAP_MID_VS_INDEX:
            ref = observation.index
        elif definition == GAP_LAST_VS_MARK:
            mid = observation.last
            ref = observation.mark
        else:
            return None
        if mid is None or ref is None or mid <= 0 or ref <= 0:
            return None
        return (mid / ref - 1.0) * 10_000.0
