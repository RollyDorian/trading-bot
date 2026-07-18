import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading_bot.research.evaluator import evaluate_momentum
from trading_bot.research.exporter import write_versioned_export
from trading_bot.research.replay import replay_parquet
from trading_bot.research.signals.momentum import MomentumConfig, MomentumSignalCallback
from trading_bot.storage.models import MarketEvent

START = datetime(2026, 7, 18, tzinfo=UTC)


def _events(prices: list[float]) -> list[MarketEvent]:
    return [
        MarketEvent(
            id=index + 1,
            received_at=START + timedelta(seconds=index),
            exchange_at=START + timedelta(seconds=index),
            source="fixture",
            event_type="ask_bid_price",
            symbol="ETH/USDT-P",
            sequence=index + 1,
            latency_ms=0.0,
            payload={"bidPrice": price - 0.5, "askPrice": price + 0.5},
        )
        for index, price in enumerate(prices)
    ]


def _dataset(tmp_path: Path) -> Path:
    return write_versioned_export(
        events=_events([100, 100, 100, 103, 104, 104, 100, 97, 96]),
        output_root=tmp_path,
        version="v1_20260718",
        symbol="ETH/USDT-P",
        start=START,
        end=START + timedelta(minutes=1),
    )


def test_callback_replay_is_deterministic_and_chronological(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    path = dataset / "ETH-USDT-P.parquet"
    config = MomentumConfig(window=3, threshold_bps=20)
    first = replay_parquet(path, MomentumSignalCallback(config))
    second = replay_parquet(path, MomentumSignalCallback(config))
    assert first == second
    assert [signal.direction for signal in first] == [1, -1]
    assert [signal.timestamp for signal in first] == sorted(
        signal.timestamp for signal in first
    )


def test_evaluator_writes_research_results_without_costs(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    report = evaluate_momentum(dataset, MomentumConfig(window=3, threshold_bps=20))
    saved = json.loads((dataset / "eval_momentum.json").read_text(encoding="utf-8"))
    assert saved == report
    assert report["result_type"] == "offline_research_evaluation_without_costs"
    assert report["signal_count"] == 2
    assert all("hypothetical_pnl" in trade for trade in report["trades"])
    assert "fees" in report["warning"]
