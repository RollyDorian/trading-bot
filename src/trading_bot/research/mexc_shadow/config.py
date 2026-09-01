"""Pluggable engine configuration. Hypotheses, not claimed ground truth."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from trading_bot.research.mexc_shadow.safety import assert_no_credential_keys
from trading_bot.research.mexc_shadow.types import (
    GAP_DEFINITIONS,
    MOMENTUM_DEFINITIONS,
)


@dataclass(frozen=True, slots=True)
class SignalParams:
    momentum_definition: str = "mid_return_lookback"
    gap_definition: str = "mid_vs_mark"
    momentum_lookback: int = 5
    momentum_lookback_seconds: float | None = None
    mom_abs_min_bps: float = 3.0
    gap_abs_min_bps: float = 1.5
    target_multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.momentum_definition not in MOMENTUM_DEFINITIONS:
            raise ValueError(f"unknown momentum_definition {self.momentum_definition!r}")
        if self.gap_definition not in GAP_DEFINITIONS:
            raise ValueError(f"unknown gap_definition {self.gap_definition!r}")
        if self.momentum_lookback < 1:
            raise ValueError("momentum_lookback must be >= 1")
        if self.mom_abs_min_bps < 0 or self.gap_abs_min_bps < 0:
            raise ValueError("abs minima must be >= 0")
        if self.target_multiplier <= 0:
            raise ValueError("target_multiplier must be > 0")


@dataclass(frozen=True, slots=True)
class ShadowParams:
    rapid_adverse_bps: float = 4.3
    rapid_adverse_window_seconds: float = 2.0
    hard_stop_bps: float = 12.0
    time_stop_seconds: float = 60.0
    trail_activation_bps: float = 7.0
    trail_retrace_bps: float = 6.5
    risk_down_trigger_bps: float = -80.0
    risk_restore_trigger_bps: float = -20.0
    risk_down_notional_multiplier: float = 0.7
    virtual_leverage: float = 1.0
    default_notional: float = 1.0

    def __post_init__(self) -> None:
        if self.rapid_adverse_bps <= 0 or self.hard_stop_bps <= 0:
            raise ValueError("adverse/hard-stop magnitudes must be > 0")
        if self.rapid_adverse_window_seconds <= 0 or self.time_stop_seconds <= 0:
            raise ValueError("time windows must be > 0")
        if self.trail_activation_bps <= 0 or self.trail_retrace_bps <= 0:
            raise ValueError("trail thresholds must be > 0")
        if not 0 < self.risk_down_notional_multiplier <= 1:
            raise ValueError("risk_down_notional_multiplier must be in (0, 1]")
        if self.virtual_leverage <= 0 or self.default_notional <= 0:
            raise ValueError("leverage and default_notional must be > 0")
        if self.risk_down_trigger_bps >= 0 or self.risk_restore_trigger_bps > 0:
            raise ValueError("risk triggers are drawdown bps (down <= restore <= 0)")
        if self.risk_down_trigger_bps > self.risk_restore_trigger_bps:
            raise ValueError("risk_down_trigger_bps must be <= risk_restore_trigger_bps")


@dataclass(frozen=True, slots=True)
class ThrottleParams:
    """Research/risk caps on shadow *acceptance*. All raw candidates are still stored."""

    max_shadow_per_hour: int | None = None
    max_shadow_per_day: int | None = None
    max_positions_per_symbol: int = 1

    def __post_init__(self) -> None:
        if self.max_positions_per_symbol != 1:
            raise ValueError("v1 allows only one virtual position per symbol")
        for label, value in (
            ("max_shadow_per_hour", self.max_shadow_per_hour),
            ("max_shadow_per_day", self.max_shadow_per_day),
        ):
            if value is not None and value < 1:
                raise ValueError(f"{label} must be None or >= 1")


@dataclass(frozen=True, slots=True)
class CostScenario:
    name: str
    fee_bps_per_side: float
    note: str = ""


ZERO_FEE = CostScenario("zero_fee", 0.0, "Zero-fee hypothesis; default.")
MAKER_6_BPS = CostScenario("maker_6bps_per_side", 6.0, "Configurable maker stress.")
TAKER_8_BPS = CostScenario("taker_8bps_per_side", 8.0, "Configurable taker stress.")
DEFAULT_COST_SCENARIOS = (ZERO_FEE, MAKER_6_BPS, TAKER_8_BPS)


@dataclass(frozen=True, slots=True)
class EngineConfig:
    profile_id: str
    signal: SignalParams = field(default_factory=SignalParams)
    shadow: ShadowParams = field(default_factory=ShadowParams)
    throttle: ThrottleParams = field(default_factory=ThrottleParams)
    symbol_overrides: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    provenance_note: str = ""

    def for_symbol(self, symbol: str) -> EngineConfig:
        raw = self.symbol_overrides.get(symbol)
        if not raw:
            return self
        assert_no_credential_keys(dict(raw))
        signal = _overlay_dataclass(self.signal, raw.get("signal") or {})
        shadow = _overlay_dataclass(self.shadow, raw.get("shadow") or {})
        throttle = _overlay_dataclass(self.throttle, raw.get("throttle") or {})
        return replace(self, signal=signal, shadow=shadow, throttle=throttle)


def _overlay_dataclass(base: Any, overlay: Mapping[str, Any]) -> Any:
    if not overlay:
        return base
    allowed = {item.name for item in fields(base)}
    unknown = set(overlay) - allowed
    if unknown:
        raise ValueError(f"unknown overlay fields: {sorted(unknown)}")
    return replace(base, **dict(overlay))


_ENGINE_MAPPING_KEYS = frozenset(
    {
        "profile_id",
        "signal",
        "shadow",
        "throttle",
        "symbol_overrides",
        "provenance_note",
    }
)


def engine_config_from_mapping(payload: Mapping[str, Any]) -> EngineConfig:
    data = dict(payload)
    assert_no_credential_keys(data)
    unknown = set(data) - _ENGINE_MAPPING_KEYS
    if unknown:
        raise ValueError(f"unknown engine config fields: {sorted(unknown)}")
    if "profile_id" not in data:
        raise ValueError("engine config requires profile_id")
    signal = SignalParams(**dict(data.get("signal") or {}))
    shadow = ShadowParams(**dict(data.get("shadow") or {}))
    throttle = ThrottleParams(**dict(data.get("throttle") or {}))
    overrides = dict(data.get("symbol_overrides") or {})
    for symbol, sub in overrides.items():
        if not isinstance(sub, dict):
            raise ValueError(f"symbol override for {symbol!r} must be an object")
        assert_no_credential_keys(sub)
    return EngineConfig(
        profile_id=str(data["profile_id"]),
        signal=signal,
        shadow=shadow,
        throttle=throttle,
        symbol_overrides=overrides,
        provenance_note=str(data.get("provenance_note") or ""),
    )


def load_engine_config_json(path: Path) -> EngineConfig:
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("engine config JSON must be an object")
    return engine_config_from_mapping(payload)
