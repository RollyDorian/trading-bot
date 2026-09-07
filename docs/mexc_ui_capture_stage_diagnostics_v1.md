# MEXC UI capture stage diagnostics v1

MILESTONE: `MEXC_UI_CAPTURE_STAGE_DIAGNOSTICS_V1`

STATUS: `MEXC_UI_CAPTURE_STAGE_DIAGNOSTICS_READY`

DECISION: `STOP_FOR_LEAD_REVIEW`

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false**

MOM/GAP: **not inspected**

Protocol v2.0.0: **unchanged**

Catalog: `v1.2` (unchanged)

Extension: `1.3.3` → `1.3.4`

Scheduler / mutation coalescing / persistence batching / priority: **unchanged**

Live diagnostic capture: **NOT_RUN**

## Purpose

Instrument the existing COLLECT-only MEXC UI capture path so a later short
Chrome run can separate timer delivery from queue wait, extraction, IPC,
IndexedDB append, and ACK. This milestone does not redesign the FIFO, does
not coalesce mutations, does not batch persistence, and does not start ML,
PAPER, LIVE, or mom/gap retune.

## Instrumented path

`timer_delivery → content_queue → dom_extraction → send_ipc → background_queue → indexeddb_append → ack`

Interval callbacks record: `interval_callback_ordinal`, `expected_deadline_mono`, `callback_mono`, `callback_delay_ms`, `content_enqueue_mono`, `content_queue_wait_ms`, `extract_start_mono`, `extract_end_mono`, `extract_duration_ms`, `send_start_mono`, `ack_end_mono`, `total_ack_latency_ms`.

Background records, correlated by `request_ordinal` / `session_id` /
`producer_epoch` / `session_generation`: `request_ordinal`, `session_id`, `producer_epoch`, `session_generation`, `background_receive_mono`, `background_queue_wait_ms`, `append_start_mono`, `append_end_mono`, `append_duration_ms`, `worker_boot_id`.

Cumulative counters: `expected_interval_opportunities`, `timer_callbacks`, `callbacks_enqueued`, `tasks_started`, `snapshots_persisted`, `acked`, `abandoned`.

`document.visibilityState` is stamped at callback and extraction. Visibility
and page-lifecycle transitions are retained up to 128
events. Producer epoch plus session generation are stamped so multiple
producers or stale queued work can be **detected**. Stale work is not
rejected in this milestone (detect-only fencing).

## Observation timestamps

`received_at_local`, `observed_at_local`, and `monotonic_ms` remain extract
completion on the live DOM. Expected timer deadlines are diagnostic fields
only. Never backdate market observations to `t0 + n * interval`.

Append completion is not written into the same IndexedDB transaction after
that transaction finishes. `append_end_mono` travels on the ACK and in the
session-end summary / completion ring. Missing completion evidence is
`unknown`, not a zero duration.

## Bounds and privacy

- interval details cap: 2048
- mutation samples: first 128, then every
  16, cap 512
- slow-stage examples: 256
- lifecycle events: 128
- IDs ≤ 64 chars; enums ≤ 32
- checkpoint gap ≥ 5000 ms, fire-and-forget on
  an already-awake worker (no extra 5 s timer that keeps the service worker
  alive)
- no HTML, account, order, or private DOM in diagnostic objects
- counters and histograms still update after detail caps
  (`diagnostic_truncated` plus suppressed counts)

## What this does not do

No live 12-minute visibility experiment was run here. No 8–12 h corpus. No
protocol v2 threshold change. Frozen profiles are untouched. Hibachi COLLECT
is untouched.

## Lead-review next step

12-minute visibility sequence from cadence architecture review §4.4 after lead review; do not start the 8-12h corpus or retune mom/gap.
