# ETH executable path quality remediation v1

STATUS: `ETH_EXECUTABLE_PATH_QUALITY_REMEDIATION_READY`
ML_STATUS: `NOT_STARTED`
DECISION: `STOP_FOR_LEAD_REVIEW`

## Eligibility freeze (before rerun economics)

- rule: A decision/path second is executable only when bid/ask come from a causally valid fresh native quote or a fresh reconstructed book under the documented 5s staleness limit, with no unresolved gap/desync/reconnect boundary on the open window.
- preferred source: `DIRECT_QUOTE_FRESH`
- max stale quote/book seconds: `5.0` / `5.0`
- bound fitted from TP/SL: `False`
- quote fallback executable: `False`
- stale carry executable: `False`

A 1s row is executable only as `DIRECT_QUOTE_FRESH` (native BBO within 5s, same connection) or `RECONSTRUCTED_BOOK_FRESH` (valid book within 5s when no fresh quote exists). DATA_INVALID is not TIMEOUT.

## Root cause

- From 2026-08-19 onward, ask_bid_price data includes timestampMs. Exact-key contract expected only bidPrice/bidSize/askPrice/askSize, so 100% of quotes failed with payload.data fields do not match contract. That is a parser classification failure, not an unknown RAW type. July 29–Aug 6 quotes lack timestampMs and parsed.

- raw_row_to_market_event read schema_version (absent on B2 rows) and defaulted to 1, labelling reconstructed books best_effort_legacy. Archive column is raw_schema_version=2. exchange_sequence is null; local_sequence is connection-global so it cannot detect missed orderbook diffs.

- Aug 19 20:50: Same connection after the 18:04Z capacity-stop resume. RAW contains trades, marks, spots, quotes, and orderbook. After the quote parser fix, 20:50 TOB is DIRECT_QUOTE_FRESH (example 20:50:14 bid 2131.68 ask 2134.26, ~12 bps, mark 2134.26). Peak 1s mid jump in the next minute is ~112 bps with a temporarily wide native book, not an archive stitch. v1 500+ bps 60s MFE mixed this burst with later far-ask flicker; magnitude is not a silent forward-fill.

- Aug 19 21:08: Native ask_bid_price itself prints bid ~2315 / ask ~2372–2404 (spread 240–360 bps) while mark, spot, and trade prints stay ~2312–2327. Reconstructed L2 ask matches the native quote ask, so this is not a reconstructor ghost invented after quote drop. Quote timestampMs repeats across ~3 Hz receipts (growing latency_ms, still <5s). Classification: one-sided/flickering native BBO (far resting ask, bid-side trades). Not quote-fallback, not a collection gap, not unknown event type. Under the frozen 5s rule it remains DIRECT_QUOTE_FRESH. A spread/mark cap would be a new eligibility rule and was not applied.

- July 30-style contamination: v1 tagged valid_book=False rows as stale/quote-fallback. Those days had parsing quotes; market_state preferred reconstructed book and used quote only when the book was invalid. Fresh native BBO was treated as contamination. After remediation it is DIRECT_QUOTE_FRESH.

- unknown types: Restored B2 parquet uses `topic`, not `event_type`. spot_check previously counted every row as unknown. Payloads are ask_bid_price/orderbook/mark_price/spot_price/funding/trades.

- Binance external: `NOT_AVAILABLE_LOCALLY`

## Remaining venue-print quality (not filtered)

Native Hibachi BBO can print a far ask while trades/mark stay on the
bid (Aug 19 ~21:08). Those seconds stay `DIRECT_QUOTE_FRESH` under
the frozen 5s rule. Options for a later eligibility rule were not
applied and must not be fitted to TP/SL hit rates.

- rows: `487257`
- spread ≥ 50 / 100 / 200 bps: `238` / `154` / `143`
- |mark−mid| ≥ 25 bps: `189`
- applied as eligibility filter: `false`
- lead options (not applied): keep 5s only; invalidate at forensic 25 bps mark-vs-mid; lead-chosen spread cap; require quote and reconstructed book to agree

## Rebuild

Parser accepts v2 `timestampMs`. Mapper reads `raw_schema_version`.
market_state_1s prefers the native quote and labels `tob_source`.
Artifacts rebuilt under `data/research/full_corpus/runs/executable_path_clean_v1/`.
v1 reports and the previous market_state tree were not overwritten.
Discovery usable hours: `107.86` → `135.35`. TOB mix: `487248`
`DIRECT_QUOTE_FRESH`, `9` `RECONSTRUCTED_BOOK_FRESH`.

## Contamination proof (primary TP/SL, offset 0)

Stale/fallback barrier resolutions are **0 by construction** (no
`QUOTE_FALLBACK` / `STALE_CARRY` row can resolve TP or SL).
`DATA_INVALID` is an unobservable window (gap/reconnect/non-executable
second), not TIMEOUT.

The v1 forensic peak gate still **FAIL**s: `15/15` largest 1s excursions
carry quality tags (mark-vs-mid and/or ≥50 bps 1s jump). Those tags are
the remaining native wide-BBO prints, not silent forward-fill. Clean
TP/SL STATUS stays `ETH_TP_SL_FIRST_TOUCH_FEASIBILITY_CLEAN_BLOCKED_BAD_DATA`
for that forensic remainder.

- max stale/fallback resolution fraction: `0.0`
- zero by construction: `True`

| H | TP | SL | dir | n_valid | DATA_INVALID | TP+SL | stale/fallback | frac |
|---:|---:|---:|---|---:|---:|---:|---:|---:|
| 120 | 20 | 5 | long | 4009 | 75 | 1317 | 0 | 0.00% |
| 120 | 20 | 5 | short | 4009 | 75 | 1264 | 0 | 0.00% |
| 120 | 20 | 10 | long | 4002 | 82 | 584 | 0 | 0.00% |
| 120 | 20 | 10 | short | 4003 | 81 | 591 | 0 | 0.00% |
| 120 | 20 | 15 | long | 4002 | 82 | 324 | 0 | 0.00% |
| 120 | 20 | 15 | short | 3999 | 85 | 337 | 0 | 0.00% |
| 120 | 20 | 20 | long | 3999 | 85 | 221 | 0 | 0.00% |
| 120 | 20 | 20 | short | 3999 | 85 | 217 | 0 | 0.00% |
| 120 | 25 | 5 | long | 4009 | 75 | 1283 | 0 | 0.00% |
| 120 | 25 | 5 | short | 4008 | 76 | 1224 | 0 | 0.00% |
| 120 | 25 | 10 | long | 4002 | 82 | 548 | 0 | 0.00% |
| 120 | 25 | 10 | short | 4002 | 82 | 546 | 0 | 0.00% |
| 120 | 25 | 15 | long | 4002 | 82 | 287 | 0 | 0.00% |
| 120 | 25 | 15 | short | 3998 | 86 | 289 | 0 | 0.00% |
| 120 | 25 | 20 | long | 3999 | 85 | 183 | 0 | 0.00% |
| 120 | 25 | 20 | short | 3998 | 86 | 169 | 0 | 0.00% |
| 120 | 30 | 5 | long | 4009 | 75 | 1269 | 0 | 0.00% |
| 120 | 30 | 5 | short | 4008 | 76 | 1207 | 0 | 0.00% |
| 120 | 30 | 10 | long | 4002 | 82 | 534 | 0 | 0.00% |
| 120 | 30 | 10 | short | 4002 | 82 | 525 | 0 | 0.00% |
| 120 | 30 | 15 | long | 4002 | 82 | 271 | 0 | 0.00% |
| 120 | 30 | 15 | short | 3997 | 87 | 267 | 0 | 0.00% |
| 120 | 30 | 20 | long | 3999 | 85 | 166 | 0 | 0.00% |
| 120 | 30 | 20 | short | 3997 | 87 | 147 | 0 | 0.00% |
| 180 | 20 | 5 | long | 2661 | 69 | 1120 | 0 | 0.00% |
| 180 | 20 | 5 | short | 2658 | 72 | 1075 | 0 | 0.00% |
| 180 | 20 | 10 | long | 2653 | 77 | 570 | 0 | 0.00% |
| 180 | 20 | 10 | short | 2651 | 79 | 578 | 0 | 0.00% |
| 180 | 20 | 15 | long | 2651 | 79 | 338 | 0 | 0.00% |
| 180 | 20 | 15 | short | 2648 | 82 | 354 | 0 | 0.00% |
| 180 | 20 | 20 | long | 2647 | 83 | 245 | 0 | 0.00% |
| 180 | 20 | 20 | short | 2648 | 82 | 251 | 0 | 0.00% |
| 180 | 25 | 5 | long | 2661 | 69 | 1084 | 0 | 0.00% |
| 180 | 25 | 5 | short | 2656 | 74 | 1042 | 0 | 0.00% |
| 180 | 25 | 10 | long | 2653 | 77 | 522 | 0 | 0.00% |
| 180 | 25 | 10 | short | 2649 | 81 | 536 | 0 | 0.00% |
| 180 | 25 | 15 | long | 2651 | 79 | 290 | 0 | 0.00% |
| 180 | 25 | 15 | short | 2646 | 84 | 309 | 0 | 0.00% |
| 180 | 25 | 20 | long | 2647 | 83 | 196 | 0 | 0.00% |
| 180 | 25 | 20 | short | 2646 | 84 | 204 | 0 | 0.00% |
| 180 | 30 | 5 | long | 2661 | 69 | 1064 | 0 | 0.00% |
| 180 | 30 | 5 | short | 2656 | 74 | 1018 | 0 | 0.00% |
| 180 | 30 | 10 | long | 2653 | 77 | 499 | 0 | 0.00% |
| 180 | 30 | 10 | short | 2649 | 81 | 510 | 0 | 0.00% |
| 180 | 30 | 15 | long | 2651 | 79 | 265 | 0 | 0.00% |
| 180 | 30 | 15 | short | 2646 | 84 | 283 | 0 | 0.00% |
| 180 | 30 | 20 | long | 2647 | 83 | 171 | 0 | 0.00% |
| 180 | 30 | 20 | short | 2646 | 84 | 177 | 0 | 0.00% |
| 300 | 20 | 5 | long | 1582 | 68 | 871 | 0 | 0.00% |
| 300 | 20 | 5 | short | 1584 | 66 | 851 | 0 | 0.00% |
| 300 | 20 | 10 | long | 1575 | 75 | 513 | 0 | 0.00% |
| 300 | 20 | 10 | short | 1576 | 74 | 534 | 0 | 0.00% |
| 300 | 20 | 15 | long | 1569 | 81 | 333 | 0 | 0.00% |
| 300 | 20 | 15 | short | 1571 | 79 | 351 | 0 | 0.00% |
| 300 | 20 | 20 | long | 1567 | 83 | 249 | 0 | 0.00% |
| 300 | 20 | 20 | short | 1570 | 80 | 251 | 0 | 0.00% |
| 300 | 25 | 5 | long | 1582 | 68 | 846 | 0 | 0.00% |
| 300 | 25 | 5 | short | 1583 | 67 | 818 | 0 | 0.00% |
| 300 | 25 | 10 | long | 1575 | 75 | 482 | 0 | 0.00% |
| 300 | 25 | 10 | short | 1575 | 75 | 497 | 0 | 0.00% |
| 300 | 25 | 15 | long | 1569 | 81 | 300 | 0 | 0.00% |
| 300 | 25 | 15 | short | 1570 | 80 | 313 | 0 | 0.00% |
| 300 | 25 | 20 | long | 1567 | 83 | 214 | 0 | 0.00% |
| 300 | 25 | 20 | short | 1569 | 81 | 211 | 0 | 0.00% |
| 300 | 30 | 5 | long | 1582 | 68 | 829 | 0 | 0.00% |
| 300 | 30 | 5 | short | 1582 | 68 | 803 | 0 | 0.00% |
| 300 | 30 | 10 | long | 1575 | 75 | 460 | 0 | 0.00% |
| 300 | 30 | 10 | short | 1574 | 76 | 474 | 0 | 0.00% |
| 300 | 30 | 15 | long | 1569 | 81 | 277 | 0 | 0.00% |
| 300 | 30 | 15 | short | 1569 | 81 | 289 | 0 | 0.00% |
| 300 | 30 | 20 | long | 1567 | 83 | 191 | 0 | 0.00% |
| 300 | 30 | 20 | short | 1568 | 82 | 187 | 0 | 0.00% |

## Before/after (frozen grids, no retune)

### First passage executable either-side hit fraction (non-overlap mean)

| cell | v1 | clean |
|---|---:|---:|
| 120s_10bps | 0.20855628724082623 | 0.19395694528090554 |
| 120s_15bps | 0.09231893296384695 | 0.08837083347799929 |
| 120s_20bps | 0.04783641752280023 | 0.04559867906931933 |
| 120s_25bps | 0.02719208887446279 | 0.025439198150640448 |
| 120s_30bps | 0.017687125168157375 | 0.016332880128534982 |
| 300s_10bps | 0.48205294345381167 | 0.4533792954330389 |
| 300s_15bps | 0.26526886859936044 | 0.2505659337850734 |
| 300s_20bps | 0.15443843668760868 | 0.14713522423317743 |
| 300s_25bps | 0.10120833073894708 | 0.09858334982468614 |
| 300s_30bps | 0.07061907776052825 | 0.06701788683263986 |
| 600s_10bps | 0.7161190339423249 | 0.6849487595049718 |
| 600s_15bps | 0.4841631982670359 | 0.4612852342778676 |
| 600s_20bps | 0.3158587909665288 | 0.30040872280599595 |
| 600s_25bps | 0.21105794300921515 | 0.20083998183801233 |
| 600s_30bps | 0.14985122573075413 | 0.14389556299576617 |
| 60s_10bps | 0.08984831195598322 | 0.08354942153619627 |
| 60s_15bps | 0.035225768661566 | 0.033302366795973946 |
| 60s_20bps | 0.01547919496355775 | 0.01461415628863769 |
| 60s_25bps | 0.008845317370624481 | 0.007835851740464932 |
| 60s_30bps | 0.00593581069219877 | 0.005037328704308805 |

### TP/SL 120/180/300 × TP20/25/30 SL10 long (offset 0)

| cell | v1 n | v1 TP-first | clean n | clean TP-first | clean DATA_INVALID |
|---|---:|---:|---:|---:|---:|
| 120s_tp20_sl10_long | 3221 | 63 | 4002 | 74 | 82 |
| 120s_tp25_sl10_long | 3221 | 29 | 4002 | 36 | 82 |
| 120s_tp30_sl10_long | 3221 | 20 | 4002 | 22 | 82 |
| 180s_tp20_sl10_long | 2141 | 86 | 2653 | 97 | 77 |
| 180s_tp25_sl10_long | 2141 | 40 | 2653 | 47 | 77 |
| 180s_tp30_sl10_long | 2141 | 22 | 2653 | 23 | 77 |
| 300s_tp20_sl10_long | 1281 | 85 | 1575 | 99 | 75 |
| 300s_tp25_sl10_long | 1281 | 54 | 1575 | 65 | 75 |
| 300s_tp30_sl10_long | 1281 | 38 | 1575 | 42 | 75 |

### UTC day TP-first 120s TP20 SL10 long

- 2026-08-19 v1 → clean: `{'before': 0.078125, 'after': 0.06666666666666667}`
- 2026-08-20 v1 → clean: `{'before': 0.05357142857142857, 'after': 0.05982905982905983}`

## Versioned rerun reports

- `docs/eth_first_passage_full_corpus_clean_v1.md`
- `docs/eth_tp_sl_first_touch_feasibility_clean_v1.md`

Existing v1 reports remain immutable.

## Lead decision

STOP_FOR_LEAD_REVIEW. Do not start ML, feature selection, TP/SL
optimization, PAPER, or live trading.

