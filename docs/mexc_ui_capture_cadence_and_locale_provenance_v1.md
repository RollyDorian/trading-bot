# MEXC UI capture cadence and locale provenance v1

STATUS: `MEXC_UI_CAPTURE_CADENCE_AND_LOCALE_PROVENANCE_READY`

DECISION: `STOP_FOR_LEAD_REVIEW`

EXTENSION: `1.3.3`

CATALOG: `v1.2` (unchanged)

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false**

MOM/GAP: **not inspected**

Long capture: **not started**

## Purpose

Capture-infrastructure fix only. Interval ticks must remain a heartbeat, and
locale must be an explicit provenance record rather than an implied pathname
side effect. Frozen p95/99% timing bars are unchanged.

## Observation heartbeat

The content script previously skipped an `interval` emit when the
bid/ask/last/mark/index key matched the previous snapshot (unless the header
probe signature changed). That skip is removed.

Every configured interval tick now extracts the live DOM and commits a raw
snapshot even when those values are identical. MutationObserver snapshots remain
allowed. Repeated equal values are new observations. Missing fields stay missing;
nothing is copied from an earlier snapshot. `age_ms` still measures time since
the last value change, so an unchanged price may accumulate age on a freshly
observed snapshot. `sample_interval_ms` stays on every snapshot; session start
and end records keep `interval_ms`.

## Locale provenance

Resolution, recomputed on every extract (SPA path/language changes included):

1. recognized localized futures pathname (`/ru-RU/futures/...`, `/en-US/futures/...`);
2. else, for bare `/futures/...`, normalized `document.documentElement.lang`;
3. else unknown, with the existing fail-closed number parser.

Document-lang mapping is only `ru` / `ru-RU` → `ru-RU` and `en` / `en-US` →
`en-US`. No punctuation guessing. If path and document language both resolve
and disagree, the path wins and `locale_path_document_disagree` is true.

Every snapshot now stamps `parser_locale`, `locale_source`
(`path` | `document_lang` | `unknown`), `document_lang`, and `page_path`.

## Verification

Deterministic tests cover unchanged interval ticks, mutation interleaved with
heartbeat, ru-RU and en-US paths, bare path plus `lang="en"` / `lang="ru"`,
missing/unsupported lang fail-closed, and SPA locale/path recomputation.

## Lead-review next step

Reload extension **1.3.3** for the next operator capture. This milestone does
not start a replacement long corpus and does not retune mom/gap.
