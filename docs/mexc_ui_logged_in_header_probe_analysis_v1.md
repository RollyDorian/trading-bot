# MEXC logged-in header probe analysis v1

STATUS: `MEXC_UI_LOGGED_IN_HEADER_PROBE_ANALYSIS_READY`

DECISION: `STOP_FOR_LEAD_REVIEW`

EXTENSION: `1.3.2`

CATALOG: `v1.2`

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false**

Prior: `docs/mexc_ui_logged_in_header_probe_v1.md`

## Purpose

Explain why the 1.3.0 logged-in capture reported 8 `commonItem` header nodes
and zero Fair/Index/Funding title hits, using only the 1.3.1
`market_header_probe` plus that capture's catalog version. No mom/gap
inspection, no 5–15 min locale gate, no positional Fair/Index mapping.

## Evidence used

| Source | Role |
| --- | --- |
| `mexc_ui_capture_57e805e7-8665-431f-8704-113df7c55b4a_2026-09-03T18-21-57-069Z.ndjson` | 1.3.1 logged-in `/ru-RU/futures/TAO_USDT`; SHA-256 `2da83ad802f92600d36fad7e4cf0a99a6eddb53b26eca7958c45f9e76dab2a28`; 138 snapshots; **one** probe (`fnv1a32:a059c18c`) |
| 1.3.0 short-gate NDJSON | Contrast only: `selector_catalog_version=v1`, `header_item_count=8`, title hits `(0,0,0)` on all 3194 rows |
| Screenshot `2026-09-03 22:22:16` | Corroborates visible labels already present in the probe. Not used to invent aliases. |

The 1.3.1 probe is the structural source of truth. Extra header columns visible
on the screenshot are **not** in the probe and are not classified.

## Probe item structure (all 3 items)

Direct children are always two `i` nodes: title then value. No
`title` / `aria-*` / `data-title` attributes. No redacted private subtrees.
`current_title_token_matched` and `current_value_token_matched` are **true**
on every item.

| Index | Item class tokens | Title text | Value text | Title class | Value class |
| --- | --- | --- | --- | --- | --- |
| 0 | `…__commonItem` | Индексная цена | 228,90 | `…__itemTitle` + `…__itemTitleWrapper` | `…__itemContent` |
| 1 | `…__commonItem` | Справедливая цена | 228,78 | same | `…__itemContent` |
| 2 | `…__commonItem` + `…__fundingItem` | Ставка финансирования / Обратный отсчет | +0,0025% / 01:38:39 | `…__itemTitle` | `…__itemContent` |

Parsed fields on every 1.3.1 snapshot: index 228.90, mark 228.78, funding
+0.0025%, selectors `header_struct:*`, locale `ru-RU`, catalog **v1.1**.
Simultaneous title hits `(1,1,1)`.

## Failure-mode classification

| Hypothesis | Verdict |
| --- | --- |
| Title class mismatch (`itemTitle`) | **Rejected.** Probe flags true; CSS-module names contain `itemTitle`. |
| Value class mismatch (`itemContent`) | **Rejected.** Probe flags true. |
| Changed nesting | **Rejected.** Stable two-child title/value `i` pair. |
| Different logged-in labels | **Rejected.** Exact strings already in v1.1 aliases after slash normalization. |
| Duplicated/hidden header variants in this probe | **Not observed.** `matched_item_count=3`, one signature, nothing redacted. |
| Positional 2nd=Fair / 3rd=Index | **Not used.** Titles are unique and sufficient. |
| Stale catalog v1 (no `market_header` aliases) | **Accepted for the 1.3.0 8/0 capture.** That export stamped `selector_catalog_version=v1`. v1 JSON has English Fair/Mark labels only and **no** `field_title_aliases`. `extractMarketHeader` still counts `contractDetail`+`commonItem` nodes, then `lookup[title]` is empty → zero hits even when titles are present. |

The 1.3.1 `isVisible` filter explains 3 vs 8 matched nodes. The probe does not
include the extra five 1.3.0 nodes, so their classes are not asserted. Zero
title hits on 1.3.0 do not require a class mismatch once aliases are missing.

## Remediation (extension 1.3.2 / catalog v1.2)

Smallest deterministic change:

1. If `market_header.field_title_aliases` is missing/empty, use the
   probe-verified default alias table (same strings as v1.1/v1.2).
2. Register the exact observed funding title
   `Ставка финансирования / Обратный отсчет` (spaces around `/`).
3. Record `header_alias_count` so a zero-alias catalog is visible.
4. Fixture `tao_logged_in_ru_header_probe.html` reproduces the three probe items.

No positional mapping. No private/account/order DOM. Locale parsing and raw
text unchanged. Full 5–15 min locale gate not run here.

## Decision

**STOP_FOR_LEAD_REVIEW.** Do not retune mom/gap. Do not start a long corpus
until lead accepts 1.3.2 and a later short recapture if required.
