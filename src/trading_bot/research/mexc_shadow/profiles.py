"""Sample research profiles. Hypotheses, not claimed ground truth."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from trading_bot.research.mexc_shadow.config import (
    EngineConfig,
    ShadowParams,
    SignalParams,
    ThrottleParams,
)

# Visible |mom|~3–5.5 and |gap|~1.5–5.2 from author TAO logs are minima only.
# No upper cap is encoded. Exact mom/gap identities are not claimed.
AUTHOR_OBSERVED_NOTE = (
    "author_observed_v0 reconstructs visible TAO-log bands as hypotheses: "
    "long mom>0 and gap<0; short mom<0 and gap>0; mom_abs_min_bps=3.0; "
    "gap_abs_min_bps=1.5; target_multiplier=2.0; rapid_adverse 4.3 bps / 2s; "
    "hard_stop_bps=12; trail_activation_bps=7.0; trail_retrace_bps=6.5; "
    "risk_down_notional_multiplier=0.7 from the logs. "
    "time_stop_seconds=60 and risk_down/restore triggers are placeholders "
    "not present in the logs. Throttle is unlimited except one virtual "
    "position per symbol. Do not treat these values as fitted truth."
)

CONSERVATIVE_NOTE = (
    "conservative_v0 keeps the same signal/shadow hypotheses as "
    "author_observed_v0. Hourly 10 and daily 250 caps apply only to accepted "
    "shadow signals as research/risk notification controls, not anti-detection "
    "pacing. Every raw candidate is still stored. One virtual position per symbol."
)


def author_observed_v0() -> EngineConfig:
    return EngineConfig(
        profile_id="author_observed_v0",
        signal=SignalParams(),
        shadow=ShadowParams(),
        throttle=ThrottleParams(),
        provenance_note=AUTHOR_OBSERVED_NOTE,
    )


def conservative_v0() -> EngineConfig:
    return replace(
        author_observed_v0(),
        profile_id="conservative_v0",
        throttle=ThrottleParams(max_shadow_per_hour=10, max_shadow_per_day=250),
        provenance_note=CONSERVATIVE_NOTE,
    )


PROFILE_BUILDERS: dict[str, Callable[[], EngineConfig]] = {
    "author_observed_v0": author_observed_v0,
    "conservative_v0": conservative_v0,
}


def load_profile(profile_id: str) -> EngineConfig:
    try:
        return PROFILE_BUILDERS[profile_id]()
    except KeyError as exc:
        raise ValueError(f"unknown mexc_shadow profile {profile_id!r}") from exc
