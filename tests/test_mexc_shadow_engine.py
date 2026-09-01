"""Deterministic replay and mechanism tests for the MEXC shadow engine."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_bot.research.mexc_shadow.config import (
    EngineConfig,
    ShadowParams,
    SignalParams,
    ThrottleParams,
    engine_config_from_mapping,
    load_engine_config_json,
)
from trading_bot.research.mexc_shadow.costs import net_bps
from trading_bot.research.mexc_shadow.engine import run_shadow_replay
from trading_bot.research.mexc_shadow.features import FeatureEngine
from trading_bot.research.mexc_shadow.profiles import (
    author_observed_v0,
    conservative_v0,
    load_profile,
)
from trading_bot.research.mexc_shadow.shadow import ShadowBook, executable_pnl_bps
from trading_bot.research.mexc_shadow.signal import classify_direction
from trading_bot.research.mexc_shadow.source import (
    MemorySource,
    MexcUiObserver,
    ReplayFixtureSource,
)
from trading_bot.research.mexc_shadow.types import Candidate, Observation

REPO = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO / "configs" / "mexc_shadow"
FIXTURE = REPO / "tests" / "fixtures" / "mexc_shadow" / "ui_observer_sample.json"
BASE = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
HALF = 0.01


def _mark_for_gap(mid: float, gap_bps: float) -> float:
    return mid / (1.0 + gap_bps / 10_000.0)


def _obs(
    seconds: float,
    mid: float,
    *,
    gap_bps: float = -3.0,
    symbol: str = "TAOUSDT",
    last: float | None = None,
    index: float | None = None,
) -> Observation:
    ts = BASE + timedelta(seconds=seconds)
    return Observation(
        observed_at=ts,
        received_at=ts,
        symbol=symbol,
        bid=mid - HALF,
        ask=mid + HALF,
        mark=_mark_for_gap(mid, gap_bps),
        last=last,
        index=index,
        source="replay_fixture",
    )


def _warmup_long(
    *,
    signal_second: float = 5.0,
    signal_mid: float = 100.01,
    prior_mid: float = 99.96,
    gap_bps: float = -3.0,
    symbol: str = "TAOUSDT",
) -> list[Observation]:
    # Five equal priors so lookback=5 yields mom vs prior_mid.
    rows = [_obs(float(i), prior_mid, gap_bps=gap_bps, symbol=symbol) for i in range(5)]
    rows.append(_obs(signal_second, signal_mid, gap_bps=gap_bps, symbol=symbol))
    return rows


def _candidate(
    *,
    direction: str = "long",
    gap_bps: float = -3.0,
    target_bps: float = 6.0,
    notional: float = 1.0,
    when: datetime = BASE,
    symbol: str = "TAOUSDT",
) -> Candidate:
    return Candidate(
        observed_at=when,
        symbol=symbol,
        direction=direction,  # type: ignore[arg-type]
        mom_bps=4.0,
        gap_bps=gap_bps,
        target_bps=target_bps,
        throttle="accepted",
        accepted_for_shadow=True,
        notional_multiplier=notional,
    )


def _book_quiet_exits(**overrides: float) -> ShadowParams:
    # Large protective thresholds so TIME_STOP / GAP_HIT can be isolated.
    base = ShadowParams(
        rapid_adverse_bps=50.0,
        hard_stop_bps=50.0,
        trail_activation_bps=50.0,
        trail_retrace_bps=50.0,
        time_stop_seconds=60.0,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_classify_direction_author_pattern() -> None:
    assert classify_direction(4.0, -2.0) == "long"
    assert classify_direction(-4.0, 2.0) == "short"
    assert classify_direction(4.0, 2.0) is None
    assert classify_direction(-4.0, -2.0) is None
    assert classify_direction(0.0, -2.0) is None


def test_profiles_match_json_and_conservative_caps() -> None:
    author = author_observed_v0()
    conservative = conservative_v0()
    from_json = load_engine_config_json(CONFIG_DIR / "author_observed_v0.json")
    cons_json = load_engine_config_json(CONFIG_DIR / "conservative_v0.json")
    assert load_profile("author_observed_v0").signal == author.signal
    assert from_json.signal == author.signal
    assert from_json.shadow == author.shadow
    assert from_json.throttle == author.throttle
    assert cons_json.throttle.max_shadow_per_hour == 10
    assert cons_json.throttle.max_shadow_per_day == 250
    assert conservative.signal == author.signal
    assert conservative.shadow == author.shadow
    assert conservative.throttle.max_positions_per_symbol == 1


def test_config_rejects_credentials_and_unknown_fields() -> None:
    with pytest.raises(ValueError, match="credential"):
        engine_config_from_mapping({"profile_id": "x", "api_key": "nope"})
    with pytest.raises(ValueError, match="unknown engine config"):
        engine_config_from_mapping({"profile_id": "x", "place_mode": "live"})
    with pytest.raises(ValueError, match="unknown overlay"):
        EngineConfig(
            profile_id="x",
            symbol_overrides={"TAOUSDT": {"signal": {"not_a_field": 1}}},
        ).for_symbol("TAOUSDT")


def test_mexc_ui_observer_is_read_only_and_stores_depth() -> None:
    rows = list(ReplayFixtureSource(FIXTURE).iter_observations())
    assert len(rows) == 1
    obs = rows[0]
    assert obs.source == "mexc_ui_observer"
    assert obs.symbol == "TAOUSDT"
    assert obs.orderbook_bids == ((99.99, 12.0), (99.98, 20.0))
    assert obs.received_at > obs.observed_at
    with pytest.raises(ValueError, match="credential"):
        list(
            MexcUiObserver(
                [{"symbol": "TAOUSDT", "bid": 1, "ask": 2, "api_secret": "x"}]
            ).iter_observations()
        )


def test_replay_long_and_short_gap_hit() -> None:
    long_rows = _warmup_long()
    # target = 2 * 3 = 6 bps; long enter ask 100.02 → bid 100.09 is ~7 bps.
    long_rows.append(_obs(6.0, 100.10, gap_bps=-3.0))
    long_report = run_shadow_replay(MemorySource(long_rows), author_observed_v0())
    assert len(long_report.trades) == 1
    trade = long_report.trades[0]
    assert trade.direction == "long"
    assert trade.exit_reason == "GAP_HIT"
    assert abs(trade.target_bps - 2.0 * abs(trade.entry_gap_bps)) < 1e-9
    assert trade.gross_bps >= trade.target_bps

    short_rows = [_obs(float(i), 100.06, gap_bps=3.0) for i in range(5)]
    short_rows.append(_obs(5.0, 100.01, gap_bps=3.0))
    # Short enter bid ~100.00; exit ask 99.93 → ~7 bps.
    short_rows.append(_obs(6.0, 99.92, gap_bps=3.0))
    short_report = run_shadow_replay(MemorySource(short_rows), author_observed_v0())
    assert short_report.trades[0].direction == "short"
    assert short_report.trades[0].exit_reason == "GAP_HIT"


def test_same_print_does_not_exit_and_filters_are_stored() -> None:
    opened = run_shadow_replay(MemorySource(_warmup_long()), author_observed_v0())
    assert opened.n_open == 1
    assert opened.trades == []
    assert opened.candidates[0].accepted_for_shadow is True

    weak = [_obs(float(i), 100.0, gap_bps=-3.0) for i in range(5)]
    weak.append(_obs(5.0, 100.005, gap_bps=-3.0))  # mom ~0.5 bps < 3
    stored = run_shadow_replay(MemorySource(weak), author_observed_v0())
    assert stored.trades == []
    assert stored.candidates
    assert all(row.throttle == "filters_not_met" for row in stored.candidates)
    assert all(row.accepted_for_shadow is False for row in stored.candidates)


def test_position_open_stores_candidate_without_second_shadow() -> None:
    rows = _warmup_long()
    rows.append(_obs(6.0, 100.012, gap_bps=-3.0))
    report = run_shadow_replay(MemorySource(rows), author_observed_v0())
    assert report.n_open == 1
    assert sum(1 for row in report.candidates if row.accepted_for_shadow) == 1
    assert any(row.throttle == "position_open" for row in report.candidates)


def test_one_virtual_position_per_symbol_two_symbols_ok() -> None:
    tao = _warmup_long(symbol="TAOUSDT")
    btc = _warmup_long(symbol="BTCUSDT", signal_mid=100.01, prior_mid=99.96)
    report = run_shadow_replay(MemorySource(tao + btc), author_observed_v0())
    assert report.n_open == 2
    assert {row.symbol for row in report.candidates if row.accepted_for_shadow} == {
        "TAOUSDT",
        "BTCUSDT",
    }


def test_symbol_override_tightens_only_one_symbol() -> None:
    cfg = replace(
        author_observed_v0(),
        symbol_overrides={"BTCUSDT": {"signal": {"mom_abs_min_bps": 20.0}}},
    )
    tao = _warmup_long(symbol="TAOUSDT")
    btc = _warmup_long(symbol="BTCUSDT")
    report = run_shadow_replay(MemorySource(tao + btc), cfg)
    by_symbol = {row.symbol: row for row in report.candidates}
    assert by_symbol["TAOUSDT"].accepted_for_shadow is True
    assert by_symbol["BTCUSDT"].throttle == "filters_not_met"


def test_shadow_exit_reasons() -> None:
    entry = _obs(0.0, 100.01, gap_bps=-3.0)
    book = ShadowBook(author_observed_v0().shadow)
    book.maybe_open(_candidate(when=entry.observed_at), entry, author_observed_v0().shadow)

    # Rapid adverse ~−5 bps inside 2s, not hard-stop.
    rapid = book.on_observation(_obs(1.0, 99.98, gap_bps=-3.0))
    assert rapid is not None
    assert rapid.exit_reason == "RAPID_ADVERSE"
    assert rapid.gross_bps <= -4.3

    book = ShadowBook(author_observed_v0().shadow)
    book.maybe_open(_candidate(when=entry.observed_at), entry, author_observed_v0().shadow)
    hard = book.on_observation(_obs(3.0, 99.89, gap_bps=-3.0))
    assert hard is not None
    assert hard.exit_reason == "HARD_STOP"

    book = ShadowBook(author_observed_v0().shadow)
    book.maybe_open(_candidate(when=entry.observed_at), entry, author_observed_v0().shadow)
    time_stop = book.on_observation(_obs(60.0, 100.01, gap_bps=-3.0))
    assert time_stop is not None
    assert time_stop.exit_reason == "TIME_STOP"

    trail_params = author_observed_v0().shadow
    book = ShadowBook(trail_params)
    trail_entry = _obs(0.0, 100.01, gap_bps=-5.0)
    book.maybe_open(
        _candidate(gap_bps=-5.0, target_bps=10.0, when=trail_entry.observed_at),
        trail_entry,
        trail_params,
    )
    assert book.on_observation(_obs(1.0, 100.11, gap_bps=-5.0)) is None
    trail = book.on_observation(_obs(2.0, 100.04, gap_bps=-5.0))
    assert trail is not None
    assert trail.exit_reason == "TRAIL_EXIT"

    # Same-print HARD_STOP beats RAPID_ADVERSE when both fire.
    book = ShadowBook(author_observed_v0().shadow)
    book.maybe_open(_candidate(when=entry.observed_at), entry, author_observed_v0().shadow)
    both = book.on_observation(_obs(1.0, 99.89, gap_bps=-3.0))
    assert both is not None
    assert both.exit_reason == "HARD_STOP"


def test_risk_overlay_scales_then_restores_notional() -> None:
    params = _book_quiet_exits(
        time_stop_seconds=1.0,
        risk_down_trigger_bps=-5.0,
        risk_restore_trigger_bps=-1.0,
        risk_down_notional_multiplier=0.7,
    )
    book = ShadowBook(params)
    entry = _obs(0.0, 100.01)
    book.maybe_open(_candidate(when=entry.observed_at), entry, params)
    first = book.on_observation(_obs(1.0, 99.96))
    assert first is not None
    assert first.gross_bps <= -5.0
    assert abs(book.notional_multiplier() - 0.7) < 1e-12

    entry2 = _obs(2.0, 100.01)
    book.maybe_open(
        _candidate(when=entry2.observed_at, notional=book.notional_multiplier()),
        entry2,
        params,
    )
    second = book.on_observation(_obs(3.0, 100.12))
    assert second is not None
    assert abs(second.notional_multiplier - 0.7) < 1e-12
    assert abs(book.notional_multiplier() - 1.0) < 1e-12


def test_cost_scenarios_change_net_only() -> None:
    rows = _warmup_long()
    rows.append(_obs(6.0, 100.10, gap_bps=-3.0))
    report = run_shadow_replay(MemorySource(rows), author_observed_v0())
    gross = report.trades[0].gross_bps
    zero = report.cost_summaries["zero_fee"]
    maker = report.cost_summaries["maker_6bps_per_side"]
    taker = report.cost_summaries["taker_8bps_per_side"]
    assert zero["sum_gross_bps"] == maker["sum_gross_bps"] == taker["sum_gross_bps"]
    assert zero["sum_net_bps"] == pytest.approx(gross)
    assert maker["sum_net_bps"] == pytest.approx(gross - 12.0)
    assert taker["sum_net_bps"] == pytest.approx(gross - 16.0)
    assert net_bps(report.trades[0], 0.0) == pytest.approx(gross)


def test_conservative_hourly_cap_stores_rejected_candidates() -> None:
    cfg = replace(
        conservative_v0(),
        signal=replace(conservative_v0().signal, momentum_lookback=1),
        shadow=replace(conservative_v0().shadow, time_stop_seconds=0.5),
    )
    rows = []
    for i in range(12):
        mid = 100.0 + i * 0.05
        rows.append(_obs(float(i), mid, gap_bps=-3.0))
    report = run_shadow_replay(MemorySource(rows), cfg)
    accepted = [row for row in report.candidates if row.accepted_for_shadow]
    hourly = [row for row in report.candidates if row.throttle == "max_per_hour"]
    assert len(accepted) == 10
    assert hourly
    assert len(report.candidates) == 11


def test_daily_cap_is_independent_of_hourly() -> None:
    cfg = EngineConfig(
        profile_id="day_cap",
        signal=SignalParams(momentum_lookback=1, mom_abs_min_bps=3.0, gap_abs_min_bps=1.5),
        shadow=replace(author_observed_v0().shadow, time_stop_seconds=0.5),
        throttle=ThrottleParams(max_shadow_per_day=2),
    )
    rows = [_obs(float(i), 100.0 + i * 0.05, gap_bps=-3.0) for i in range(4)]
    report = run_shadow_replay(MemorySource(rows), cfg)
    assert sum(1 for row in report.candidates if row.accepted_for_shadow) == 2
    assert any(row.throttle == "max_per_day" for row in report.candidates)


def test_feature_plugins_and_seconds_lookback() -> None:
    sma = FeatureEngine(
        SignalParams(
            momentum_definition="mid_vs_sma",
            gap_definition="mid_vs_index",
            momentum_lookback=3,
        )
    )
    rows = [
        _obs(0.0, 100.0, gap_bps=-3.0, index=100.03),
        _obs(1.0, 100.02, gap_bps=-3.0, index=100.05),
        _obs(2.0, 100.04, gap_bps=-3.0, index=100.07),
    ]
    snaps = [sma.update(row) for row in rows]
    assert snaps[-1].mom_bps is not None
    assert snaps[-1].gap_bps is not None

    last_gap = FeatureEngine(SignalParams(gap_definition="last_vs_mark", momentum_lookback=1))
    snap = last_gap.update(_obs(0.0, 100.0, gap_bps=-3.0, last=99.97))
    # last vs mark: last 99.97, mark from gap=-3 on mid 100.
    assert snap.gap_bps is not None

    timed = FeatureEngine(
        SignalParams(momentum_lookback_seconds=5.0, momentum_lookback=1)
    )
    timed.update(_obs(0.0, 99.96, gap_bps=-3.0))
    later = timed.update(_obs(5.0, 100.01, gap_bps=-3.0))
    assert later.mom_bps is not None
    assert later.mom_bps == pytest.approx((100.01 / 99.96 - 1.0) * 10_000.0)


def test_executable_pnl_includes_spread() -> None:
    long_bps = executable_pnl_bps("long", 100.0, 100.02, 100.0, 100.02)
    short_bps = executable_pnl_bps("short", 100.0, 100.02, 100.0, 100.02)
    assert long_bps < 0
    assert short_bps < 0


def test_unknown_profile_and_crossed_book_fail_closed() -> None:
    with pytest.raises(ValueError, match="unknown mexc_shadow profile"):
        load_profile("live_orders_v0")
    with pytest.raises(ValueError, match="strictly below"):
        list(
            MexcUiObserver(
                [
                    {
                        "symbol": "TAOUSDT",
                        "bid": 100.0,
                        "ask": 100.0,
                        "received_at": BASE.isoformat(),
                    }
                ]
            ).iter_observations()
        )
