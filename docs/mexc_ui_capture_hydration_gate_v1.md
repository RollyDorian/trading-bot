# MEXC UI capture hydration gate v1

STATUS: `MEXC_UI_CAPTURE_HYDRATION_GATE_READY`

DECISION: `STOP_FOR_LEAD_REVIEW`

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false** (frozen `author_observed_v0` / `conservative_v0`)

Prior capture milestone: `docs/mexc_ui_market_observation_capture_v1.md`

## Purpose

Harden read-only MEXC UI capture correctness and durability, then prove
the feed against a **hydrated** public `TAO_USDT` futures page before any
long observation session.

Profiles were not tuned. No mom/gap/exit/threshold/throttle changes.

## What changed

### 1. Field age

`age_ms` is no longer `previous.age_ms + intervalMs` (or snapshot-dt).

Each field stores `changed_at_monotonic_ms`. While the parsed value is
stable:

`age_ms = round(now_monotonic - changed_at_monotonic_ms)`

A mutation burst that reprints the same bid does not inflate age.

The age clock resets when:

- page host/path or symbol changes
- `capture_id` changes (extension capture restart)
- a field goes missing then becomes valid again

Python: `trading_bot.research.mexc_shadow.ui_capture.age`.
Extension: `lastChangeMono` / `lastValue` in `content.js`.

### 2. Durable local capture

The service-worker in-memory array and silent 20k truncation are gone.

Extension IndexedDB (`mexc_ui_capture_v1`):

- sessions + bounded chunk records (250 snapshots / chunk)
- sequence assigned on commit
- SW suspend/restart keeps committed chunks
- export reconstructs ordered NDJSON (`session_start`, snapshots,
  `session_end`) via popup Blob (not a data-URL)
- storage errors fail closed, stop capture, and surface in the popup

Python mirror: `DurableCaptureStore` (same contract, used in tests and
to persist this session's hydrated sample). No B2 / no network upload.

### 3. Live selectors (fail closed)

Hydrated MEXC does not ship `data-mexc-capture` attributes.

| Field | Live source | Not used |
| --- | --- | --- |
| symbol | `/futures/TAO_USDT` → `TAOUSDT` | ticker list / ticket |
| mark | unique **Fair Price** label values | Index, ticket, last |
| index | unique **Index Price** label values | Fair Price |
| bid / ask | unique `asksWrapper` / `bidsWrapper` + `sell` / `buy` price nodes | ticket, Fair/Index, arbitrary Order Book tab |
| last | unique `lastPrice` class token, excluding `lastPriceWrapper` and the **Last Price** dropdown | document title as a mapper (title happened to match last in this sample) |
| funding | label + numeric uncle (`+0.0050%/…`); sibling `/` is not a number | Open Interest |

Two distinct Order Book tab nodes are **not** used as BBO. Disagreeing
matches stay `ambiguous` / invalid. Ticket region `data-mexc-capture-ignore`
still wins in fixtures.

## Hydrated operator sample

Page: `https://www.mexc.com/futures/TAO_USDT` (already open, numeric,
not `--`). Read-only. No clicks, no login, no orders.

The Cursor browser cannot load the unpacked MV3 extension. The 150 s
sample was taken in-page with the **same wrapper/label contract**, then
committed through `DurableCaptureStore` to local NDJSON (gitignored):

`data/mexc_ui_capture/hydration_gate/tao_hydrated_150s.ndjson`

Screenshots (gitignored):

`data/mexc_ui_capture/hydration_gate/screenshots/`

| File | Approx. local time | Visible |
| --- | --- | --- |
| `mexc_tao_hydration_header.png` | 21:25 UTC+4 | TAOUSDT, Index 224.82, Fair 224.77, Last ~224.78, ask 224.84, bid 224.77 |
| `mexc_tao_hydration_compare_1.png` | 21:32 UTC+4 | TAOUSDT, Index 224.65, Fair 224.52, Last ~224.43, **ask 224.43 / bid 224.42** |
| `mexc_tao_hydration_compare_end.png` | 21:36 UTC+4 | TAOUSDT, Index 224.65, Fair 224.52, **ask 224.43 / bid 224.42** |

### Nearby raw snapshots vs screenshots

Session `hydration-gate-v1-tao-2026-09-01`

- First raw: `2026-09-01T17:34:08.226Z` — bid **224.42**, ask **224.43**,
  mark **224.52**, index **224.65**, last **224.73**, valid.
- Last raw: `2026-09-01T17:36:38.806Z` — bid **224.42**, ask **224.43**,
  mark **224.52**, index **224.65**, last **224.73**, valid.

Required match:

- Symbol `TAOUSDT` from the futures path. Correct.
- Bid below ask (224.42 < 224.43). Sides not swapped.
- Mark (Fair 224.52) vs index (224.65) **not swapped**.
- Compare_1 and end screenshots show the same best bid/ask as the raw
  stream. Header screenshot is ~9 minutes earlier; BBO had moved, as
  expected.
- Last moved between compare_1 (~224.43, coinciding with the ask) and
  capture start (224.73). During the 150 s window last changed 26 times
  and ended at 224.73 (page title after unlock also 224.73). Do not treat
  last as BBO.
- Trading ticket (Isolated 20x, Market, Sign Up / Log In) was not read
  as market prices.
- Unique `asksWrapper`/`bidsWrapper` (n=1). No arbitrary tab pick.

Funding is **missing in this CDP sample** because the in-page loop only
read the Funding Rate sibling (`/`). The extension uncle-walk sees
`+0.0050%` on both duplicate headers (same rate). Fixture + uncle logic
cover that path; this sample’s missingness must not be read as “funding
absent on MEXC”.

## Timing-quality report (hydrated sample)

| Metric | Value |
| --- | --- |
| snapshot count | 249 |
| capture duration | 150580 ms (~2.51 min) |
| MutationObserver fires / emits | 115 / 99 |
| interval emits | 149 |
| manual emits | 1 |
| inter-snapshot p50 / p95 / p99 | 957 / 1006 / 1022 ms |
| min / max interarrival | 30 / 1980 ms |
| invalid count | 0 |
| bid≥ask | 0 |
| simultaneous bid+ask+mark+index | 249 / 249 |
| sequence diagnostics | none |
| `timing_adequacy` | `MARGINAL_FOR_FEW_BPS` |

Per-field change counts (first snapshot counts as a change): bid 1,
ask 1, mark 1, index 1, last 26.

Per-field age: bid/ask/mark/index stay at the opening values for the
whole window (p50 age ~75.6 s, max ~150.6 s). Last p50 age 2991 ms,
p95 8999 ms, max 12055 ms.

BBO did not tick for 2.5 minutes on this TAO book while last did. A
~1 s extract cadence (full DOM walk) is **marginal** for later few-bps
mom/gap reconstruction: last updates are visible on a few-second scale;
bid/ask were static here so this sample cannot prove sub-second BBO
timing. Do not retune profiles against that.

## Replay

```text
python -m trading_bot.research.mexc_shadow.ui_capture quality --raw data/mexc_ui_capture/hydration_gate/tao_hydrated_150s.ndjson --out data/mexc_ui_capture/hydration_gate/quality.json
python -m trading_bot.research.mexc_shadow.ui_capture replay-smoke --raw data/mexc_ui_capture/hydration_gate/tao_hydrated_150s.ndjson --out data/mexc_ui_capture/hydration_gate/replay_smoke.json
```

- quality SHA-256 (normalized replay rows):
  `7d26316a3c78384badead3e4095eb9fce37a237dd957a86b40699eda0b030eb6`
- replay-smoke `author_observed_v0`: 249 observations, 0 candidates,
  0 trades, `PIPELINE_SMOKE_ONLY`

Zero candidates is not an edge result. Do not interpret frozen-profile
PnL as strategy evidence. Do not tune mom/gap against this replay.

## Safety (unchanged)

No clicking, order submission, Python browser driver in the package,
credentials, private endpoints, anti-detection, jitter, ML, PAPER, or
live trading. Hibachi COLLECT-only. No B2 for this capture.

## Follow-on (not this milestone)

- Operator long session on desktop Chrome with the unpacked extension
  (IndexedDB durability under SW suspend).
- Funding presence on an extension export (uncle-walk).
- Faster extract if lead wants sub-second BBO (not a profile tune).
