"""Public types for the isolated MEXC signal/shadow research engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Direction = Literal["long", "short"]
ExitReason = Literal[
    "GAP_HIT",
    "TIME_STOP",
    "TRAIL_EXIT",
    "RAPID_ADVERSE",
    "HARD_STOP",
]
ThrottleReason = Literal[
    "accepted",
    "max_per_hour",
    "max_per_day",
    "position_open",
    "missing_features",
    "filters_not_met",
]

# Frozen protective-first order when several exits fire on the same print.
EXIT_PRIORITY: tuple[ExitReason, ...] = (
    "HARD_STOP",
    "RAPID_ADVERSE",
    "TRAIL_EXIT",
    "GAP_HIT",
    "TIME_STOP",
)

MOM_MID_RETURN_LOOKBACK = "mid_return_lookback"
MOM_MID_VS_SMA = "mid_vs_sma"
GAP_MID_VS_MARK = "mid_vs_mark"
GAP_MID_VS_INDEX = "mid_vs_index"
GAP_LAST_VS_MARK = "last_vs_mark"

MOMENTUM_DEFINITIONS = (MOM_MID_RETURN_LOOKBACK, MOM_MID_VS_SMA)
GAP_DEFINITIONS = (GAP_MID_VS_MARK, GAP_MID_VS_INDEX, GAP_LAST_VS_MARK)


@dataclass(frozen=True, slots=True)
class Observation:
    """One read-only market print. Never an order or account snapshot."""

    observed_at: datetime
    received_at: datetime
    symbol: str
    bid: float
    ask: float
    source: str = "mexc_ui_observer"
    mid: float | None = None
    last: float | None = None
    mark: float | None = None
    index: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    # Optional L2 snapshot from the observer; unused by v1 features.
    orderbook_bids: tuple[tuple[float, float], ...] | None = None
    orderbook_asks: tuple[tuple[float, float], ...] | None = None

    def executable_mid(self) -> float | None:
        if self.mid is not None and self.mid > 0:
            return self.mid
        if self.bid > 0 and self.ask > self.bid:
            return (self.bid + self.ask) / 2.0
        return None


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    observed_at: datetime
    symbol: str
    mom_bps: float | None
    gap_bps: float | None
    mid: float | None
    momentum_definition: str
    gap_definition: str


@dataclass(frozen=True, slots=True)
class Candidate:
    """Every raw signal candidate is stored, including throttle rejects."""

    observed_at: datetime
    symbol: str
    direction: Direction
    mom_bps: float
    gap_bps: float
    target_bps: float
    throttle: ThrottleReason
    accepted_for_shadow: bool
    notional_multiplier: float


@dataclass(frozen=True, slots=True)
class ShadowTrade:
    symbol: str
    direction: Direction
    entry_at: datetime
    exit_at: datetime
    entry_bid: float
    entry_ask: float
    exit_bid: float
    exit_ask: float
    entry_mom_bps: float
    entry_gap_bps: float
    target_bps: float
    exit_reason: ExitReason
    gross_bps: float
    notional_multiplier: float
    virtual_leverage: float
    max_favorable_bps: float
    max_adverse_bps: float


@dataclass
class ReplayReport:
    profile_id: str
    observations: int
    candidates: list[Candidate] = field(default_factory=list)
    trades: list[ShadowTrade] = field(default_factory=list)
    cost_summaries: dict[str, dict[str, float | int | None]] = field(default_factory=dict)
    n_open: int = 0
    notes: tuple[str, ...] = ()
