# MEXC UI market observation capture v1

STATUS: `MEXC_UI_MARKET_OBSERVATION_CAPTURE_READY`

DECISION: `STOP_FOR_LEAD_REVIEW`

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false** (frozen `author_observed_v0` / `conservative_v0`)

MEXC_ZERO_FEE_SIGNAL_RECON_AND_ENGINE_V1: provisionally accepted; parameters unchanged.

## Purpose

Obtain a deterministic, timestamped, **strictly read-only** MEXC UI
market-data stream that can be replayed through
`trading_bot.research.mexc_shadow`.

Chain:

```
MEXC UI → UiRawSnapshot → Observation → append-only local NDJSON
        → CaptureNdjsonSource / MexcUiObserver → frozen shadow smoke
```

DOM selectors live only in the capture catalog. They are not wired into
mom/gap/threshold logic.

## Safety boundary

Capture code (Python + unpacked MV3 extension) must not:

- click Buy / Sell / Open / Close / Cancel
- submit forms
- call private or trading endpoints
- inspect or load credentials
- change leverage, margin, or order settings
- attempt CAPTCHA / rate-limit / anti-bot bypass
- generate synthetic human behavior (no jitter, no random delays)

It may only read values already rendered on the user's open futures page
and write them locally.

Python still does not drive a browser. The extension observes the page
the operator already opened.

## Operator capture (unpacked extension)

Load `extensions/mexc_ui_capture/` as an unpacked Chrome/Edge extension.
Open a futures contract, including locale URLs such as
`https://www.mexc.com/futures/TAO_USDT` or
`https://www.mexc.com/ru-RU/futures/TAO_USDT?type=linear_swap`.
Start capture from the popup. Interval is 250 / 500 / 1000 ms as a
sampling fallback beside MutationObserver. Export NDJSON locally.
Ingest with:

```text
python -m trading_bot.research.mexc_shadow.ui_capture quality --raw FILE --out REPORT.json
python -m trading_bot.research.mexc_shadow.ui_capture replay-smoke --raw FILE --out SMOKE.json
```

Local files belong under `data/mexc_ui_capture/` (gitignored). No B2.

## Schema

`mexc_ui_raw_snapshot` version 1 records:

- `received_at_local`, `observed_at_local`, `monotonic_ms`, `sequence`
- `exchange_display_at` when the UI exposes a timestamp (else null)
- symbol, bid, ask, sizes, last, mark (Fair Price), index, funding
- visible depth levels when a unique `data-mexc-capture="orderbook"` root exists
- per-field `selector_id`, `parse_status`, `match_count`, `age_ms`
- `observation_valid` and `invalid_reasons`

Missing UI values stay null. `--` is missing, not a number. Mid is not
written unless a mid field exists (v1 has none). Shadow PnL uses
ask→bid (long) and bid→ask (short).

Fail closed: disagreeing duplicate selector hits mark the field
`ambiguous` and the snapshot invalid. Raw lines are still appended.

## Live page session (this milestone)

An automated read-only open of `https://www.mexc.com/futures/TAO_USDT`
confirmed **labels**: Index Price, Fair Price, Funding Rate / Countdown,
Last Price, Order Book (Price / Quantity / Total). Numeric cells were
`--` (SPA not hydrated in that browser). Title showed a last-like
`224.86 [TAOUSDT]`; that title prefix is **not** mapped to `last`.

| Field | Synthetic fixture | Live hydrated page this session |
|---|---|---|
| symbol | yes (attr + `/futures/TAO_USDT`) | path/title only; DOM still `--` |
| bid / ask / sizes | yes (order book max bid / min ask) | **unavailable** (book `--`) |
| last | yes (Last Price) | **unavailable** (`--`) |
| mark (Fair Price) | yes | **unavailable** (`--`) |
| index | yes | **unavailable** (`--`) |
| funding | yes (first number + `%`) | **unavailable** (`--`) |
| depth | yes when unique orderbook root | **unavailable** |
| exchange_display_at | optional attr | **unavailable** |
| screenshot vs bid/ask | fixture-proven | **not possible**; book not rendered |

`LIVE_PAGE_HYDRATION`: `UNAVAILABLE_IN_SESSION`.
A short manual capture on a hydrated desktop browser remains an
**operator step** before a long observation session. This environment
did not bypass that hydration gap.

## Fixture pipeline (completed)

Synthetic DOM fixtures prove:

- ticket Buy/Open Long prices inside `data-mexc-capture-ignore` are dropped
- ambiguous Index Price disagreement invalidates the snapshot
- `--` stays null
- append-only NDJSON never rewrites prior bytes
- replay of the same file is deterministic (SHA-256 of normalized rows)
- executable shadow PnL is not mid-to-mid
- frozen `author_observed_v0` smoke runs as `PIPELINE_SMOKE_ONLY`

## Precision for future mom/gap

Not established on live data in this session. Fixtures show bid, ask,
mark, and index can coexist on the same snapshot with a shared
`received_at_local`. Whether a hydrated MEXC page updates those fields
with enough joint precision is **unanswered** until an operator capture
is reviewed against screenshots.

## What this milestone does not do

- No ML, PAPER, or live orders
- No strategy-parameter retune
- No profitability from the short/failed-hydration view
- No production/B2 capture integration
- No long observation session

## Decision

**STOP_FOR_LEAD_REVIEW** before a long TAO capture. Next operator step:
hydrated-page extension capture, screenshot check of bid/ask/mark/index,
then only if those match, a longer local NDJSON session.
