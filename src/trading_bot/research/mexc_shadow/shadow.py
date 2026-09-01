"""Virtual / shadow positions. No exchange orders are created."""

from __future__ import annotations

from dataclasses import dataclass

from trading_bot.research.mexc_shadow.config import ShadowParams
from trading_bot.research.mexc_shadow.types import (
    EXIT_PRIORITY,
    Candidate,
    ExitReason,
    Observation,
    ShadowTrade,
)


def executable_pnl_bps(
    direction: str,
    entry_bid: float,
    entry_ask: float,
    exit_bid: float,
    exit_ask: float,
) -> float:
    """Long: enter ask, exit bid. Short: enter bid, exit ask. Spread is inside gross."""

    if direction == "long":
        if entry_ask <= 0:
            raise ValueError("entry ask must be positive")
        return (exit_bid / entry_ask - 1.0) * 10_000.0
    if entry_bid <= 0:
        raise ValueError("entry bid must be positive")
    return (entry_bid / exit_ask - 1.0) * 10_000.0


@dataclass
class _OpenShadow:
    candidate: Candidate
    entry: Observation
    params: ShadowParams
    max_favorable_bps: float = 0.0
    max_adverse_bps: float = 0.0


class ShadowBook:
    """At most one virtual position per symbol. Risk overlay scales next notionals."""

    def __init__(self, params: ShadowParams) -> None:
        self._params = params
        self._open: dict[str, _OpenShadow] = {}
        self._closed: list[ShadowTrade] = []
        self._cum_pnl_bps: float = 0.0
        self._hwm_bps: float = 0.0
        self._risk_down = False

    @property
    def trades(self) -> list[ShadowTrade]:
        return list(self._closed)

    def position_open(self, symbol: str) -> bool:
        return symbol in self._open

    def notional_multiplier(self) -> float:
        return self.notional_multiplier_for(self._params)

    def notional_multiplier_for(self, params: ShadowParams) -> float:
        # Account-level risk-down flag; size uses the symbol's notional knobs.
        scale = params.risk_down_notional_multiplier if self._risk_down else 1.0
        return params.default_notional * params.virtual_leverage * scale

    def open_count(self) -> int:
        return len(self._open)

    def maybe_open(
        self,
        candidate: Candidate,
        observation: Observation,
        params: ShadowParams,
    ) -> None:
        if not candidate.accepted_for_shadow:
            return
        if observation.symbol in self._open:
            return
        self._open[observation.symbol] = _OpenShadow(
            candidate=candidate,
            entry=observation,
            params=params,
        )

    def on_observation(self, observation: Observation) -> ShadowTrade | None:
        open_pos = self._open.get(observation.symbol)
        if open_pos is None:
            return None
        pnl = executable_pnl_bps(
            open_pos.candidate.direction,
            open_pos.entry.bid,
            open_pos.entry.ask,
            observation.bid,
            observation.ask,
        )
        if pnl > open_pos.max_favorable_bps:
            open_pos.max_favorable_bps = pnl
        adverse = -pnl if pnl < 0 else 0.0
        if adverse > open_pos.max_adverse_bps:
            open_pos.max_adverse_bps = adverse
        reason = self._exit_reason(open_pos, observation, pnl)
        if reason is None:
            return None
        return self._close(open_pos, observation, pnl, reason)

    def _exit_reason(
        self,
        open_pos: _OpenShadow,
        observation: Observation,
        pnl: float,
    ) -> ExitReason | None:
        params = open_pos.params
        elapsed = (observation.observed_at - open_pos.entry.observed_at).total_seconds()
        hits: list[ExitReason] = []
        if pnl <= -params.hard_stop_bps:
            hits.append("HARD_STOP")
        if elapsed <= params.rapid_adverse_window_seconds and pnl <= -params.rapid_adverse_bps:
            hits.append("RAPID_ADVERSE")
        if (
            open_pos.max_favorable_bps >= params.trail_activation_bps
            and (open_pos.max_favorable_bps - pnl) >= params.trail_retrace_bps
        ):
            hits.append("TRAIL_EXIT")
        if pnl >= open_pos.candidate.target_bps:
            hits.append("GAP_HIT")
        if elapsed >= params.time_stop_seconds:
            hits.append("TIME_STOP")
        if not hits:
            return None
        for reason in EXIT_PRIORITY:
            if reason in hits:
                return reason
        return hits[0]

    def _close(
        self,
        open_pos: _OpenShadow,
        observation: Observation,
        pnl: float,
        reason: ExitReason,
    ) -> ShadowTrade:
        trade = ShadowTrade(
            symbol=observation.symbol,
            direction=open_pos.candidate.direction,
            entry_at=open_pos.entry.observed_at,
            exit_at=observation.observed_at,
            entry_bid=open_pos.entry.bid,
            entry_ask=open_pos.entry.ask,
            exit_bid=observation.bid,
            exit_ask=observation.ask,
            entry_mom_bps=open_pos.candidate.mom_bps,
            entry_gap_bps=open_pos.candidate.gap_bps,
            target_bps=open_pos.candidate.target_bps,
            exit_reason=reason,
            gross_bps=pnl,
            notional_multiplier=open_pos.candidate.notional_multiplier,
            virtual_leverage=open_pos.params.virtual_leverage,
            max_favorable_bps=open_pos.max_favorable_bps,
            max_adverse_bps=open_pos.max_adverse_bps,
        )
        del self._open[observation.symbol]
        self._closed.append(trade)
        self._cum_pnl_bps += pnl * open_pos.candidate.notional_multiplier
        if self._cum_pnl_bps > self._hwm_bps:
            self._hwm_bps = self._cum_pnl_bps
        # Drawdown is vs peak cumulative notional-weighted gross, not vs zero.
        drawdown = self._cum_pnl_bps - self._hwm_bps
        if drawdown <= self._params.risk_down_trigger_bps:
            self._risk_down = True
        elif self._risk_down and drawdown >= self._params.risk_restore_trigger_bps:
            self._risk_down = False
        return trade
