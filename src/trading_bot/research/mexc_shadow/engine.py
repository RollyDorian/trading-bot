"""Replay orchestration: observe → features → candidate log → shadow only."""

from __future__ import annotations

from trading_bot.research.mexc_shadow.config import EngineConfig
from trading_bot.research.mexc_shadow.costs import summarize_costs
from trading_bot.research.mexc_shadow.features import FeatureEngine
from trading_bot.research.mexc_shadow.shadow import ShadowBook
from trading_bot.research.mexc_shadow.signal import CandidateGate
from trading_bot.research.mexc_shadow.source import MarketDataSource
from trading_bot.research.mexc_shadow.types import Candidate, Observation, ReplayReport


def run_shadow_replay(source: MarketDataSource, config: EngineConfig) -> ReplayReport:
    """Deterministic offline replay. Does not call any exchange."""

    feature_engines: dict[str, FeatureEngine] = {}
    gate = CandidateGate(config)
    book = ShadowBook(config.shadow)
    candidates: list[Candidate] = []
    n_obs = 0
    for observation in source.iter_observations():
        n_obs += 1
        _validate_observation(observation)
        resolved = config.for_symbol(observation.symbol)
        engine = feature_engines.get(observation.symbol)
        if engine is None:
            engine = FeatureEngine(resolved.signal)
            feature_engines[observation.symbol] = engine
        snap = engine.update(observation)
        # Exits are evaluated before a same-print re-entry.
        book.on_observation(observation)
        candidate = gate.evaluate(
            snap,
            position_open=book.position_open(observation.symbol),
            notional_multiplier=book.notional_multiplier_for(resolved.shadow),
        )
        if candidate is not None:
            candidates.append(candidate)
            if candidate.accepted_for_shadow:
                book.maybe_open(candidate, observation, resolved.shadow)
    return ReplayReport(
        profile_id=config.profile_id,
        observations=n_obs,
        candidates=candidates,
        trades=book.trades,
        n_open=book.open_count(),
        cost_summaries=summarize_costs(book.trades),
        notes=(
            "mom/gap definitions are configurable hypotheses, not claimed identities.",
            "Throttle limits are research/risk controls, not anti-detection behavior.",
            "Do not tune parameters against these replay results in this milestone.",
        ),
    )


def _validate_observation(observation: Observation) -> None:
    if observation.bid >= observation.ask:
        raise ValueError("crossed book cannot enter the shadow engine")
    if observation.executable_mid() is None:
        raise ValueError("observation missing executable mid")
