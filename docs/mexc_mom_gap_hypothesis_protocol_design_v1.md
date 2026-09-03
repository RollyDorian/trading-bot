# MEXC mom/gap hypothesis-identification protocol design v1

PROTOCOL_ID: `MEXC_MOM_GAP_HYPOTHESIS_PROTOCOL_DESIGN_V1`

PROTOCOL_VERSION: `1.0.0`

STATUS: `MEXC_MOM_GAP_HYPOTHESIS_PROTOCOL_DESIGN_READY`

DECISION: `STOP_FOR_LEAD_REVIEW`

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

## 1. Purpose

Pre-register a small, closed hypothesis family for identifying the most
plausible meanings of the author's `mom` and `gap`. This is an identity and
mechanism study, not a strategy search.

The protocol is frozen before any corrected long `TAOUSDT` corpus is inspected.
The future executor must not change formulas, lookbacks, filters, event
clustering, gates, or decision rules after seeing that corpus. Any change creates
a new protocol version and requires a new, still-hidden corpus.

This protocol does not:

- inspect or reuse the known semantically invalid 11.67-hour TAO capture as a
  mom/gap corpus;
- calculate PnL, costs, Sharpe, hit rate, or trade profitability;
- run the shadow book or use exit outcomes to choose a candidate;
- fit thresholds, tune lookbacks, build ML, start PAPER, or implement LIVE;
- assume that the author's labels are ground truth.

## 2. Frozen evidence boundary

### 2.1 Author-log anchors

The only author-derived anchors admitted by v1 are:

| Anchor | Frozen interpretation |
|---|---|
| Direction | long: `mom > 0` and `gap < 0`; short: `mom < 0` and `gap > 0` |
| Observed `mom` scale | approximately 3–5.5 bps |
| Observed `gap` scale | approximately 1.5–5.2 bps |
| Target | approximately `2 × abs(gap)` |
| Exit families | rapid adverse about 4.3–4.4 bps in about 2 s; hard stop 12 bps; trail about 6.5–7 bps |

The documented ranges are observed bands, not fitted upper limits. Candidate
generation uses the documented lower magnitudes, while band similarity is
scored separately. The 60-second time stop and account drawdown triggers are
placeholders, not author-log evidence. The repository explicitly calls the
current feature definitions stand-ins and the identities unproven
([reconstruction](mexc_zero_fee_signal_recon_and_engine_v1.md#reconstructed-evidence-hypotheses-not-truth),
[profiles](../src/trading_bot/research/mexc_shadow/profiles.py#L15-L24)).

### 2.2 Repository and schema anchors

The existing engine supplies two provisional momentum definitions, three gap
definitions, causal buffering, the author sign rule, and `2 × abs(gap)` target
construction. These are hypotheses, not facts
([features](../src/trading_bot/research/mexc_shadow/features.py#L1-L94),
[signal](../src/trading_bot/research/mexc_shadow/signal.py#L16-L61),
[types](../src/trading_bot/research/mexc_shadow/types.py#L35-L42)).

The admissible capture contract is `mexc_ui_raw_snapshot`, schema version 1,
selector catalog `v1.1`. It supports sample intervals 250/500/1000 ms and
captures executable BBO, last, Fair/mark, index, field parse status, field age,
monotonic change time, observation validity, and sequence metadata
([catalog](../src/trading_bot/research/mexc_shadow/ui_capture/catalog.py#L6-L13),
[schema](../src/trading_bot/research/mexc_shadow/ui_capture/schema.py#L148-L223)).
Missing values remain missing; normalization rejects crossed BBO and does not
invent a UI mid or missing reference price
([normalization](../src/trading_bot/research/mexc_shadow/ui_capture/normalize.py#L176-L213)).

The historical 11.67-hour capture is expressly
`CAPTURE_INFRASTRUCTURE_EVIDENCE`, not a mom/gap corpus, because mark/index were
not captured and locale parsing was not semantically recoverable
([locale remediation](mexc_ui_locale_data_semantics_remediation_v1.md#historical-1167h-corpus)).
It is excluded from all protocol statistics.

### 2.3 Missing author-frequency anchor

Current evidence does not document the number of author entries together with
their logged exposure duration. The `conservative_v0` limits of 10/hour and
250/day are notification/risk controls, not author frequency evidence
([candidate vs throttle](mexc_zero_fee_signal_recon_and_engine_v1.md#candidate-vs-throttle)).
Therefore v1 pre-registers the frequency comparison but its expected status is
`FREQUENCY_REFERENCE_UNAVAILABLE` unless the lead supplies a timestamped author
log manifest already inside the frozen evidence set. No rate may be inferred
from throttle settings, screenshots, or anecdotal density.

## 3. Notation and causal sampling

For one symbol and one capture session:

- `q(t) = (bid(t) + ask(t)) / 2`, the executable mid;
- `l(t)`, `m(t)`, and `i(t)` are captured last, Fair/mark, and index;
- `bps(a,b) = 10,000 × (a / b - 1)`;
- `r_h(x,t) = bps(x(t), x(t-h))`;
- `s_h(q,t) = bps(q(t), mean(q(u): t-h ≤ u ≤ t))`.

All calculations use arrival-available data. Within a session, order by
`monotonic_ms` and use `received_at_local` as the auditable wall-clock field.
Never use a future observation, exchange display time alone, interpolation, or
backfill from another session.

Create a 500 ms UTC grid. At grid time `t`, select the most recent whole valid
snapshot with receipt time `≤ t` and grid lag `≤ 1,000 ms`. All fields for one
row must come from that same snapshot; do not combine a newer BBO with an older
header record. For `t-h`, take the grid row exactly `h` earlier. The SMA uses
all 500 ms grid rows in the closed interval `[t-h,t]`; any missing row makes the
feature unavailable. Reset every state at a session boundary or raw time gap
greater than 2,000 ms.

Use simple percentage returns, matching the existing engine, not log returns.
Field `age_ms` is reported but is not itself a missingness rule: an unchanged
displayed price can legitimately have high age. A missing/unparsable/ambiguous
field remains unavailable.

## 4. Frozen candidate family

The only lookbacks are:

`H = {1 s, 2 s, 5 s}`

Each row below produces exactly three candidate cells, one per `h ∈ H`, for 21
cells total. Formula orientation is frozen; the executor must not swap a ratio
or multiply a feature by −1 to improve agreement.

| Family | `mom_h(t)` | `gap(t)` | Follower / reference | Rationale |
|---|---|---|---|---|
| `F01_MID_RET_MID_MARK` | `r_h(q,t)` | `bps(q,m)` | `q / m` | current default stand-in |
| `F02_MID_RET_MID_INDEX` | `r_h(q,t)` | `bps(q,i)` | `q / i` | executable momentum plus index dislocation |
| `F03_MARK_RET_MID_MARK` | `r_h(m,t)` | `bps(q,m)` | `q / m` | Fair leads executable quote |
| `F04_INDEX_RET_MID_INDEX` | `r_h(i,t)` | `bps(q,i)` | `q / i` | index leads executable quote |
| `F05_LAST_RET_MID_LAST` | `r_h(l,t)` | `bps(q,l)` | `q / l` | trade print leads executable quote |
| `F06_LAST_RET_LAST_MARK` | `r_h(l,t)` | `bps(l,m)` | `l / m` | existing `last_vs_mark` gap with last momentum |
| `F07_MID_SMA_MID_MARK` | `s_h(q,t)` | `bps(q,m)` | `q / m` | existing SMA-style momentum plus Fair dislocation |

Candidate cell IDs append the lookback: for example,
`F03_MARK_RET_MID_MARK__H2S`.

No depth, volume, funding, order-flow, volatility, lag sweep, alternate
normalization, extra price pairing, or external venue is part of this family.

## 5. Input admissibility gate

The executor must write an input manifest before feature calculation containing
path, byte count, SHA-256, capture IDs, session IDs, UTC bounds, schema/catalog
versions, extension version, and the protocol commit SHA.

The corpus is `ADMISSIBLE` only if all conditions hold:

1. It was not inspected by the protocol designer before protocol commit.
2. It is a new capture produced after locale/header remediation; the excluded
   historical 11.67-hour file and fixtures are forbidden.
3. Schema is exactly `mexc_ui_raw_snapshot` v1 and catalog is `v1.1`.
4. Sequence/chunk validation reports no unexplained duplicate, reversal, or
   missing committed chunk.
5. At least 8.0 usable hours remain after session/time-gap exclusions.
6. At least 95% of grid rows contain simultaneous valid bid, ask, last, mark,
   and index; `bid < ask`; parse statuses are `ok` or `ok_redundant`.
7. At least 99% of raw interarrivals are `≤ 2,000 ms` and p95 is `≤ 1,000 ms`.
8. Locale parser mode matches the page-path locale, and retained raw text/tokens
   support an audit of numeric scale.

Failure yields `DATA_INADEQUATE`, not rejection of any candidate and not a reason
to relax the gate.

## 6. Candidate-event construction

For each candidate cell and grid row with both features present:

- long-compatible: `mom > 0`, `gap < 0`;
- short-compatible: `mom < 0`, `gap > 0`;
- magnitude-eligible: `abs(mom) ≥ 3.0 bps` and `abs(gap) ≥ 1.5 bps`.

An event episode begins on an ineligible-to-eligible transition. It remains the
same episode through brief state flicker and rearms only after the cell has been
continuously ineligible for more than 2,000 ms. The episode representative is
its first eligible row. This collapse is used for frequency and band statistics
so DOM mutation rate cannot manufacture signal frequency.

All raw episodes count. Position state, throttle, exit state, and prior accepted
signals are ignored. A cell is fully evaluable only with at least 60 episodes,
at least 20 long and 20 short. Smaller samples are `INSUFFICIENT_EVENTS`.

For future-response diagnostics, keep the earliest representative and exclude
later representatives within 10 seconds for that cell. This non-overlapping
subset does not replace the full episode set used for frequency.

## 7. Frozen tests

### 7.1 Direct author-entry match, when scorable

This test is allowed only if the frozen author evidence manifest contains
individual entries with timestamps and printed `mom`/`gap` values. Match each
entry one-to-one to the nearest earlier grid row within 1,000 ms; never use a
later row or reuse one row for two entries.

Use the printed decimal precision plus one source price tick converted to bps as
the numeric tolerance, with an absolute floor of 0.2 bps. `DIRECT_MATCH_PASS`
requires at least 30 matched entries, both directions, at least 90% simultaneous
`mom` and `gap` numeric agreement, and at least 95% direction agreement. If a
printed target exists, section 7.4 must also pass. If timestamps or individual
values are absent, emit `DIRECT_MATCH_NOT_SCORABLE`; never synthesize alignment.

When more than one cell passes, direct error can identify a unique cell only if
its median tolerance-normalized joint error is at most half the runner-up's.
Otherwise the cells remain a plausible set.

### 7.2 Direction and sign

Report counts for all four `(sign(mom), sign(gap))` quadrants before applying
the author rule. Do not condition only on compatible rows when reporting base
rates.

For the event set, report long/short counts and rates per usable hour. The
formula orientation passes only as written. A one-sided-only result is not
full identification: fewer than 20 episodes on either side yields
`DIRECTION_COVERAGE_INSUFFICIENT`.

If timestamp-aligned author entries later exist inside the frozen evidence
manifest, require exact direction agreement on at least 90% of matched entries,
with a 99% Wilson lower bound above 0.50. Without aligned entries, direction is
an eligibility constraint, not discriminating evidence.

### 7.3 Magnitude-band similarity

At episode representatives calculate:

- `mom_band_share = P(3.0 ≤ abs(mom) ≤ 5.5)`;
- `gap_band_share = P(1.5 ≤ abs(gap) ≤ 5.2)`;
- `joint_band_share = P(both bands)`.

The cell passes `BAND_SIMILARITY` only if:

1. median `abs(mom)` lies in `[3.0, 5.5]` bps;
2. median `abs(gap)` lies in `[1.5, 5.2]` bps;
3. `joint_band_share ≥ 0.70` overall;
4. `joint_band_share ≥ 0.60` separately for long and short.

Report p10/p25/p50/p75/p90 and the share above each documented upper band.
Do not clamp, winsorize, standardize, or delete large values.

### 7.4 Target and exit-scale consistency

For every episode record the non-performance proxy
`target_proxy_bps = 2 × abs(gap)`. This is an algebraic unit/sign audit only.
Do not test whether price reaches it.

If a frozen author-log row supplies both printed `gap` and target, define
`target_error = abs(target_printed - 2 × abs(gap_printed))`. It is consistent
when error is at most `max(0.2 bps, 0.10 × abs(target_printed))` for at least 90%
of paired rows. Because this relation uses the printed gap, it cannot by itself
distinguish market-price formulas.

Rapid-adverse, hard-stop, and trail values are reported only as scale context.
No exit replay is allowed in candidate identification.

### 7.5 Candidate-frequency similarity

Define `candidate_rate = episode_count / usable_hours`, separately overall,
long, short, and per UTC hour.

If the frozen author evidence contains entry count `N_a` and exposure hours
`T_a`, define `author_rate = N_a / T_a`. Frequency is similar only when:

1. `0.5 ≤ candidate_rate / author_rate ≤ 2.0`; and
2. the exact 95% Poisson rate intervals for candidate and author counts overlap.

Apply the same rule by side if author direction counts are available. Do not
derive author rate from `conservative_v0` caps. If `N_a` or `T_a` is absent,
emit `FREQUENCY_REFERENCE_UNAVAILABLE`; the field is non-scoring and cannot
break a tie or be silently treated as a pass.

### 7.6 Temporal stability

Split usable grid time into six consecutive equal-duration blocks before
looking at candidates. Never choose calendar windows or regimes after seeing
results. A block is evaluable for a cell with at least 10 episodes.

`TEMPORALLY_STABLE` requires:

- at least four evaluable blocks;
- band medians inside the author bands in at least four evaluable blocks;
- block `joint_band_share ≥ 0.60` in at least four evaluable blocks;
- every evaluable block's event rate in `[0.25, 4.0] ×` the full-corpus rate;
- no one block contains more than 40% of all episodes;
- the mechanism classification in section 8 is not contradictory across
  evaluable blocks.

Failure is not repaired by dropping a block, redefining a regime, or changing a
lookback.

## 8. Momentum + lag/catch-up versus simple mean reversion

For each non-overlapping event, let `d = +1` for long and `d = −1` for short.
For future horizon `τ ∈ {1 s, 2 s, 5 s, 10 s}` and the candidate's frozen
follower `A` and reference `B`, calculate:

- `F_τ = d × bps(A(t+τ), A(t))` — follower progress in signal direction;
- `R_τ = d × bps(B(t+τ), B(t))` — reference progress in signal direction;
- `C_τ = F_τ - R_τ` — signed gap closure;
- `M_τ = d × bps(X(t+τ), X(t))`, where `X` is the return-momentum source;
  for `F07`, use `X=q`.

For closing events (`C_τ > 0`), attribute closure without PnL:

`follower_share_τ = max(F_τ,0) / (max(F_τ,0) + max(-R_τ,0))`

and define it as 1 when the denominator is zero but `C_τ > 0` because the
follower outran a same-direction reference.

Use 2 s and 5 s as the two primary horizons; 1 s and 10 s are mandatory
diagnostics and may not replace a failed primary horizon. Proportions use 99%
Wilson intervals.

Classify `MOMENTUM_LAG_CATCHUP` only when, at both primary horizons:

1. the 99% lower bound for `P(F_τ > 0)` is above 0.50;
2. the 99% lower bound for `P(C_τ > 0)` is above 0.50;
3. median `follower_share_τ ≥ 2/3`; and
4. the 99% lower bound for `P(M_τ > 0)` is above 0.50.

Classify `SIMPLE_MEAN_REVERSION` when, at both primary horizons, the 99% lower
bound for `P(M_τ < 0)` is above 0.50, or gap closure is significant but median
`follower_share_τ ≤ 1/3` and follower progress is not significant. Otherwise
classify `MECHANISM_UNRESOLVED`.

For `F06`, run the registered last/Fair decomposition and additionally require
the executable mid to move in direction `d` with a 99% lower bound above 0.50;
otherwise report `NON_EXECUTABLE_PRINT_ONLY` and reject it as the author's
actionable gap.

This test distinguishes a lagging follower catching a continuing leader from a
price simply reversing its own prior move or a reference retreating toward the
follower. It is a market-state response test, not a trade-return or PnL test.

## 9. Rejection and identification rules

### 9.1 Cell status

Assign exactly one status to every one of the 21 cells:

- `DATA_INADEQUATE`: corpus gate failed or required formula fields unavailable;
- `INSUFFICIENT_EVENTS`: fewer than 60 total or fewer than 20 per direction;
- `IDENTITY_REJECTED`: evaluable but fails a scorable direct match, band
  similarity, available frequency similarity, temporal stability,
  target/unit consistency, or yields
  `SIMPLE_MEAN_REVERSION`/`NON_EXECUTABLE_PRINT_ONLY`;
- `MECHANISM_UNRESOLVED`: identity resemblance passes but causal mechanism does
  not classify;
- `PLAUSIBLE_B`: band, stability, and catch-up pass while author frequency is
  unavailable;
- `PLAUSIBLE_A`: all gates, including available author-frequency similarity,
  pass.

Data inadequacy and insufficient events are never rewritten as hypothesis
rejection.

### 9.2 Protocol conclusion

Apply this deterministic order:

1. If the corpus is inadmissible: `DATA_INADEQUATE`.
2. If no cell is evaluable: `IDENTIFICATION_INSUFFICIENT`.
3. If exactly one cell is `PLAUSIBLE_A`: `UNIQUE_IDENTITY_SUPPORTED`.
4. If exactly one cell is `PLAUSIBLE_B` and no cell is `PLAUSIBLE_A`:
   `PROVISIONAL_UNIQUE_FREQUENCY_UNAVAILABLE`.
5. If multiple cells are otherwise plausible and the direct-error dominance
   rule in section 7.1 selects one: `UNIQUE_IDENTITY_SUPPORTED`.
6. If multiple plausible cells share one formula family but different
   lookbacks: `FORMULA_SUPPORTED_LOOKBACK_UNRESOLVED` and list all surviving
   lookbacks.
7. If multiple formula families remain: `PLAUSIBLE_SET_NOT_UNIQUE`.
8. If all evaluable cells are rejected: `MEXC_ONLY_FAMILY_REJECTED`.
9. Otherwise: `IDENTIFICATION_UNRESOLVED`.

Do not force a winner with a weighted score, smallest p-value, best future move,
best target reach, or PnL. Report all cells, including failures.

## 10. External-reference conclusion gate

`EXTERNAL_REFERENCE_PROBABLY_REQUIRED` is allowed only after two independent,
new, admissible captures on two UTC dates, each with at least 8 usable hours,
and only when all of the following are true in both captures:

1. p95 raw interarrival is `≤ 1,000 ms` and at least 99% is `≤ 2,000 ms`, so the
   documented 2-second dynamics were observable;
2. simultaneous BBO/last/Fair/index coverage and locale semantics pass;
3. every evaluable MEXC-only cell is `IDENTITY_REJECTED`; no candidate remains
   merely data-inadequate, event-insufficient, or mechanism-unresolved;
4. rejection reproduces by candidate cell and reason across both dates;
5. no local reference (last, Fair, or index) exhibits registered follower-led
   catch-up at both primary horizons;
6. the conclusion does not depend only on unavailable author-frequency data.

This outcome means the closed MEXC-only family cannot reproduce the documented
identity at adequate resolution and an unobserved leader—most plausibly an
external reference venue—should be tested next. It does **not** identify a
venue, instrument, stream, lag, or profitable strategy. Any external-feed
candidate family requires a separate pre-registration and arrival-time causal
contract; the repository's existing external-feed design likewise treats local
receipt time as the only causal clock
([timestamp model](external_relative_value_feed_design_v1.md#4-timestamp-model)).

If any prerequisite fails, use `MEXC_ONLY_INCONCLUSIVE`, not the external-feed
conclusion.

## 11. Execution instructions for Grok

1. Checkout the protocol commit and record its SHA.
2. Do not open the corrected corpus until the protocol SHA and corpus SHA-256
   are recorded together in an immutable run manifest.
3. Validate schema, capture quality, locale semantics, timing, and sequences
   before calculating any feature.
4. Materialize the fixed 500 ms grid once; hash it; reuse it for all cells.
5. Evaluate all 21 cells in listed order. Do not stop after finding a plausible
   cell.
6. Emit one machine-readable row per cell with counts, side counts, band
   summaries, frequency result, six-block stability, four-horizon mechanism
   diagnostics, rejection reason, and final status.
7. Emit a Markdown report containing the input manifest, exclusions, all cell
   results, exact decision-rule trace, and one protocol conclusion enum.
8. Do not execute the shadow book, read trade PnL, alter profiles, or propose a
   tuned replacement in the same run.
9. If code is needed to execute the protocol, keep it offline/read-only and add
   deterministic tests. Any ambiguity stops the run for lead review.
10. End with `STOP_FOR_LEAD_REVIEW`. Do not merge, start ML, PAPER, or LIVE.

Required report metadata:

```text
protocol_id
protocol_version
protocol_commit_sha
executor_name_and_version
code_commit_sha
input_sha256
grid_sha256
capture_ids
session_ids
utc_start
utc_end
usable_hours
quality_status
frequency_reference_status
cell_results[21]
protocol_conclusion
decision=STOP_FOR_LEAD_REVIEW
```

## 12. Pre-registration lock

Frozen in v1.0.0:

- 7 formula families;
- lookbacks `{1,2,5}` seconds;
- 500 ms causal grid and 1,000 ms as-of limit;
- 2,000 ms session-gap and episode-clustering limits;
- author direction and magnitude thresholds/bands;
- 60-event/20-per-side sufficiency floor;
- six temporal blocks;
- response horizons `{1,2,5,10}` seconds with primary `{2,5}`;
- 99% Wilson mechanism gates;
- all rejection, identification, and external-reference rules.

Any alteration is protocol v2+ and must be committed before inspecting a fresh
holdout corpus.

## Decision

`STOP_FOR_LEAD_REVIEW`
