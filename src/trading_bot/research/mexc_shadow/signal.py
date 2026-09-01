"""Candidate construction. Every raw candidate is stored; throttle is separate."""

from __future__ import annotations

from datetime import datetime, timedelta

from trading_bot.research.mexc_shadow.config import EngineConfig, SignalParams, ThrottleParams
from trading_bot.research.mexc_shadow.types import (
    Candidate,
    Direction,
    FeatureSnapshot,
    ThrottleReason,
)


def classify_direction(mom_bps: float, gap_bps: float) -> Direction | None:
    """Author-log pattern as a hypothesis: long mom>0 gap<0; short mom<0 gap>0."""

    if mom_bps > 0 and gap_bps < 0:
        return "long"
    if mom_bps < 0 and gap_bps > 0:
        return "short"
    return None


def passes_abs_filters(params: SignalParams, mom_bps: float, gap_bps: float) -> bool:
    return abs(mom_bps) >= params.mom_abs_min_bps and abs(gap_bps) >= params.gap_abs_min_bps


class CandidateGate:
    def __init__(self, config: EngineConfig) -> None:
        self._config = config
        # Hourly/daily caps are profile-level research controls, not per-symbol.
        self._accepted_at: list[datetime] = []

    def evaluate(
        self,
        features: FeatureSnapshot,
        *,
        position_open: bool,
        notional_multiplier: float,
    ) -> Candidate | None:
        params = self._config.for_symbol(features.symbol).signal
        if features.mom_bps is None or features.gap_bps is None:
            return None
        direction = classify_direction(features.mom_bps, features.gap_bps)
        if direction is None:
            return None
        if not passes_abs_filters(params, features.mom_bps, features.gap_bps):
            return Candidate(
                observed_at=features.observed_at,
                symbol=features.symbol,
                direction=direction,
                mom_bps=features.mom_bps,
                gap_bps=features.gap_bps,
                target_bps=params.target_multiplier * abs(features.gap_bps),
                throttle="filters_not_met",
                accepted_for_shadow=False,
                notional_multiplier=notional_multiplier,
            )
        target = params.target_multiplier * abs(features.gap_bps)
        throttle = self._throttle_reason(
            self._config.for_symbol(features.symbol).throttle,
            features.observed_at,
            position_open=position_open,
        )
        accepted = throttle == "accepted"
        if accepted:
            self._accepted_at.append(features.observed_at)
        return Candidate(
            observed_at=features.observed_at,
            symbol=features.symbol,
            direction=direction,
            mom_bps=features.mom_bps,
            gap_bps=features.gap_bps,
            target_bps=target,
            throttle=throttle,
            accepted_for_shadow=accepted,
            notional_multiplier=notional_multiplier,
        )

    def _throttle_reason(
        self,
        throttle: ThrottleParams,
        when: datetime,
        *,
        position_open: bool,
    ) -> ThrottleReason:
        if position_open:
            return "position_open"
        stamps = self._accepted_at
        if throttle.max_shadow_per_hour is not None:
            window = when - timedelta(hours=1)
            hourly = sum(1 for stamp in stamps if stamp > window)
            if hourly >= throttle.max_shadow_per_hour:
                return "max_per_hour"
        if throttle.max_shadow_per_day is not None:
            day = when.date()
            daily = sum(1 for stamp in stamps if stamp.date() == day)
            if daily >= throttle.max_shadow_per_day:
                return "max_per_day"
        return "accepted"
