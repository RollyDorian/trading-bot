# MEXC UI locale data semantics final gate v1

STATUS: `MEXC_UI_LOCALE_DATA_SEMANTICS_FINAL_GATE_FAIL`

DECISION: `STOP_FOR_LEAD_REVIEW`

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false**

MOM/GAP: **not inspected**

Long capture: **not started**

## Purpose

Score a 5–15 minute logged-in `/ru-RU/futures/TAO_USDT` capture from
extension 1.3.2 / catalog v1.2 before any replacement long corpus.
Frozen 95% simultaneous coverage and interarrival bars are applied to
this sample. The 8-hour corpus-length bar is not applied here.

## Capture

- path: `data/mexc_ui_capture/mexc_ui_capture_56f504d4-5dbe-47b7-8df9-2448fb6f96e6_2026-09-07T12-54-45-135Z.ndjson`
- sha256: `5665207fd95c46611fecf8a2082ba39ceb5809ada5f7dd26aefdb4807f9613e9`
- snapshots: 1083
- duration minutes: 8.0742
- page_paths: `{'/futures/TAO_USDT': 1083}`
- locales: `{'unknown': 1083}`
- field parser_locale: `{'unknown': 5415}`
- catalog: `{'v1.2': 1083}`
- min header_alias_count: 9
- header_item_count: `{'3': 1083}`
- header title hits mark,index,funding: `{'1,1,1': 1083}`
- median last/bid/ask/mark/index: 265.23 / 265.24 / 265.27 / 265.28 / 265.28
- simultaneous bid+ask+last+mark+index: 1083 (100.0%)
- strategy-ready: 1083 (100.0%)
- DATA_INVALID: 0
- STARTUP_WARMUP: 0
- missing-field bursts: 0
- raw interarrival p95_ms / frac≤2000ms / n>2000ms: 1917.0 / 0.987985212569316 / 13
- raw_text comma/dot counts: 0 / 5415
- passed: **False**

| Gate | Result |
| --- | --- |
| `duration_5_to_15_min` | PASS |
| `page_path_ru_RU_futures` | FAIL |
| `parser_locale_ru_RU` | FAIL |
| `catalog_v1_2` | PASS |
| `header_alias_count_positive` | PASS |
| `symbol_taousdt` | PASS |
| `absolute_price_scale` | PASS |
| `bid_lt_ask` | PASS |
| `mark_index_header_struct` | PASS |
| `no_selector_ambiguity_burst` | PASS |
| `no_post_readiness_data_invalid` | PASS |
| `sequence_storage_ok` | PASS |
| `export_replay_deterministic` | PASS |
| `simultaneous_coverage_ge_95pct` | PASS |
| `frozen_interarrival` | FAIL |
| `raw_text_ru_decimal_comma` | FAIL |

### Failure classes

- `page_path_ru_RU_futures`
- `parser_locale_ru_RU`
- `frozen_interarrival`
- `raw_text_ru_decimal_comma`

### Missing-field bursts

None. Every snapshot had simultaneous bid, ask, last, mark, and index.

### Screenshots vs nearest snapshots

- 2026-09-07T16:48:04+04:00: UI last `265.02` / Fair `265.09` / Index `265.08` vs snapshot `2026-09-07T12:48:04.125Z` last 265.04 mark 265.08 index 265.08. Last within 2 cents. BBO/Fair differ by ordinary lag. Index matches 265.08.
- 2026-09-07T16:53:55+04:00: UI last `265.44` / Fair `265.49` / Index `265.48` vs snapshot `2026-09-07T12:53:54.917Z` last 265.44 mark 265.5 index 265.49. Last exact. Fair/index within 1–2 cents. Wrapper BBO a few cents behind the screenshot book.

## Findings

This sample is **not** admissible as the ru-RU locale/header final gate.
- page_path observed `{'/futures/TAO_USDT': 1083}`; required prefix `/ru-RU/futures/`.
- parser_locale observed `{'unknown': 1083}` (fields `{'unknown': 5415}`); required `ru-RU`.
- retained raw_text decimal semantics: 0 comma / 5415 dot tokens on bid/ask/last/mark/index.
- frozen interarrival preview failed (p95_ms=1917.0, frac≤2000ms=0.987985212569316, n>2000ms=13). These bars are not relaxed.
Header/catalog/scale/continuity on this file are reported above; they do not override the ru-RU path/locale requirement.

## Decision

**STOP_FOR_LEAD_REVIEW.** Do not start the long capture. Do not retune mom/gap.

Replacement long corpus remains blocked until a 5–15 min `/ru-RU/futures/TAO_USDT` recapture passes every gate above.

