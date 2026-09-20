# MEXC UI interval-only visible final v2 gate

STATUS: `MEXC_UI_INTERVAL_ONLY_VISIBLE_FINAL_V2_GATE_PASS`

DECISION: `STOP_FOR_LEAD_REVIEW`

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false**

MOM/GAP: **not inspected** (0 of 21 cells executed)

Long capture: **not started**

## Purpose

Score a 5–15 minute logged-in TAOUSDT capture from extension 1.3.5 /
catalog v1.2 after the interval-only remediation. Protocol **v2.0.0**
data-admissibility bars are unchanged. The whole running session is
scored while visible; hidden-tab 2 Hz remains out of scope.

## Capture

- path: `data/mexc_ui_capture/mexc_ui_capture_e03fc35f-4280-486c-8144-86ffb1aa6515_2026-09-07T18-30-52-811Z.ndjson`
- sha256: `d1d4c08f785efa489760cf33d853223277f00c4837ac9773249c5345f74e3f45`
- snapshots: 1010
- duration minutes: 8.4079
- page_paths: `{'/futures/TAO_USDT': 1010}`
- parser_locale: `{'en-US': 1010}`
- locale_source: `{'document_lang': 1010}`
- document_lang: `{'en-US': 1010}`
- locale routes: `{'document_lang': 1010}`
- catalog: `{'v1.2': 1010}`
- sample_interval_ms: `{'500': 1010}`
- trigger_counts: `{'manual': 1, 'interval': 1009}`
- heartbeat expected/timer/persisted interval: 1009 / 1009 / 1009
- mutation callbacks / dirty_sets / enqueued / raw rows: 2116 / 862 / 0 / 0
- visibility_states: `{'visible': 1010}`
- visible→hidden transitions: 0
- stale_generation rows/counter: 0 / 0
- session_id_mismatch rows/counter: 0 / 0
- median last/bid/ask/mark/index: 259.03 / 259.01 / 259.03 / 259.01 / 259.0
- simultaneous bid+ask+last+mark+index: 1010 (100.0%)
- DATA_INVALID: 0
- STARTUP_WARMUP: 0
- raw interarrival p50/p90/p95/p99/max: 499.0 / 514.0 / 518.0 / 529.0 / 544.0
- frac≤2000ms / n>2000ms / n>1000ms: 1.0 / 0 / 0
- replay_canonical_sha256: `554f48839864615b9dddda0a9fbe7e915422d2d0a7def7421f3020187b3c91ea`
- passed: **True**

## FIFO before/after (1.3.4 final-visible vs this session)

Not a fitted-percentage pass bar. Measured evidence only.

- prior final_visible content wait p95: 1525.699999988079 ms
- prior content queue depth high-water: 26
- now content wait p50/p90/p95/p99/max: 0.0 / 0.09999999403953552 / 0.09999999403953552 / 0.10000002384185791 / 0.20000001788139343 ms
- now content queue depth high-water: 0
- extract duration p50/p95/max: 73.30000001192093 / 80.19999998807907 / 111.80000001192093 ms
- background wait p50/p95/max: 0.09999999403953552 / 0.19999998807907104 / 2.4000000059604645 ms
- callback delay p50/p95/max: 8.099999994039536 / 17.099999994039536 / 48.19999998807907 ms
- append duration (histogram) p50/p95/max: 16.0 / 32.0 / 41.900000005960464 ms
- ACK latency (histogram) p50/p95/max: 16.0 / 64.0 / 88.59999999403954 ms

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
| `frozen_interarrival_p95` | PASS |
| `frozen_interarrival_le_2000` | PASS |
| `extension_1_3_5` | PASS |
| `exactly_one_manual_start_raw_row` | PASS |
| `remaining_raw_rows_interval` | PASS |
| `zero_mutation_raw_rows` | PASS |
| `mutation_callbacks_not_enqueued` | PASS |
| `continuous_visibility_visible` | PASS |
| `zero_visible_to_hidden` | PASS |
| `heartbeat_expected_equals_timer_callbacks` | PASS |
| `heartbeat_persisted_equals_timer_callbacks` | PASS |
| `no_stale_generation` | PASS |
| `no_session_id_mismatch` | PASS |
| `screenshot_agreement` | PASS |

### Failure classes

- none

### Screenshots vs nearest snapshots

- 2026-09-07T22:28:44+04:00: UI last `258.7` / Fair `258.72` / Index `258.71` / bid `258.67` / ask `258.73` vs snapshot `2026-09-07T18:28:43.830Z` last 258.7 bid 258.68 ask 258.71 mark 258.72 index 258.71. Absolute prices ~258. Last delta 0.0 vs UI 258.70. Fair/mark delta 0.0; index delta 0.0. Ordinary book lag of 1–2 cents on bid/ask. Not a mark/index swap.

## Findings

Every applicable frozen protocol-v2 bar and every interval-only visible-tab bar passed. The 8–12h identification corpus is still not started.

## Decision

**STOP_FOR_LEAD_REVIEW.** Do not start the 8–12h corpus. Do not retune mom/gap.

