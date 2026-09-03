# MEXC UI locale data semantics remediation v1

STATUS: `MEXC_UI_LOCALE_DATA_SEMANTICS_REMEDIATION_IMPLEMENTATION_READY`

DECISION: `GATE_PENDING_OPERATOR_CAPTURE`

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

**Pending operator capture.** Reload unpacked extension 1.3.0 on
logged-in `https://www.mexc.com/ru-RU/futures/TAO_USDT`, capture
5–15 minutes, export NDJSON, and keep screenshots of bid, ask,
last, Fair/Mark, and Index. Then re-run:

```
python -m trading_bot.research.mexc_shadow.ui_capture \
  locale-remediation --raw FILE \
  --out docs/mexc_ui_locale_data_semantics_remediation_v1.json \
  --md docs/mexc_ui_locale_data_semantics_remediation_v1.md
```

## Decision

**GATE_PENDING_OPERATOR_CAPTURE.** Do not start ML or PAPER. Do not retune frozen profiles.

