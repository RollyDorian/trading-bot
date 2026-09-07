# MEXC UI interval-only capture remediation v1

MILESTONE: `MEXC_UI_INTERVAL_ONLY_CAPTURE_REMEDIATION_V1`

STATUS: `MEXC_UI_INTERVAL_ONLY_CAPTURE_REMEDIATION_READY`

DECISION: `STOP_FOR_LEAD_REVIEW`

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false**

MOM/GAP: **not inspected**

Protocol v2.0.0: **unchanged** (frozen)

Catalog: `v1.2` (unchanged)

Extension: `1.3.4` → `1.3.5`

## Purpose

Remove mutation raw rows from the visible-path observation stream. The 1.3.4
visibility experiment showed final-visible content FIFO backpressure from
MutationObserver snapshots sharing emitChain with the 500 ms heartbeat.
This milestone implements interval-only raw observations. It does not restore
hidden-tab 2 Hz, does not batch IndexedDB, and does not decouple persistence.

## Required capture semantics

Raw market observations are:

1. one explicit initial/manual observation at session start;
2. one fresh full DOM observation on every configured 500 ms interval callback.

`MutationObserver` must not enqueue a raw market snapshot. It may only
increment bounded diagnostic counters and set `dirty_since_last_interval`.
The interval observation rereads every required DOM field regardless of that
flag. The dirty flag never skips a heartbeat, never copies cached market
fields into a new observation, and never forward-fills missing fields.

Observation timestamps stay extract-time: `received_at_local`,
`observed_at_local`, `monotonic_ms`. Locale provenance, catalog v1.2,
symbol/header/BBO parsing, sequence/storage fail-closed behavior, and stage
diagnostics remain. Visibility is still recorded.

Intended steady-state raw rate is approximately 2 Hz plus the single initial
row.

## What this does not do

- persistence decoupling
- IndexedDB batching
- alternate hidden-tab timer sources
- backdating or catch-up observations
- hidden-tab 2 Hz
- mom/gap retune, ML, PAPER, LIVE, protocol v2 changes, or a long capture

## Lead-review next step

Lead review, then a short visible-tab gate against frozen protocol v2. Do not start the 8–12 h corpus in this milestone.
