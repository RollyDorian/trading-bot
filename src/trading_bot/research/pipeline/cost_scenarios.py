"""Explicit cost sensitivities for offline baseline validation."""

from __future__ import annotations

from dataclasses import replace

from trading_bot.research.replay import CostConfig

EXECUTION_DELAYS_SECONDS = (0, 1, 2)


def optimistic(*, execution_delay_seconds: int = 0) -> CostConfig:
    """Low-cost sensitivity case; not an expected or guaranteed execution outcome."""

    return CostConfig(
        maker_fee_rate=0.0001,
        taker_fee_rate=0.00035,
        funding_rate_per_8h=0.00005,
        slippage_bps=1.0,
        latency_penalty_bps=0.5,
        execution_delay_seconds=execution_delay_seconds,
    )


def base(*, execution_delay_seconds: int = 1) -> CostConfig:
    """Current replay defaults, made explicit for corpus comparisons."""

    return CostConfig(execution_delay_seconds=execution_delay_seconds)


def conservative(*, execution_delay_seconds: int = 2) -> CostConfig:
    """Higher-cost stress case for sensitivity analysis."""

    return CostConfig(
        maker_fee_rate=0.0003,
        taker_fee_rate=0.0006,
        funding_rate_per_8h=0.0002,
        slippage_bps=4.0,
        latency_penalty_bps=2.0,
        execution_delay_seconds=execution_delay_seconds,
    )


def with_execution_delay(config: CostConfig, seconds: int) -> CostConfig:
    """Copy a cost case with an explicit 0/1/2-second execution delay."""

    if seconds not in EXECUTION_DELAYS_SECONDS:
        raise ValueError("execution delay sensitivity supports only 0, 1, or 2 seconds")
    return replace(config, execution_delay_seconds=seconds)


def execution_delay_scenarios(config: CostConfig) -> dict[int, CostConfig]:
    """Expand one cost case across all approved delay sensitivities."""

    return {
        seconds: with_execution_delay(config, seconds)
        for seconds in EXECUTION_DELAYS_SECONDS
    }


def cost_scenarios() -> dict[str, CostConfig]:
    """Return the named cost cases with their default execution delays."""

    return dict(COST_SCENARIOS)


OPTIMISTIC = optimistic()
BASE = base()
CONSERVATIVE = conservative()
COST_SCENARIOS = {
    "optimistic": OPTIMISTIC,
    "base": BASE,
    "conservative": CONSERVATIVE,
}
