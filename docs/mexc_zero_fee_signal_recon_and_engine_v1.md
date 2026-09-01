# MEXC zero-fee signal recon and engine v1

STATUS: `MEXC_ZERO_FEE_SIGNAL_RECON_AND_ENGINE_READY`

DECISION: `STOP_FOR_LEAD_REVIEW`

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

ETH_SELECTOR_SIGNAL_SCREEN_V1: **paused** (not implemented)

## Purpose

Build a strictly **signal-only / shadow-only** research engine for a
zero-fee MEXC hypothesis reconstructed from the author's TAO logs.

Flow: market observation → local features → signal → virtual/shadow
position. No order execution, no anti-bot evasion, no human-behavior
simulation.

Hibachi COLLECT, PostgreSQL, dashboard trading, `paper.py`, and
`exchange.py` are untouched.

## Safety boundary

The package `trading_bot.research.mexc_shadow` must not contain any
ability to:

- place, cancel, or modify an order
- click trading buttons
- call private trading endpoints
- load trading API credentials

`MexcUiObserver` is a read-only adapter over **already-captured**
snapshots. v1 does not open a browser, send HTTP, or attach a live
ticker.

Tests fail closed on credential-shaped config keys, forbidden source
markers, and trading/browser-driver imports.

## Reconstructed evidence (hypotheses, not truth)

Author TAO logs strongly show the following **visible** pattern. Exact
meanings of `mom` and `gap` are **not** claimed.

| Item | Visible reconstruction | Encoded as |
|---|---|---|
| Long | `mom > 0` and `gap < 0` | `classify_direction` |
| Short | `mom < 0` and `gap > 0` | `classify_direction` |
| \|mom\| | roughly 3–5.5 bps | `mom_abs_min_bps=3.0` (no upper cap) |
| \|gap\| | roughly 1.5–5.2 bps | `gap_abs_min_bps=1.5` (no upper cap) |
| Target | `target_bps ≈ 2 * abs(entry_gap_bps)` | `target_multiplier=2.0` |
| Rapid adverse | about −4.3/−4.4 bps within ~2s | `4.3` bps / `2.0` s |
| Hard stop | −0.12% = −12 bps | `hard_stop_bps=12` |
| Trail | ~6.5–7 bps retrace from max | activation `7.0`, retrace `6.5` |
| Risk-down size | notional ×0.7, later restore | `risk_down_notional_multiplier=0.7` |

Placeholders **not** taken from the logs (must not be treated as fitted):

- `time_stop_seconds=60`
- `risk_down_trigger_bps=-80`
- `risk_restore_trigger_bps=-20`

Default feature plugins (`mid_return_lookback`, `mid_vs_mark`) are
**stand-ins**. Alternate plugins exist so a later review can swap
definitions without rewriting the shadow book.

## Architecture

```
MarketDataSource
  MexcUiObserver / ReplayFixtureSource / MemorySource
        ↓
FeatureEngine   (pluggable mom/gap, causal buffer)
        ↓
CandidateGate   (store every directional candidate;
                 throttle only shadow acceptance)
        ↓
ShadowBook      (one virtual position per symbol;
                 configurable exits; risk overlay)
        ↓
cost overlay    (0 / maker 6 / taker 8 bps per side)
```

### Data adapter

`Observation` stores observed timestamp, receive time, symbol, bid,
ask, optional mid/last/mark/index, optional sizes, optional orderbook
levels, and `source`. Crossed books are rejected.

### Candidate vs throttle

Every raw directional candidate is stored, including
`filters_not_met`, `position_open`, `max_per_hour`, and `max_per_day`.

Throttle limits are research/risk notification controls, **not**
anti-detection behavior.

- `author_observed_v0`: unlimited accepted shadows except one virtual
  position per symbol
- `conservative_v0`: same hypotheses, plus max 10 accepted shadows/hour
  and 250/day (global)

### Shadow execution

Virtual states only. Exits, frozen protective-first when several fire
on the same print:

1. `HARD_STOP`
2. `RAPID_ADVERSE`
3. `TRAIL_EXIT`
4. `GAP_HIT` (executable PnL ≥ `target_multiplier * abs(entry_gap)`)
5. `TIME_STOP`

Long gross is ask→bid; short is bid→ask. Spread is inside gross.
Default fee is 0. The same closed trades are also scored at maker 6
bps/side and taker 8 bps/side (round-trip = 2 × side).

Risk overlay is account-level drawdown vs peak cumulative
notional-weighted gross. Next shadow size uses ×0.7 while in the
drawdown regime, then restores.

Open positions at end-of-replay are left open (`n_open`); v1 does not
invent an EOD flatten.

## Sample configs

- `configs/mexc_shadow/author_observed_v0.json`
- `configs/mexc_shadow/conservative_v0.json`

Python loaders: `author_observed_v0()`, `conservative_v0()`,
`load_profile(...)`. Per-symbol overlays are supported under
`symbol_overrides`.

## What this milestone does not do

- No ML, no PAPER, no live orders
- No Hibachi collector, storage, or dashboard changes
- No parameter tuning against replay results
- No claim that the engine is the author's bot, or that it is
  profitable

## Decision

**STOP_FOR_LEAD_REVIEW** before any retune of mom/gap definitions,
thresholds, or placeholders against results.
