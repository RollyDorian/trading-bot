from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_bot.research.dataset import write_dataset
from trading_bot.research.quality import validate_dataset
from trading_bot.research.replay import (
    BaselineConfig,
    CostConfig,
    calculate_trade,
    maximum_drawdown,
    replay_dataset,
)
from trading_bot.storage.models import MarketEvent

START = datetime(2026, 7, 18, tzinfo=UTC)


def _events(count: int = 20) -> list[MarketEvent]:
    return [
        MarketEvent(
            id=index + 1,
            received_at=START + timedelta(seconds=index),
            exchange_at=START + timedelta(seconds=index),
            source="fixture",
            event_type="trades",
            symbol="ETH/USDT-P",
            sequence=index + 1,
            latency_ms=0.0,
            payload={"topic": "trades", "price": 100 + index, "quantity": 1},
        )
        for index in range(count)
    ]


def test_costs_are_deducted_before_net_pnl() -> None:
    costs = CostConfig(
        maker_fee_rate=0,
        taker_fee_rate=0.001,
        funding_rate_per_8h=0.001,
        slippage_bps=1,
        latency_penalty_bps=1,
        execution_delay_seconds=0,
    )
    trade = calculate_trade(
        direction=1,
        entry_time=START,
        exit_time=START + timedelta(hours=8),
        entry_price=100,
        exit_price=110,
        notional=1_000,
        costs=costs,
    )
    assert trade.gross_pnl == 100
    assert trade.net_pnl == pytest.approx(
        trade.gross_pnl - trade.fees - trade.funding - trade.slippage
    )
    assert trade.fees > 0
    assert trade.funding > 0
    assert trade.slippage > 0


def test_maximum_drawdown_uses_cumulative_net_results() -> None:
    assert maximum_drawdown([10, -4, -9, 2]) == 13
    assert maximum_drawdown([]) == 0


def test_offline_replay_is_deterministic(tmp_path: Path) -> None:
    dataset_dir = write_dataset(
        events=_events(),
        symbol="ETH/USDT-P",
        start=START,
        end=START + timedelta(minutes=1),
        output_root=tmp_path,
    )
    signal = BaselineConfig(
        lookback_seconds=2,
        return_threshold_bps=1,
        holding_seconds=3,
        cooldown_seconds=1,
    )
    costs = CostConfig(execution_delay_seconds=0)
    validate_dataset(dataset_dir)
    first = replay_dataset(dataset_dir, signal_config=signal, cost_config=costs)
    second = replay_dataset(dataset_dir, signal_config=signal, cost_config=costs)
    assert first == second
    assert first["result_type"] == "offline_research_simulation"
    assert first["signals"] > 0
    assert first["net_pnl"] < first["gross_pnl"]
