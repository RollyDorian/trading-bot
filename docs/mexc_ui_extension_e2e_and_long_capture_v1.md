# MEXC UI extension E2E and long capture v1

STATUS: `MEXC_UI_EXTENSION_E2E_AND_LONG_CAPTURE_PHASE_A_RETRY_FAIL`

DECISION: `STOP_FOR_LEAD_REVIEW`

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false** (frozen `author_observed_v0` / `conservative_v0`)

Prior milestone: `docs/mexc_ui_capture_hydration_gate_v1.md`

## Purpose

Prove the **unpacked** read-only MV3 extension on hydrated `TAO_USDT`
(Phase A), then start an 8–12 hour observation only if every Phase A
gate passes.

## Decision

The 1.2.1 Phase A retry is **not clean**. Do **not** start Phase B.

Duplicate-heading false invalids are gone: session `1b29865a` had
`orderbook_heading_count=2` on every row, `visible_orderbook_heading_count=1`,
and **zero** invalids — all BBO from `live_asks_bids_wrapper`.

A **new** selector failure class appeared: `missing_wrapper_bbo` on the
first **4** snapshots of session `0b298494` (868 ms after start). Wrappers
were visible (`visible_asks_wrapper_count=1`, `visible_bids_wrapper_count=1`)
but prices were absent; last/mark/index were also missing. Capture
fail-closed (no invented BBO, no crossed book). BBO resumed at seq 5.

Required pass said any new selector failure class blocks Phase B.
**STOP_FOR_LEAD_REVIEW.** Lead must authorize any 8–12 h capture.

Export was **last session only** twice, not export-all. The earlier
session file remains on disk. Concatenated two-file replay is
deterministic. Screenshots agree with nearby raw after save lag.
No ML / PAPER / live orders. Profiles were not tuned.

## Phase A retry (extension 1.2.1) — 2026-09-02

Page: `www.mexc.com/futures/TAO_USDT`. Interval 500 ms, chunk size 250.

| File | Role |
| --- | --- |
| `mexc_ui_capture_1b29865a-…T15-09-39-745Z.ndjson` | session 1, last-session export |
| `mexc_ui_capture_0b298494-…T15-24-38-742Z.ndjson` | session 2, last-session export |
| `Снимок экрана 2026-09-02 190927.png` | screenshot 1 (UTC+4) |
| `Снимок экрана 2026-09-02 192432.png` | screenshot 2 (UTC+4) |

| Metric | Combined (concat) | Session 1 `1b29865a` | Session 2 `0b298494` |
| --- | --- | --- | --- |
| snapshots | 9308 | 4506 | 4802 |
| valid / invalid | 9304 / 4 (99.96% / 0.04%) | **4506 / 0** | 4798 / 4 |
| duration | span ~29.65 min incl. gap | **14.64 min** | **14.28 min** |
| chunks | 39 | 19 | 20 |
| sequence gaps / duplicates | none (per session) | 1…4506 | 1…4802 |
| trigger mix | manual 2, mutation 9251, interval 55 | 1 / 4485 / 20 | 1 / 4766 / 35 |
| interarrival p50 / p90 / p95 / p99 | — | 131 / 397 / 554 / 946 ms | 125 / 350 / 485 / 707 ms |
| storage SHA-256 | concat `33be16f3…a5e94623` | `f05906bc…f9574f64` | `9eca0b69…ed9cf470` |
| replay SHA-256 | `4b1e60cf…dadb090` (repeat identical) | `f3ee7fc6…c633036c` | `32810f5f…b4945f83` |
| chosen BBO | wrapper 9304, none 4 | wrapper 4506 | wrapper 4798, none 4 |
| headings | count 2 on almost all rows; visible 1 | 2 / visible 1 | mostly 2 / visible 1 |
| storage errors | none | none | none |
| crossed book | 0 | 0 | 0 |
| simultaneous bid+ask+mark+index | 9300 | 4506 | 4794 |
| `timing_adequacy` | `ADEQUATE_FOR_REVIEW_NOT_PROOF` | same | same |

Stop/start gap: **43.579 s** (`15:09:36.766Z` → `15:10:20.345Z`).
Session-end records: `storage_error=null`, `sequence_gaps=[]`,
`client_sequence_mismatches=[]`.

Invalid window (session 2 only): sequences **1–4**,
`2026-09-02T15:10:20.345Z`–`15:10:21.213Z`, contiguous
`missing_wrapper_bbo` + `missing_required:bid/ask`. Seq 5 already has
wrapper BBO (`216.39`/`216.42`); last/mark/index appear by seq 9–12.

### Screenshots vs nearby raw

**Shot 190927** (local 19:09:27 = `15:09:27Z`) vs seq **4417**
(`15:09:24.062Z`, −2.94 s):

| | UI | Raw |
| --- | --- | --- |
| bid | 216.56 | **216.56** |
| ask | 216.59 | **216.59** |
| last | 216.61 | **216.61** |
| Fair / mark | 216.63 | **216.63** |
| Index | 216.65 | **216.65** |

Nearest-timestamp raw (seq 4438, `15:09:26.967Z`, −33 ms) already had
BBO/last ticked to 216.40 / 216.41 / 216.50. Agreement is with the
snapshot ~2.9 s earlier (screenshot save lag). Sides not crossed.

**Shot 192432** (local 19:24:32 = `15:24:32Z`) vs seq **4733**
(`15:24:24.346Z`, −7.65 s):

| | UI | Raw |
| --- | --- | --- |
| bid | 216.63 | **216.63** |
| ask | 216.66 | **216.66** |
| last | 216.61 | **216.61** |
| Fair / mark | 216.64 | **216.64** |
| Index | 216.79 | **216.79** |

Nearest-timestamp raw (seq 4772, −179 ms) still has last **216.61**
exact; BBO had already moved to 216.70 / 216.73. Save-lag window is
longer than shot 1.

UI showed Available `0.0000 USDT` and Positions(0). No trading
interaction.

### Gates (retry)

| Gate | Result |
| --- | --- |
| unpacked extension export | PASS (1.2.1 IndexedDB) |
| IndexedDB chunks | PASS (39) |
| sequences contiguous | PASS |
| stop/start boundaries | PASS (2 sessions, 43.6 s gap) |
| prior session retained | PASS (first file kept) |
| export-all in one NDJSON | **FAIL** (last-session-only twice) |
| reload / SW restart attested | **FAIL** (not attested) |
| export hashes | PASS |
| quality stream | **FAIL** (`n_invalid=4`) |
| replay-smoke | PASS (`HYPOTHESIS_SMOKE`, concat SHA stable) |
| screenshot quote agreement | PASS (nearby exact; nearest already ticked) |
| no storage errors | PASS |
| no crossed book | PASS |
| no selector ambiguity (old heading class) | PASS (`ambiguous_orderbook_heading=0`) |
| no new selector failure class | **FAIL** (`missing_wrapper_bbo` ×4) |
| no trading interaction | PASS |
| duration 15–30 min | PASS on combined span (~29.7 min); each session ~14.5 min |

### `HYPOTHESIS_SMOKE` (not performance)

Frozen `author_observed_v0` only:

- Session 1: 4506 observations, 682 candidates, 0 trades, 0 open.
- Session 2: 4798 observations, 658 candidates, 1 trade, 0 open.
- Concat: 9304 observations, 1340 candidates, 1 trade, 0 open.

Do not retune lookbacks, mom/gap, thresholds, target, stops, throttle,
or sizing from these counts.

## Selector remediation (code)

Canonical live BBO is `asksWrapper` + ask-price class and `bidsWrapper` +
bid-price class. Duplicate **Order Book** headings are diagnostic and
heading-fallback only. Visibility is applied before ambiguity. Last is
never used to split a wrapper book. Every snapshot carries bounded
`orderbook_diagnostics`. No full-page HTML.

## Historical Phase A (1.2.0, 2026-09-02 13:22–13:58Z)

The first operator run **failed** on a 25 s contiguous burst of
`ambiguous_orderbook_heading` (111 snapshots) in session `f00ba4f6`.
Session `a073233b` was clean. That heading class did **not** recur on
the 1.2.1 retry.

## Extension 1.2.2 — localized futures URL injection

Not a new Phase A capture. Manifest 1.2.1 only injected on
`https://www.mexc.com/futures/*`, so operator pages such as
`https://www.mexc.com/ru-RU/futures/TAO_USDT?type=linear_swap` had no
content script and popup Start raised uncaught
`Receiving end does not exist`.

1.2.2 adds `https://www.mexc.com/*/futures/*` to `host_permissions` and
`content_scripts` (non-localized `/futures/*` and `futures.mexc.com`
kept). Spot paths such as `/ru-RU/spot/` stay unmatched. Popup Start
reverts persisted `capturing=false` when the receiver is absent; Stop
is safe without a content script.

Phase B remains **NOT_STARTED**. STOP_FOR_LEAD_REVIEW.

## Phase B

**NOT_STARTED.** Do not begin the 8–12 hour TAOUSDT session. A new
selector failure class (`missing_wrapper_bbo`) appeared on restart
hydration. Lead review is required before any long capture.

STOP_FOR_LEAD_REVIEW. No ML / PAPER / LIVE.
