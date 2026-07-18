import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trading_bot.research.dataset import validate_manifest
from trading_bot.research.quality import require_acceptable_quality


@dataclass(frozen=True, slots=True)
class BaselineConfig:
    lookback_seconds: int = 30
    return_threshold_bps: float = 10.0
    holding_seconds: int = 30
    cooldown_seconds: int = 15
    notional: float = 1_000.0

    def __post_init__(self) -> None:
        if self.lookback_seconds < 1 or self.holding_seconds < 1:
            raise ValueError("Lookback and holding periods must be positive.")
        if self.cooldown_seconds < 0 or self.return_threshold_bps <= 0:
            raise ValueError("Cooldown must be non-negative and threshold positive.")
        if self.notional <= 0:
            raise ValueError("Notional must be positive.")


@dataclass(frozen=True, slots=True)
class CostConfig:
    maker_fee_rate: float = 0.0002
    taker_fee_rate: float = 0.00045
    funding_rate_per_8h: float = 0.0001
    slippage_bps: float = 2.0
    latency_penalty_bps: float = 1.0
    execution_delay_seconds: int = 1

    def __post_init__(self) -> None:
        values = (
            self.maker_fee_rate,
            self.taker_fee_rate,
            self.funding_rate_per_8h,
            self.slippage_bps,
            self.latency_penalty_bps,
            self.execution_delay_seconds,
        )
        if any(value < 0 for value in values):
            raise ValueError("Cost parameters must be non-negative.")


@dataclass(frozen=True, slots=True)
class ResearchIntent:
    timestamp: datetime
    direction: int
    reference_price: float
    return_bps: float


@dataclass(frozen=True, slots=True)
class SimulatedTrade:
    direction: int
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    gross_pnl: float
    fees: float
    funding: float
    slippage: float
    net_pnl: float


def configuration_hash(signal: BaselineConfig, costs: CostConfig) -> str:
    encoded = json.dumps(
        {"signal": asdict(signal), "costs": asdict(costs)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def maximum_drawdown(pnls: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def calculate_trade(
    *,
    direction: int,
    entry_time: datetime,
    exit_time: datetime,
    entry_price: float,
    exit_price: float,
    notional: float,
    costs: CostConfig,
) -> SimulatedTrade:
    quantity = notional / entry_price
    gross = direction * quantity * (exit_price - entry_price)
    fees = notional * costs.taker_fee_rate + quantity * exit_price * costs.taker_fee_rate
    slippage = (notional + quantity * exit_price) * costs.slippage_bps / 10_000
    holding_seconds = max(0.0, (exit_time - entry_time).total_seconds())
    funding = notional * abs(costs.funding_rate_per_8h) * holding_seconds / 28_800
    latency = (notional + quantity * exit_price) * costs.latency_penalty_bps / 10_000
    return SimulatedTrade(
        direction=direction,
        entry_time=entry_time,
        exit_time=exit_time,
        entry_price=entry_price,
        exit_price=exit_price,
        gross_pnl=gross,
        fees=fees,
        funding=funding,
        slippage=slippage + latency,
        net_pnl=gross - fees - funding - slippage - latency,
    )


def _load_candles(dataset_dir: Path) -> list[dict[str, Any]]:
    table = pq.read_table(dataset_dir / "candles_1s.parquet")
    required = {"timestamp", "open", "high", "low", "close", "volume", "trade_count"}
    if set(table.column_names) != required:
        raise ValueError("Candle dataset schema is incompatible.")
    rows = cast(
        list[dict[str, Any]],
        table.to_pylist(),
    )
    rows.sort(key=lambda row: row["timestamp"])
    if any(
        left["timestamp"] >= right["timestamp"]
        for left, right in zip(rows, rows[1:], strict=False)
    ):
        raise ValueError("Candles must have unique strictly increasing timestamps.")
    return rows


def replay_dataset(
    dataset_dir: Path,
    *,
    signal_config: BaselineConfig | None = None,
    cost_config: CostConfig | None = None,
    allow_warnings: bool = False,
) -> dict[str, Any]:
    quality = require_acceptable_quality(dataset_dir, allow_warnings=allow_warnings)
    manifest = validate_manifest(dataset_dir)
    signal = signal_config or BaselineConfig()
    costs = cost_config or CostConfig()
    candles = _load_candles(dataset_dir)
    skipped: Counter[str] = Counter()
    intents: list[ResearchIntent] = []
    trades: list[SimulatedTrade] = []
    position: tuple[int, int, float, datetime] | None = None
    cooldown_until: datetime | None = None

    for index, candle in enumerate(candles):
        timestamp = candle["timestamp"]
        price = float(candle["close"])
        if position is not None:
            direction, entry_index, entry_price, entry_time = position
            if (timestamp - entry_time).total_seconds() >= signal.holding_seconds:
                trades.append(
                    calculate_trade(
                        direction=direction,
                        entry_time=entry_time,
                        exit_time=timestamp,
                        entry_price=entry_price,
                        exit_price=price,
                        notional=signal.notional,
                        costs=costs,
                    )
                )
                position = None
                cooldown_until = timestamp
            else:
                skipped["position_open"] += 1
                continue
        target_index = index - signal.lookback_seconds
        if target_index < 0:
            skipped["insufficient_history"] += 1
            continue
        if cooldown_until is not None:
            elapsed = (timestamp - cooldown_until).total_seconds()
            if elapsed < signal.cooldown_seconds:
                skipped["cooldown"] += 1
                continue
        old_price = float(candles[target_index]["close"])
        return_bps = (price / old_price - 1) * 10_000
        if abs(return_bps) < signal.return_threshold_bps:
            skipped["below_threshold"] += 1
            continue
        direction = 1 if return_bps > 0 else -1
        execution_index = index + costs.execution_delay_seconds
        if execution_index >= len(candles):
            skipped["execution_delay_out_of_range"] += 1
            continue
        execution = candles[execution_index]
        intent = ResearchIntent(timestamp, direction, price, return_bps)
        intents.append(intent)
        position = (
            direction,
            execution_index,
            float(execution["close"]),
            execution["timestamp"],
        )

    if position is not None and candles:
        direction, _, entry_price, entry_time = position
        final = candles[-1]
        if final["timestamp"] > entry_time:
            trades.append(
                calculate_trade(
                    direction=direction,
                    entry_time=entry_time,
                    exit_time=final["timestamp"],
                    entry_price=entry_price,
                    exit_price=float(final["close"]),
                    notional=signal.notional,
                    costs=costs,
                )
            )
        else:
            skipped["unclosed_at_end"] += 1

    gross = sum(trade.gross_pnl for trade in trades)
    fees = sum(trade.fees for trade in trades)
    funding = sum(trade.funding for trade in trades)
    slippage = sum(trade.slippage for trade in trades)
    net = sum(trade.net_pnl for trade in trades)
    wins = sum(trade.net_pnl > 0 for trade in trades)
    average_holding = (
        sum((trade.exit_time - trade.entry_time).total_seconds() for trade in trades)
        / len(trades)
        if trades
        else 0.0
    )
    return {
        "result_type": "offline_research_simulation",
        "dataset_id": manifest["dataset_id"],
        "configuration_hash": configuration_hash(signal, costs),
        "configuration": {"signal": asdict(signal), "costs": asdict(costs)},
        "events": manifest["row_counts"]["events"],
        "candles": len(candles),
        "signals": len(intents),
        "simulated_entries": len(intents),
        "simulated_exits": len(trades),
        "gross_pnl": gross,
        "fees": fees,
        "funding": funding,
        "slippage_and_latency": slippage,
        "net_pnl": net,
        "win_rate": wins / len(trades) if trades else 0.0,
        "maximum_drawdown": maximum_drawdown([trade.net_pnl for trade in trades]),
        "average_holding_seconds": average_holding,
        "skipped_signal_reasons": dict(sorted(skipped.items())),
        "intents": [asdict(intent) for intent in intents],
        "trades": [asdict(trade) for trade in trades],
        "warning": "Benchmark simulation only; not a validated trading strategy.",
        "dataset_quality_status": quality["status"],
        "quality_warnings_allowed": allow_warnings,
    }


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, default=str, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def terminal_summary(report: dict[str, Any]) -> str:
    return (
        "OFFLINE RESEARCH SIMULATION — benchmark only\n"
        f"dataset={report['dataset_id']} config={report['configuration_hash']}\n"
        f"events={report['events']} candles={report['candles']} signals={report['signals']} "
        f"entries={report['simulated_entries']} exits={report['simulated_exits']}\n"
        f"gross_pnl={report['gross_pnl']:.6f} fees={report['fees']:.6f} "
        f"funding={report['funding']:.6f} "
        f"slippage_latency={report['slippage_and_latency']:.6f} "
        f"net_pnl={report['net_pnl']:.6f}\n"
        f"win_rate={report['win_rate']:.6f} "
        f"max_drawdown={report['maximum_drawdown']:.6f} "
        f"avg_holding_seconds={report['average_holding_seconds']:.3f}\n"
        f"skipped={json.dumps(report['skipped_signal_reasons'], sort_keys=True)}"
    )
