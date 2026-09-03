# MEXC UI locale data semantics remediation v1

STATUS: `MEXC_UI_LOCALE_DATA_SEMANTICS_REMEDIATION_SHORT_GATE_FAIL`

DECISION: `STOP_FOR_LEAD_REVIEW`

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false** (frozen `author_observed_v0` / `conservative_v0`)

Prior milestone: `docs/mexc_tao_long_observation_v1.md`

## Purpose

Make read-only capture semantically correct on logged-in
`/ru-RU/futures/...` before another long strategy corpus.
No mom/gap/exit/threshold tuning, ML, PAPER, or live execution.

## What changed

Extension **1.3.0**, selector catalog **v1.1**.

1. Locale-aware `parseNumber` / `parsePrice` from pathname
   (`ru-RU`, `en-US`, otherwise `unknown`). Unknown mode fails closed
   on commas instead of stripping them.
2. Wrapper bid/ask keep exact bounded DOM `raw_text` plus `raw_tokens`,
   `parser_locale`, and selector. `String(parsed_value)` is gone.
   Adjacent digit-only nested spans fail closed.
3. Capture-time symbol from `/futures/TAO_USDT` and
   `/xx-XX/futures/TAO_USDT` only.
4. Fair / Index / Funding prefer `contractDetail` + `commonItem` +
   `itemTitle` / `itemContent`. Locale labels were copied from the
   live rendered header, not guessed.

### Parser rules

| Mode | Path | Decimal | Grouping | Fail closed |
| --- | --- | --- | --- | --- |
| `ru-RU` | `/ru-RU/futures/...` | comma | space/NBSP or 3-digit periods | `218.11` |
| `en-US` | `/en-US/futures/...` | point | 3-digit commas | `218,11` |
| `unknown` | `/futures/...` or other locales | point if unambiguous | spaces only | any comma; `1.234` |

Examples: ru-RU `218,11` → 218.11; en-US `218.11` → 218.11;
`1,234.56` en-US → 1234.56; unknown `218,11` → parse failure.
Original raw text is preserved. Prices never come from `%` fields.

### Header structure (verified 2026-09-03 on public ru-RU TAOUSDT)

Logged-out market header only. This is **not** the 5–15 min operator gate.

- Bounded root: CSS module class contains `contractDetail` and `commonItem`
- Exclude `lastPriceWrapper` and `rateItem` (24h change percent)
- Title `itemTitle` / value `itemContent`
- Last: `lastPrice` excluding `lastPriceWrapper`
- BBO: `asksWrapper`/`sell` and `bidsWrapper`/`buy` (same as 1.2.x)
- Verified titles: Индексная цена, Справедливая цена,
  Ставка финансирования/Обратный отсчет, Книга ордеров
- Order-book and last prices on that page used a decimal comma in one
  text node (`‎218,11`), which the old parser stored as `21811`

Strategy-ready fields: bid, ask, last, mark/fair, index, symbol.
Funding is captured when present and is not blocking.

## Historical 11.67h corpus

- role: `CAPTURE_INFRASTRUCTURE_EVIDENCE`
- name: `mexc_ui_capture_sessions_2026-09-03T04-12-21-619Z.ndjson`
- sha256: `7a41c34d4ae855850cd8a1a47e438e940c38e09d6f5a555c3f397e5650da9c2a`
- rewrite: **False**
- rescale /100: **False**
- present: True
- sha256_match: True

mark/index were never captured and wrapper raw_text was String(parsed) after unconditional comma stripping. Heuristic /100 cannot restore the original DOM decimal representation.

It remains infrastructure evidence. It is not a mom/gap corpus.

## Short /ru-RU/ validation

- path: `data\mexc_ui_capture\mexc_ui_capture_07923da2-8d39-4da1-86f8-cc30a6c14e97_2026-09-03T16-17-00-980Z.ndjson`
- snapshots: 3194
- duration hours: 0.2544
- median last/bid/ask/mark/index: 228.63 / 228.61 / 228.64 / None / None
- strategy-ready rate: 0.0
- simultaneous bid+ask+last+mark+index: 0 (0.0)
- DATA_INVALID: 0
- passed: **False**

| Gate | Result |
| --- | --- |
| `duration_5_to_15_min` | FAIL |
| `absolute_price_scale` | PASS |
| `bid_lt_ask` | PASS |
| `symbol_taousdt` | PASS |
| `mark_index_present` | FAIL |
| `mark_index_not_swapped_selectors` | FAIL |
| `no_selector_ambiguity_burst` | PASS |
| `no_post_readiness_data_invalid` | PASS |
| `sequence_storage_ok` | PASS |
| `export_replay_deterministic` | PASS |
| `ru_RU_parser_mode` | PASS |
| `wrapper_raw_text_not_stringified` | PASS |

HYPOTHESIS_SMOKE is not strategy evidence.

### Operator screenshot vs nearest snapshots

One logged-in screenshot (`data/mexc_ui_capture`, local `2026-09-03 20:13:03` = `16:13:03Z`):

| Visible UI | Captured near 16:13:03Z |
| --- | --- |
| last `228,87` | last `228,91` raw `228,91` at `16:13:03.191Z` (age 669 ms) |
| Fair `228,94` | mark missing on every snapshot |
| Index `229,11` | index missing on every snapshot |
| book bid/ask ~`228,97` / `228,98` | bid `228,91` ask `228,93` (same second; later `228,87` / `228,88`) |

Absolute scale matches (TAO ~229, not ~22900). Bid stays below ask. Symbol is `TAOUSDT` from `/ru-RU/futures/TAO_USDT`. Wrapper `raw_text` keeps the decimal comma (`228,71`). Last is within a few cents of the screenshot, not a ×10/×100 error.

Header diagnostics: `header_item_count=8`, `header_title_hits_mark/index/funding=0` on all sampled rows. Structural `contractDetail` items are found; verified ru-RU title aliases do not hit the logged-in header. Duration is 15.26 min (gate max 15.00).

## Decision

**STOP_FOR_LEAD_REVIEW.** Short gate failed: Fair/Index/Funding were not extracted on the logged-in page, and duration slightly exceeded 15 minutes. Do not start ML or PAPER. Do not retune frozen profiles. Do not start another long strategy corpus until header extraction is fixed and a passing 5–15 min recapture exists.

