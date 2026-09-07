# MEXC mom/gap hypothesis-identification protocol v2 data-contract amendment

PROTOCOL_ID: `MEXC_MOM_GAP_HYPOTHESIS_PROTOCOL_V2_DATA_CONTRACT_AMENDMENT`

PROTOCOL_VERSION: `2.0.0`

STATUS: `MEXC_MOM_GAP_HYPOTHESIS_PROTOCOL_V2_DATA_CONTRACT_READY`

DECISION: `STOP_FOR_LEAD_REVIEW`

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false**

## 1. Amendment scope and reason

This is a data-contract-only amendment to the accepted v1.0.0 preregistration
in `mexc_mom_gap_hypothesis_protocol_design_v1.md`. The v1 document remains an
immutable historical preregistration and is not edited in place.

Version 2.0.0 exists because capture remediation advanced from selector catalog
`v1.1` to exact catalog `v1.2` before any corrected 8–12 hour `TAOUSDT` long
identification corpus was collected. It does not exist because a strategy,
candidate, response, PnL, or other outcome was observed. No `mom` or `gap` was
calculated on the short-gate capture, and no PnL was inspected.

The v1 source incorporated by reference has:

- protocol version: `1.0.0`;
- repository blob: `4e8d05940b2bc759acff881779159a7bcba1604e`;
- file SHA-256: `4535601da88c6e0bd432d21081a9e98598b99a0d937395e53e102f2306cfa5b5`.

Only the capture-contract text identified in sections 3–5 below is replaced.
Every other v1 definition, algorithm, threshold, test, status, and decision rule
is normative without reinterpretation.

## 2. Exact frozen inheritance from v1.0.0

The following remain exactly unchanged:

1. all seven formula families `F01` through `F07`, with their original formula
   orientation, follower/reference assignments, and rationales;
2. lookbacks `H = {1 s, 2 s, 5 s}` and all 21 family/lookback cells;
3. the 500 ms causal grid;
4. the 1,000 ms as-of bound;
5. the 2,000 ms raw-gap/session reset and episode rearm rules;
6. all author sign, magnitude, target, and exit-scale anchors;
7. the 60-event total and 20-per-side sufficiency floors;
8. transition-based episode construction and clustering;
9. six consecutive equal-duration temporal blocks and all block gates;
10. response horizons `{1 s, 2 s, 5 s, 10 s}`, with `{2 s, 5 s}` primary;
11. all 99% Wilson mechanism gates and the registered F06 executable-mid rule;
12. all magnitude-band, frequency, target/unit, and temporal-stability rules;
13. all cell statuses and the rejection/identification ordering;
14. the two-independent-capture external-reference conclusion gate;
15. the prohibition on PnL-, target-reach-, p-value-, score-, or ML-based
    candidate selection.

No candidate may be added, removed, renamed, reoriented, simplified, expanded,
or tuned under v2.0.0. Sections 3, 4, 6, 7, 8, and 9 of v1 apply verbatim.
The v1 evidence boundary, including the author-log anchors and unavailable
author-frequency treatment, also applies verbatim.

## 3. Replacement for v1 section 2.2 capture-contract paragraph

The admissible capture contract remains `mexc_ui_raw_snapshot`, schema version
1, but selector catalog is exactly `v1.2`. An admissible corpus must use the
heartbeat observation contract and explicit locale provenance defined below.
It must retain executable bid/ask, last, Fair/mark, index, parse status, raw
text/tokens, observation validity, receipt/monotonic timing, sequence/session
metadata, configured interval, parser mode, and locale provenance.

Missing, unparsable, ambiguous, crossed, or non-simultaneous required fields
remain unavailable. No value may be inferred from position, order, account, or
other private UI. The historical semantically invalid 11.67-hour capture and
all short-gate captures remain excluded from hypothesis statistics.

## 4. Heartbeat observation contract

The future long corpus must come from a capture implementation configured to
persist a fresh raw observation on every scheduled interval tick while the
capture session is active, including ticks on which every market value is
unchanged.

For this contract:

- each persisted tick is a new raw snapshot with a new session-local sequence,
  receipt timestamp, and monotonic timestamp;
- unchanged field values and unchanged raw text are valid and must not suppress
  the tick;
- mutation-triggered observations may be additional rows but do not replace
  scheduled interval observations;
- diagnostic deduplication may suppress repeated diagnostics, but must never
  suppress the raw market observation itself;
- session metadata and the input manifest must record the configured interval
  and attest that heartbeat observation mode was enabled;
- sequence/chunk validation and trigger counts must be sufficient to audit that
  configured interval observations were persisted throughout each session.

This requirement changes capture admissibility only. It does not change the
500 ms analysis grid, the 1,000 ms as-of bound, the 2,000 ms reset rule, or the
timing thresholds. Tick loss is judged by the existing sequence/chunk checks and
the unchanged interarrival bars, not by a new fitted tolerance.

## 5. Replacement input admissibility gate

Before feature calculation, the executor must write an input manifest containing
path, byte count, SHA-256, capture IDs, session IDs, UTC bounds, schema/catalog
versions, extension version, protocol commit SHA, configured capture interval,
heartbeat-contract attestation, page-path values, `document_lang` values, parser
modes, and the numeric-scale audit result.

The corpus is `ADMISSIBLE` only if all conditions hold:

1. The protocol designer did not inspect the corpus before the v2 protocol
   commit.
2. It is a new corrected long capture produced after catalog-v1.2 remediation.
   The excluded historical 11.67-hour file, every short-gate capture, and all
   fixtures are forbidden as identification data.
3. Schema is exactly `mexc_ui_raw_snapshot` v1 and selector catalog is exactly
   `v1.2` on every raw observation.
4. The heartbeat observation contract in section 4 is attested and auditable:
   configured interval ticks produced fresh raw observations even when market
   values were unchanged.
5. Sequence/chunk validation reports no unexplained duplicate, reversal, or
   missing committed chunk.
6. At least 8.0 usable hours remain after the unchanged session/time-gap
   exclusions.
7. At least 95% of 500 ms grid rows contain simultaneous valid bid, ask, last,
   mark, and index from the same as-of snapshot; `bid < ask`; all five parse
   statuses are `ok` or `ok_redundant`.
8. Raw interarrival p95 is `≤ 1,000 ms` and at least 99% of raw interarrivals
   are `≤ 2,000 ms`. These are the unchanged v1 timing bars.
9. Locale provenance passes exactly one of these routes for every session:
   - a localized `/xx-XX/futures/...` page path has a supported path locale
     and that locale exactly matches parser mode; or
   - a bare `/futures/...` page path has an explicitly stamped supported
     `document_lang` locale and that locale exactly matches parser mode.
10. `unknown` path locale, `unknown` `document_lang`, missing `document_lang`
    on a bare path, unsupported locale, or disagreement among the applicable
    provenance field and parser mode is inadmissible.
11. Retained raw text/tokens for bid, ask, last, mark, and index permit an
    independent numeric-scale audit under the recorded locale, and that audit
    passes without rescaling, reconstruction, or inferred decimal placement.

Supported locale means a locale explicitly supported by exact selector catalog
`v1.2` and its parser contract; it is not inferred from numeric punctuation,
browser settings, timezone, account language, or screenshots. A localized path
is authoritative for its locale and cannot be overridden by `document_lang`.
For a bare path, `document_lang` is mandatory provenance rather than a fallback
guess.

Failure of any item yields `DATA_INADEQUATE`, not rejection of a formula cell
and not permission to relax the gate. No feature, episode, response diagnostic,
or PnL calculation may occur before this gate passes.

## 6. Consequential wording updates only

These updates align references to the amended data contract without changing
any scientific rule.

### 6.1 External-reference gate

V1 section 10 applies unchanged. Its requirement that simultaneous
BBO/last/Fair/index coverage and locale semantics pass now means that each of
the two independent captures must pass every v2.0.0 admissibility condition,
including exact catalog `v1.2`, heartbeat observations, explicit locale
provenance, numeric-scale audit, and the unchanged timing bars.

All remaining external-reference prerequisites and the conclusion ordering are
unchanged. `EXTERNAL_REFERENCE_PROBABLY_REQUIRED` still requires two independent
new admissible captures on two UTC dates, each with at least 8 usable hours, and
reproduced rejection of every evaluable MEXC-only cell. Otherwise the result is
`MEXC_ONLY_INCONCLUSIVE`.

### 6.2 Grok execution instructions

V1 section 11 applies unchanged, with these data-contract additions before any
feature calculation:

1. record this v2 protocol commit SHA with the future corpus SHA-256;
2. verify exact schema v1 and selector catalog `v1.2`;
3. verify heartbeat attestation, configured interval, sequence/chunk continuity,
   and raw interarrival bars;
4. verify localized-path or bare-path `document_lang` provenance and exact
   parser-mode agreement;
5. verify retained raw text/tokens and numeric scale;
6. verify simultaneous valid bid/ask/last/mark/index coverage;
7. stop with `DATA_INADEQUATE` if any check fails.

After admission passes, Grok must materialize and hash the same fixed 500 ms
grid and execute all 21 v1 cells in the original order. It must not inspect PnL,
use a short-gate capture, or select parameters from outcomes.

Required report metadata is the v1 list plus:

```text
selector_catalog_version=v1.2
configured_interval_ms
heartbeat_observation_contract
heartbeat_audit_status
page_paths
document_lang_locales
parser_modes
locale_provenance_status
numeric_scale_audit_status
raw_interarrival_p95_ms
raw_interarrival_fraction_le_2000ms
simultaneous_valid_five_field_fraction
```

## 7. Version lock

V2.0.0 freezes the complete v1.0.0 hypothesis-identification protocol plus only
the data-contract amendments in this document. A future executor may not use the
catalog change, heartbeat rows, or locale provenance to alter formulas,
lookbacks, episode construction, blocks, bands, frequency logic, mechanism
tests, response horizons, rejection ordering, or the external-reference rule.

Any further change requires a new protocol version committed before another
still-hidden admissible corpus is opened. Capture-code implementation belongs to
a separate Grok-owned lane and is outside this amendment.

## Decision

`STOP_FOR_LEAD_REVIEW`
