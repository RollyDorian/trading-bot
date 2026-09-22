# MEXC TAO corrected long-corpus admissibility v1

STATUS: `MEXC_TAO_CORRECTED_LONG_CORPUS_ADMISSIBLE`

DECISION: `STOP_FOR_LEAD_REVIEW`

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false**

MOM/GAP: **not inspected** (0 of 21 cells executed)

## Purpose

Score the first corrected long TAOUSDT corpus after the accepted
extension-1.3.5 visible-path gate against frozen protocol **v2.0.0**.
Bad periods are not cropped. The 21 identification cells are not run.

## Locked inputs

- protocol v2 merge commit: `0b4f761bb4c7fa8a6e4520901bfc32f8f2bf59d1`
- protocol v2 amendment commit: `cb679745b1fda6ecde509d8f2656232a89dd192b`
- corpus path: `data/mexc_ui_capture/mexc_ui_capture_e41b48eb-852c-4a36-88b6-9fc9a10ced32_2026-09-21T06-04-21-527Z.ndjson`
- corpus sha256: `5c15b9714f804f8df5a327ae81fb2d7fb515ec052aeed0ff1af5df5a8680467c`
- byte count: 394807859
- extension: `1.3.5`
- catalog: `v1.2`
- operator condition: intended continuously-visible Chrome session

## Capture

- snapshots: 76311
- utc_start: `2026-09-20T19:16:41.575Z`
- utc_end: `2026-09-21T05:52:41.206Z`
- usable hours: 10.59875
- wall hours: 10.5998975
- usable segments: 2
- page_paths: `{'/futures/TAO_USDT': 76311}`
- parser_locale: `{'en-US': 76311}`
- locale_source: `{'document_lang': 76311}`
- document_lang: `{'en-US': 76311}`
- locale routes: `{'document_lang': 76311}`
- trigger_counts: `{'manual': 1, 'interval': 76310}`
- first_trigger: `manual`
- heartbeat unchanged interval commits: 40683
- expected/timer/persisted interval: 76312 / 76310 / 76310
- visibility_states: `{'visible': 76311}`
- visible→hidden transitions: 0
- producer_epochs: `['2319bbf6-8330-431f-8f81-2c9785e82531']`
- worker_boot_ids: `['2c184f05-d4f9-455c-9e97-da15ac8b6b9e']`
- grid rows / five-ok / rate: 76311 / 76311 / 100.0%
- grid sha256: `3bf630648ee2abfa1839d720c5e5ffe271266e2d6c6c7453870cc0b92a698d89`
- grid written: `data/mexc_ui_capture/mexc_tao_corrected_long_corpus_grid_v1.ndjson`
- DATA_INVALID after ready: 0
- selector ambiguity count: 0
- median last: 264.56
- raw interarrival p50/p90/p95/p99/max: 500.0 / 512.0 / 515.0 / 523.0 / 4131.0
- frac≤2000ms / n>2000ms / n>1000ms: 0.999986895557594 / 1 / 3
- replay_canonical_sha256: `c8a2e8524c002221df9ffdf95d86e4e2c8643fa5ac23227ff8b26299d2c262ad`
- passed: **True**

| Gate | Result |
| --- | --- |
| `designer_did_not_inspect_before_v2_commit` | PASS |
| `new_corrected_long_capture_not_excluded` | PASS |
| `schema_mexc_ui_raw_snapshot_v1` | PASS |
| `catalog_v1_2` | PASS |
| `configured_interval_500_ms` | PASS |
| `extension_1_3_5` | PASS |
| `heartbeat_unchanged_interval_commits` | PASS |
| `heartbeat_persisted_equals_timer_callbacks` | PASS |
| `sequence_chunk_continuity` | PASS |
| `no_storage_errors` | PASS |
| `usable_hours_ge_8` | PASS |
| `locale_v2_provenance` | PASS |
| `no_unknown_locale_on_strategy_ready` | PASS |
| `symbol_taousdt` | PASS |
| `exactly_one_manual_start_raw_row` | PASS |
| `remaining_raw_rows_interval` | PASS |
| `zero_mutation_raw_rows` | PASS |
| `visibility_lifecycle_evidence` | PASS |
| `operator_intended_continuously_visible` | PASS |
| `no_unexplained_producer_session_worker_transitions` | PASS |
| `grid_simultaneous_coverage_ge_95pct` | PASS |
| `frozen_interarrival_p95` | PASS |
| `frozen_interarrival_le_2000` | PASS |
| `raw_text_tokens_match_locale_scale` | PASS |
| `absolute_price_scale` | PASS |
| `bid_lt_ask` | PASS |
| `mark_index_not_swapped` | PASS |
| `no_sustained_selector_ambiguity` | PASS |
| `no_post_readiness_data_invalid` | PASS |
| `export_replay_deterministic` | PASS |
| `corpus_sha256_matches_locked_manifest` | PASS |
| `corpus_byte_count_matches_locked_manifest` | PASS |

### Failure classes

- none

## Findings

Every frozen protocol-v2 long-corpus input gate passed. The 500 ms causal grid was materialized once and hashed. The 21 cells were not evaluated.

One or more raw gaps exceeded 2,000 ms (n=1, max=4131.0 ms). Those holes were excluded from usable hours by the frozen 2,000 ms reset; they were not cropped to manufacture other bars. The 99% ≤2,000 ms interarrival bar still passed on the whole session.

## Hours and grid

- required usable hours: 8.0
- usable hours after gap exclusions: 10.59875
- 500 ms grid rows: 76311
- as-of bound: 1000 ms
- gap reset: 2000 ms

## Decision

**STOP_FOR_LEAD_REVIEW.** Do not execute the 21 cells. Do not retune mom/gap. No PnL, ML, PAPER, or LIVE.

