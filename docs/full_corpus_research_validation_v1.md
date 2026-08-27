# Full-corpus research validation v1

STATUS: `FULL_CORPUS_RESEARCH_VALIDATED`  
ML_DECISION: `COLLECT_MORE_DATA_FIRST`  
Created: `2026-08-11T13:57:05Z`

Operator artifacts (gitignored): `data/research/full_corpus/reports/`.  
This document is the versioned milestone report.

## DATA QUALITY

Verified B2 RAW was materialized read-only, checksum-verified, and merged:

| Corpus | RAW ids | Rows | market_state_1s | valid_book |
| --- | --- | ---: | ---: | ---: |
| prior_continuous | 6207906..7471912 | 1,264,007 | 76,312 | 100% |
| generation_g_7471913 | 7471913..7871912 | 400,000 | 24,175 | 100% |

- Total RAW events: **1,664,007** (~42.2 MiB merged Parquet).
- Distinct UTC days in emitted market-state: **4** (2026-08-06/07/09/10).
- Usable hours (sum of emitted 1s rows): ~**28 h** (prior ~21.2 h continuous; generation ~6.7 h across discontinuous windows).
- Normalization: **0 rejections** on both corpora; topic counts match inventory trades (3,979 / 1,073).
- Orderbook quality tag is entirely `valid_best_effort_legacy` because Hibachi WS payloads do not supply `exchange_sequence` in this corpus (`local_sequence` is present).
- Gap fix: market_state no longer invents 1s rows across multi-hour archive discontinuities (stale tops cleared; mid/OFI history reset on jump).

### Hibachi payload semantics (evidence from RAW)

From 20k prior orderbook rows:

- `messageType` at envelope top-level: Snapshot / Update.
- `depth=20` always in sample; Snapshot ~40 levels (20/side); Update median ~3 changed levels.
- Levels are `{price, quantity}` objects under `data.bid.levels` / `data.ask.levels`.
- Updates include **zero quantity** (delete) and **non-zero** (replace at price) — level-replacement semantics, not pure size deltas.
- `ask_bid_price` complements reconstructed top-of-book (sizes used for imbalance/OFI); mid/spread prefer reconstructed book when `valid_book`.
- Reconnect: `connection_id` changes invalidate reconstruction until Snapshot (code + observed distinct connection ids: prior 8, generation 5).

## SIGNAL

Exploratory Spearman IC on prior (not OOS-tuned), strongest pairs:

| Feature | Horizon | IC | Rows |
| --- | ---: | ---: | ---: |
| microprice_dev_bps | 5s | ~0.304 | 76,305 |
| imbalance | 5s | ~0.302 | 76,305 |
| imbalance | 15s | ~0.292 | 76,295 |
| basis_mark_bps | 15s | ~0.283 | 76,295 |
| ofi_5s | 5s | ~0.273 | 76,302 |
| ret_5s_bps | 5s | ~0.272 | 76,300 |

IC decays toward 30–60s. Generation (held OOS, informational) shows similar 5–15s strength for imbalance / microprice / OFI.

Feature naming: `signed_trade_flow_1s` is taker signed volume (not OFI). True Cont-style top-of-book OFI is implemented as `ofi_1s` / `ofi_5s` / `ofi_15s` from causal `ask_bid_price` changes.

## COSTS

| Component | Class |
| --- | --- |
| Bid/ask fill (never mid) | OBSERVED_FROM_DATA (path) / MODELED (policy) |
| Taker fee 4.5 bps/side | PLACEHOLDER |
| Slippage 2 bps | MODELED |
| Latency penalty 1 bp | MODELED |
| Execution delay 1s | MODELED (0s = theoretical upper bound) |
| Funding 1 bp / 8h | PLACEHOLDER (negligible vs fees on 5–60s; prior funding PnL ~0.19 on imbalance) |

Round-trip friction before spread ≈ **15 bps** (9 fee + 4 slip + 2 latency). Short-horizon gross edges of ~0–0.3 bps/trade cannot survive this under current assumptions.

## BASELINES

Predeclared params only; base cost + 1s delay. Prior = exploratory; generation = reserved OOS (not used for threshold search).

### Prior (exploratory)

| Strategy | Trades | Gross bps/trade | Net bps/trade | Net PnL |
| --- | ---: | ---: | ---: | ---: |
| momentum | 109 | -0.03 | -15.03 | -164 |
| mean_reversion | 109 | -0.67 | -15.67 | -171 |
| imbalance | 3586 | +0.23 | -14.77 | -5297 |

Imbalance alone shows tiny positive **gross**; fees/slip/latency dominate.

### Generation OOS (informational)

All three strategies net ~**-15 to -17 bps/trade** under base costs. Full optimistic/base/conservative × delay 0/1/2 matrices are in operator JSON reports.

## SPLITS

- Exploratory/train: `prior_continuous` (2026-08-06T12:21Z → 2026-08-07T09:33Z).
- Validation: chronological tail of prior if needed; do not tune on OOS.
- OOS: `generation_g_7471913` reserved.
- Only **4 UTC days** / ~**28 usable hours** — insufficient independent regimes for trustworthy ML selection.

## LEAKAGE / REPRODUCIBILITY

- Leakage audit: **PASS** (prior + generation).
- Generation content-hash rerun: **match** for market_state / features / labels.

## PERFORMANCE

| Dataset | Runtime | market_state | features | labels |
| --- | ---: | ---: | ---: | ---: |
| generation | ~29 min | 3.9 MiB | 2.5 MiB | 0.5 MiB |
| prior | ~83 min | 12.4 MiB | 7.9 MiB | 1.7 MiB |

Bottlenecks: Python normalize parse of ~1.3M events; loading orderbook JSON into market_state; 27-way baseline matrix. Practical for weeks of data on an operator machine; not for the 1 GiB VPS.

## PRODUCTION

- Collector: **running / healthy** during validation; not restarted.
- B2: read-only materialize; not mutated.
- Hot PostgreSQL: not scanned for historical research.

## LIMITATIONS

- Few calendar days / regimes despite millions of RAW events.
- No exchange_sequence → book quality is best-effort, not sequence-verified.
- Fee/funding not exchange-verified.
- Top-of-book vs quote disagreement distribution not fully tabulated (OFI uses quotes; mid uses book).
- Gross IC exists at 5–15s but does not survive modeled round-trip costs in simple baselines.

## ML_DECISION

`COLLECT_MORE_DATA_FIRST`

Reason: pipeline is trustworthy at corpus scale (normalize, causal market_state, leakage, repro), and short-horizon microstructure features show exploratory IC — but temporal coverage is too thin for model selection / OOS claims, and cost-aware baselines are deeply net-negative under justified friction.

## NEXT (not executed)

Continue production collection while improving analysis on the existing corpus; define minimum additional regime/time coverage before ML evaluation.
