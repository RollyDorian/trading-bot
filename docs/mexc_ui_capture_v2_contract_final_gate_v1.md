# MEXC UI capture v2 contract final gate

STATUS: `MEXC_UI_CAPTURE_V2_CONTRACT_FINAL_GATE_FAIL`

DECISION: `STOP_FOR_LEAD_REVIEW`

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false**

MOM/GAP: **not inspected** (0 of 21 cells executed)

Long capture: **not started**

## Purpose

Score a 5–15 minute logged-in TAOUSDT capture from extension 1.3.3 /
catalog v1.2 against the frozen mom/gap protocol **v2.0.0** data-admissibility
contract. This is not a v1.0.0 score: v2 requires exact catalog v1.2,
heartbeat interval ticks when values are unchanged, and explicit locale
provenance (localized path **or** bare `/futures/` plus `document_lang`).
The 8.0 usable-hour identification bar is not applied to this short gate.
Timing bars are unchanged from v1 (p95 ≤1000 ms, ≥99% ≤2000 ms) and are
not relaxed.

## Protocol comparison (v1.0.0 vs v2.0.0 vs this sample)

| Item | v1.0.0 | v2.0.0 | this sample |
| --- | --- | --- | --- |
| protocol version scored | 1.0.0 | **2.0.0** | **2.0.0** |
| catalog | v1.1 | exactly v1.2 | `{'v1.2': 994}` |
| heartbeat | not required | interval ticks persist when unchanged | interval=297, unchanged=284 |
| locale | path-implied | path `locale_source=path` **or** bare `/futures/` `locale_source=document_lang` | routes `{'document_lang': 994}` |
| 8.0 usable hours | identification corpus | same | **not applied** |
| p95 ≤1000 ms and ≥99% ≤2000 ms | required | unchanged | p95=1353.0 frac≤2000=0.9587109768378651 |

## Capture

- path: `data/mexc_ui_capture/mexc_ui_capture_3c6e0474-5270-4cf0-88b0-5b0207a62006_2026-09-07T14-01-05-500Z.ndjson`
- sha256: `1f83d307cc42324f803b26c01f6a6afde5eb1dc65b938909cf50eaeb09b56f2b`
- snapshots: 994
- duration minutes: 7.207
- page_paths: `{'/futures/TAO_USDT': 994}`
- parser_locale: `{'en-US': 994}`
- locale_source: `{'document_lang': 994}`
- document_lang: `{'en-US': 994}`
- locale routes: `{'document_lang': 994}`
- locale_path_document_disagree: 0
- field parser_locale: `{'en-US': 4970}`
- catalog: `{'v1.2': 994}`
- schema_version: `{'1': 994}`
- sample_interval_ms: `{'500': 994}`
- session interval_ms: `[500, 500]`
- trigger_counts: `{'manual': 1, 'mutation': 696, 'interval': 297}`
- heartbeat: interval=297 unchanged_interval_commits=284
- min header_alias_count: 9
- header_item_count: `{'3': 994}`
- header title hits mark,index,funding: `{'1,1,1': 994}`
- median last/bid/ask/mark/index: 264.5 / 264.48 / 264.51 / 264.49 / 264.48
- simultaneous bid+ask+last+mark+index: 994 (100.0%)
- strategy-ready: 994 (100.0%)
- unknown locale on strategy-ready: 0
- DATA_INVALID: 0
- STARTUP_WARMUP: 0
- missing-field bursts: 0
- raw interarrival p50/p90/p95/p99: 113.0 / 665.0 / 1353.0 / 7510.0
- frac≤2000ms / n>2000ms / n>1000ms / min / max: 0.9587109768378651 / 41 / 68 / 68.0 / 14098.0
- raw_text comma/dot counts: 0 / 4970
- scale audit fails: 0 (None)
- replay_canonical_sha256: `aff56eb1e6fc00481f40bd225caeb338046cf52f7b9b35b9d0927fb10b74a451`
- passed: **False**

| Gate | Result |
| --- | --- |
| `duration_5_to_15_min` | PASS |
| `extension_1_3_3_locale_fields` | PASS |
| `schema_mexc_ui_raw_snapshot_v1` | PASS |
| `catalog_v1_2` | PASS |
| `configured_interval_500_ms` | PASS |
| `heartbeat_unchanged_interval_commits` | PASS |
| `sequence_chunk_continuity` | PASS |
| `no_storage_errors` | PASS |
| `locale_v2_provenance` | PASS |
| `no_unknown_locale_on_strategy_ready` | PASS |
| `symbol_taousdt` | PASS |
| `raw_text_tokens_match_locale_scale` | PASS |
| `absolute_price_scale` | PASS |
| `bid_lt_ask` | PASS |
| `mark_index_not_swapped` | PASS |
| `simultaneous_coverage_ge_95pct` | PASS |
| `startup_warmup_separated_from_data_invalid` | PASS |
| `no_post_readiness_data_invalid` | PASS |
| `no_selector_ambiguity_burst` | PASS |
| `export_replay_deterministic` | PASS |
| `frozen_interarrival_p95` | FAIL |
| `frozen_interarrival_le_2000` | FAIL |

### Failure classes

- `frozen_interarrival_p95`
- `frozen_interarrival_le_2000`

### Missing-field bursts

None. Every snapshot had simultaneous bid, ask, last, mark, and index.

### Screenshots vs nearest snapshots

- 2026-09-07T17:54:14+04:00: UI last `264.50` / Fair `264.55` / Index `264.55` vs snapshot `2026-09-07T13:54:14.020Z` last 264.62 bid 264.57 ask 264.6 mark 264.57 index 264.56. Absolute prices ~264. Last within 12 cents of the screenshot. Fair/index within 2 cents of header_struct mark/index. Ordinary lag.
- 2026-09-07T17:59:17+04:00: UI last `264.56` / Fair `264.52` / Index `264.49` vs snapshot `2026-09-07T13:59:16.981Z` last 264.53 bid 264.52 ask 264.55 mark 264.57 index 264.57. Last within 3 cents. Screenshot Fair 264.52 / Index 264.49 vs snapshot mark 264.57 / index 264.57: lag, not a mark/index swap (selectors remain header_struct:mark vs header_struct:index).

## Findings

This sample is **not** ADMISSIBLE under protocol v2.0.0 data admissibility.
- v2 §5 item 8 frozen interarrival failed (p95_ms=1353.0, frac≤2000ms=0.9587109768378651, n>2000ms=41, n>1000ms=68). These bars are unchanged from v1.0.0 and are not relaxed.

## Decision

**STOP_FOR_LEAD_REVIEW.** Do not start the 8–12h corpus. Do not retune mom/gap.

Replacement long corpus remains blocked until a 5–15 min extension 1.3.3 / catalog v1.2 recapture passes every v2.0.0 short-gate bar above, including the unchanged interarrival thresholds.

