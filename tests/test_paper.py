from datetime import UTC, datetime, timedelta

import pytest

from trading_bot.paper import PaperConfig, PaperEngine


def event(topic: str, **data: object) -> dict[str, object]:
    return {"topic": topic, "data": data}


def test_long_round_trip_includes_spread_slippage_and_fees() -> None:
    engine = PaperEngine(
        PaperConfig(fast_window=2, slow_window=3, signal_threshold_bps=0.1)
    )
    now = datetime(2026, 7, 16, tzinfo=UTC)
    for index, price in enumerate((100.0, 101.0, 103.0)):
        engine.on_event(event("ask_bid_price", bidPrice=price - 0.1, askPrice=price + 0.1), now)
        engine.on_event(event("mark_price", markPrice=price), now + timedelta(seconds=index))
    assert engine.position > 0
    engine.on_event(event("ask_bid_price", bidPrice=104.9, askPrice=105.1), now)
    engine.on_event(event("mark_price", markPrice=105.0), now + timedelta(seconds=4))
    engine.close(now + timedelta(seconds=5))
    report = engine.report()
    assert report["fees_paid"] > 0
    assert report["net_pnl"] > 0
    assert report["open_position"] == 0


def test_negative_funding_credits_long_position() -> None:
    engine = PaperEngine(PaperConfig(fast_window=2, slow_window=3, signal_threshold_bps=0.1))
    now = datetime(2026, 7, 16, tzinfo=UTC)
    for index, price in enumerate((100.0, 101.0, 103.0)):
        engine.on_event(event("ask_bid_price", bidPrice=price - 0.1, askPrice=price + 0.1), now)
        engine.on_event(event("mark_price", markPrice=price), now + timedelta(seconds=index))
    cash = engine.cash
    engine.on_event(event("funding_rate_estimation", fundingRate=-0.01), now + timedelta(seconds=3))
    engine.on_event(event("mark_price", markPrice=103.0), now + timedelta(hours=8, seconds=3))
    assert engine.cash > cash
    assert engine.funding_paid < 0


def test_invalid_signal_windows_rejected() -> None:
    with pytest.raises(ValueError, match="fast < slow"):
        PaperConfig(fast_window=5, slow_window=5)
