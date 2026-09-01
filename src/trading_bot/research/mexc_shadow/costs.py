"""Fee overlays on already-closed shadow trades. Default is zero fee."""

from __future__ import annotations

from collections.abc import Sequence

from trading_bot.research.mexc_shadow.config import DEFAULT_COST_SCENARIOS, CostScenario
from trading_bot.research.mexc_shadow.types import ShadowTrade


def net_bps(trade: ShadowTrade, fee_bps_per_side: float) -> float:
    """Round-trip fee is 2 * fee_bps_per_side, independent of direction."""

    return trade.gross_bps - 2.0 * fee_bps_per_side


def summarize_costs(
    trades: Sequence[ShadowTrade],
    scenarios: Sequence[CostScenario] = DEFAULT_COST_SCENARIOS,
) -> dict[str, dict[str, float | int | None]]:
    out: dict[str, dict[str, float | int | None]] = {}
    for scenario in scenarios:
        nets = [net_bps(trade, scenario.fee_bps_per_side) for trade in trades]
        out[scenario.name] = {
            "fee_bps_per_side": scenario.fee_bps_per_side,
            "n_trades": len(nets),
            "sum_gross_bps": sum(trade.gross_bps for trade in trades),
            "sum_net_bps": sum(nets) if nets else 0.0,
            "mean_net_bps": (sum(nets) / len(nets)) if nets else None,
        }
    return out
