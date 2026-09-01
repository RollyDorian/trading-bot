"""Isolated MEXC signal/shadow research engine.

Market observation → local features → signal → virtual position only.
This package must not place, cancel, or modify orders, click trading UI,
call private trading endpoints, or load trading API credentials.
"""

from trading_bot.research.mexc_shadow.config import (
    DEFAULT_COST_SCENARIOS,
    MAKER_6_BPS,
    TAKER_8_BPS,
    ZERO_FEE,
    EngineConfig,
    ShadowParams,
    SignalParams,
    ThrottleParams,
    engine_config_from_mapping,
    load_engine_config_json,
)
from trading_bot.research.mexc_shadow.engine import run_shadow_replay
from trading_bot.research.mexc_shadow.profiles import (
    author_observed_v0,
    conservative_v0,
    load_profile,
)
from trading_bot.research.mexc_shadow.source import (
    MarketDataSource,
    MemorySource,
    MexcUiObserver,
    ReplayFixtureSource,
)
from trading_bot.research.mexc_shadow.types import (
    Candidate,
    Observation,
    ReplayReport,
    ShadowTrade,
)

__all__ = [
    "DEFAULT_COST_SCENARIOS",
    "MAKER_6_BPS",
    "TAKER_8_BPS",
    "ZERO_FEE",
    "Candidate",
    "EngineConfig",
    "MarketDataSource",
    "MemorySource",
    "MexcUiObserver",
    "Observation",
    "ReplayFixtureSource",
    "ReplayReport",
    "ShadowParams",
    "ShadowTrade",
    "SignalParams",
    "ThrottleParams",
    "author_observed_v0",
    "conservative_v0",
    "engine_config_from_mapping",
    "load_engine_config_json",
    "load_profile",
    "run_shadow_replay",
]
