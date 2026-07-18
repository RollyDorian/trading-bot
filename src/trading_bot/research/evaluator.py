import json
from dataclasses import asdict
from math import sqrt
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any

from trading_bot.research.replay import replay_parquet
from trading_bot.research.signals.momentum import (
    MomentumConfig,
    MomentumSignalCallback,
)


def evaluate_momentum(
    dataset_dir: Path,
    config: MomentumConfig | None = None,
    *,
    hypothetical_notional: float = 1_000.0,
) -> dict[str, Any]:
    if hypothetical_notional <= 0:
        raise ValueError("Hypothetical notional must be positive.")
    manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    parquet_path = dataset_dir / str(manifest["parquet_file"])
    callback = MomentumSignalCallback(config)
    signals = replay_parquet(parquet_path, callback)
    returns: list[float] = []
    trades: list[dict[str, Any]] = []
    for current, following in zip(signals, signals[1:], strict=False):
        result = current.direction * (following.mid_price / current.mid_price - 1)
        returns.append(result)
        trades.append(
            {
                "entry_timestamp": current.timestamp.isoformat(),
                "exit_timestamp": following.timestamp.isoformat(),
                "direction": current.direction,
                "entry_price": current.mid_price,
                "exit_price": following.mid_price,
                "hypothetical_return": result,
                "hypothetical_pnl": result * hypothetical_notional,
            }
        )
    if signals and callback.last_price is not None and callback.last_timestamp is not None:
        final = signals[-1]
        if callback.last_timestamp > final.timestamp:
            result = final.direction * (callback.last_price / final.mid_price - 1)
            returns.append(result)
            trades.append(
                {
                    "entry_timestamp": final.timestamp.isoformat(),
                    "exit_timestamp": callback.last_timestamp.isoformat(),
                    "direction": final.direction,
                    "entry_price": final.mid_price,
                    "exit_price": callback.last_price,
                    "hypothetical_return": result,
                    "hypothetical_pnl": result * hypothetical_notional,
                }
            )
    deviation = pstdev(returns) if len(returns) > 1 else 0.0
    mean_return = fmean(returns) if returns else 0.0
    report = {
        "result_type": "offline_research_evaluation_without_costs",
        "version": manifest["version"],
        "signal": "mid_price_momentum",
        "configuration": asdict(callback.config),
        "signal_count": len(signals),
        "trade_count": len(trades),
        "hypothetical_notional": hypothetical_notional,
        "total_hypothetical_pnl": sum(returns) * hypothetical_notional,
        "win_rate": sum(value > 0 for value in returns) / len(returns) if returns else 0.0,
        "mean_return": mean_return,
        "sharpe_approximation": mean_return / deviation * sqrt(len(returns)) if deviation else 0.0,
        "signals": [
            {
                **asdict(signal),
                "timestamp": signal.timestamp.isoformat(),
            }
            for signal in signals
        ],
        "trades": trades,
        "warning": "Research benchmark only; fees, funding, slippage, and latency omitted.",
    }
    (dataset_dir / "eval_momentum.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return report
