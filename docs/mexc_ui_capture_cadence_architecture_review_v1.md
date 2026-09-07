# MEXC UI capture cadence architecture review v1

MILESTONE: `MEXC_UI_CAPTURE_CADENCE_ARCHITECTURE_REVIEW_V1`

STATUS: `ARCHITECTURE_REVIEW_READY_DIAGNOSTICS_REQUIRED`

DECISION: `STOP_FOR_LEAD_REVIEW`

Reviewed base: `9233cee9fedae02cd38b0b36a9a3eb65774b8937` (main after PR #50).
Extension: `1.3.3`; selector catalog: `v1.2`; protocol: `2.0.0`.
Implementation changes: **none**. Live diagnostic capture: **not run**.
Mom/gap/PnL: **not inspected or calculated**. ML/PAPER/LIVE: **not started**.

## 1. Architectural verdict and evidence boundary

The current implementation guarantees neither 2 Hz observation cadence nor
bounded observation backlog. Mandatory heartbeat and optional mutation requests
share one FIFO whose next extraction waits for the previous persistence ACK.
This is a demonstrated head-of-line blocking mechanism. Its contribution to
the reported failure is not yet measured. Timer delivery, renderer work, message
delivery, and storage time cannot be separated from the existing export.

The next implementation milestone should instrument the existing pipeline first.
Do not select a persistence or scheduler replacement from row counts alone.

The [committed short-gate report](mexc_ui_capture_v2_contract_final_gate_v1.md)
provides these observations; no capture payload was opened for this review:

| Observation | What it establishes |
| --- | --- |
| 7.207 minutes; configured interval 500 ms | About 865 ideal elapsed-time opportunities, not proof that 865 callbacks ran |
| 994 rows: manual 1, mutation 696, interval 297 | Only about 34.3% of ideal opportunities appear as persisted interval rows; the roughly 568 difference is not a measured dropped-row count |
| 284 unchanged interval commits | Equal values no longer suppress every heartbeat; this does not establish complete tick delivery |
| p95 1353 ms; 95.871% <=2000 ms; p99 7510 ms; max 14098 ms | Frozen timing fails despite aggregate throughput above 2 rows/s |
| Sequence/chunks and storage-error gate pass | The committed stream is internally consistent; missing callbacks or pre-extraction tasks are not counted by its sequence |
| Simultaneous five-field coverage 100% | Captured rows have the fields; absent observations are still absent |

The 865 estimate uses the reported rounded duration. Exact opportunity accounting
needs timer-registration and Stop timestamps, because session start precedes
timer registration and the first callback is not immediate. No cause receives
a numerical share of the deficit without the proposed diagnostic run.

Source references below are pinned to the reviewed commit. They describe code
behavior, not measured latency. Platform sources were consulted 2026-09-07.

## 2. End-to-end execution and hidden boundaries

| Stage | Evidence at reviewed base | Consequence |
| --- | --- | --- |
| Producer callbacks | [content.js:1408–1448](https://github.com/RollyDorian/trading-bot/blob/9233cee9fedae02cd38b0b36a9a3eb65774b8937/extensions/mexc_ui_capture/content.js#L1408-L1448): body-wide MutationObserver calls `emit("mutation")`; timer calls `emit("interval")` | MutationObserver already groups mutations into callback deliveries, but the application neither coalesces those deliveries nor prioritizes interval work |
| Content FIFO | [content.js:1383–1405](https://github.com/RollyDorian/trading-bot/blob/9233cee9fedae02cd38b0b36a9a3eb65774b8937/extensions/mexc_ui_capture/content.js#L1383-L1405): `emitChain.then(async ...)`, extraction inside the task, then awaited `sendMessage` | No queue cap, age limit, priority, enqueue timestamp, or tick ordinal. Closures wait; they are not historical DOM observations |
| DOM extraction | [content.js:1244–1370](https://github.com/RollyDorian/trading-bot/blob/9233cee9fedae02cd38b0b36a9a3eb65774b8937/extensions/mexc_ui_capture/content.js#L1244-L1370) | Reads occur when the queued task runs. `monotonic_ms`, `received_at_local`, and `observed_at_local` are stamped near extraction completion, before send |
| Message receiver | [background.js:146–153](https://github.com/RollyDorian/trading-bot/blob/9233cee9fedae02cd38b0b36a9a3eb65774b8937/extensions/mexc_ui_capture/background.js#L146-L153) keeps asynchronous response channel open | Message acceptance is not the ACK boundary; handler must finish |
| Background FIFO | [background.js:47–97](https://github.com/RollyDorian/trading-bot/blob/9233cee9fedae02cd38b0b36a9a3eb65774b8937/extensions/mexc_ui_capture/background.js#L47-L97) serializes append calls | ACK includes committed sequence only after `appendSnapshot` resolves |
| Durable append | [durable.js:108–117,178–233](https://github.com/RollyDorian/trading-bot/blob/9233cee9fedae02cd38b0b36a9a3eb65774b8937/extensions/mexc_ui_capture/durable.js#L108-L233) | Each append first opens/reads/closes `meta`, then opens a second connection and a readwrite transaction spanning sessions/chunks/meta; reads session and chunk, serializes row, writes chunk/session/meta, waits for transaction completion, closes |
| Stop and export | [content.js:1374–1448](https://github.com/RollyDorian/trading-bot/blob/9233cee9fedae02cd38b0b36a9a3eb65774b8937/extensions/mexc_ui_capture/content.js#L1374-L1448), [background.js:58–117](https://github.com/RollyDorian/trading-bot/blob/9233cee9fedae02cd38b0b36a9a3eb65774b8937/extensions/mexc_ui_capture/background.js#L58-L117) | Stop clears capture flag and timer, disconnects observer, sends STOP independently of pending content work. START/STOP/export are outside appendChain |

For one normally operating content instance there is at most one outstanding
CAPTURE_SNAPSHOT request, because content awaits its ACK. Therefore a large
background snapshot queue is **not** the default explanation: backlog should
first accumulate in content. Multiple content instances can feed the global
background FIFO; no single-producer enforcement or sender/session binding is
shown by the receiver. Instrument producer count before blaming background
queue depth.

### 2.1 Timestamps are observations, not scheduling or disk timestamps

The scorer collects `received_at_local` from the exported rows
([v2_contract_gate.py:340](https://github.com/RollyDorian/trading-bot/blob/9233cee9fedae02cd38b0b36a9a3eb65774b8937/src/trading_bot/research/mexc_shadow/ui_capture/v2_contract_gate.py#L340)).
These are content extraction timestamps. Raw interarrival is not IndexedDB
commit interarrival. Slow persistence can indirectly stretch it by blocking the
next extraction, but a long raw gap is not itself proof of a slow transaction.

Queued timer work does not preserve the DOM at callback time. It eventually
reads the then-current DOM and stamps that later time. This is late observation,
not necessarily look-ahead in the current export. Backdating that result to the
expected timer deadline would create false causal history. Never do that.

Extraction performs multiple reads before stamping completion. An explicit
start/end bracket is needed to measure its duration. Retain completion as the
conservative arrival-available timestamp for a whole snapshot; do not relabel
the completion timestamp as callback time or exchange event time.

### 2.2 Stop, restart, and sequence hazards

Pending content tasks check the mutable `capturing` flag when they execute.
After Stop they can return before extraction without an error record. Already
in-flight append may race STOP. A quick Start can turn the flag on again while
old closures remain: they do not capture a session generation and can execute
against new state. These are code-level possibilities, not observed events in
this sample.

Durable append assigns sequence from stored `last_sequence`; content sends
`sequence: 0`. Thus continuous sequences cannot expose abandoned tick requests.
Append also replaces incoming `capture_id` with the globally active session ID,
without checking equality. START/STOP use several separate transactions outside
the append FIFO. STOP's read-modify-write can race append metadata updates;
export can read metadata and chunks at different states. A future design needs
session fencing and a final committed watermark, not merely a faster writer.

### 2.3 Storage amplification and capacity

The default chunk contains 250 JSON strings. Every append reads and rewrites
the growing chunk. A full chunk submits 1+2+...+250 = 31,375 row-equivalents to
chunk puts for 250 new rows, an average 125.5-fold logical payload rewrite.
This is a code-derived serialization/storage workload estimate, **not measured
physical disk write amplification**. Growth resets at every chunk; work is
O(N*K) for fixed K=250, not unbounded quadratic in the whole capture length.

Let `S` be extraction plus send/ACK turnaround service time per content task,
and `lambda_m` the actual mutation callback arrival rate. Sustained FIFO
stability needs `(2 + lambda_m) * E[S] < 1` with tail-latency headroom. The
persisted mutation rate is about 1.61/s but is censored by the same pipeline;
it is not a measurement of `lambda_m`. Even assuming that rate, mean service
would need to stay below about 277 ms. Interval-only traffic needs mean service
below 500 ms, also insufficient by itself to guarantee p95/p99 timing.

Awaiting transaction completion per row is compatible with 2 Hz **if measured
service times and offered traffic leave headroom**. It is not inherently too
slow, and neither IDB nor the ACK can be convicted from throughput alone.

At 2 Hz, 8–12 hours is 57,600–86,400 heartbeat rows, before mutations. With
250-row chunks that is 231–346 chunks. Long-run storage demand must use measured
bytes/row, quota, queue peaks, and service-time trends. Current popup export
accumulates all strings into `parts` then constructs a Blob
([popup.js:154–209](https://github.com/RollyDorian/trading-bot/blob/9233cee9fedae02cd38b0b36a9a3eb65774b8937/extensions/mexc_ui_capture/popup.js#L154-L209));
its memory footprint is another 12-hour risk, not a cause of this short run
unless export overlapped capture.

## 3. Ranked root-cause tree

Rank is diagnostic priority based on code exposure and ability to explain the
symptom, not a posterior probability. Several branches may operate together.

```text
Root: mandatory 500 ms opportunities are not represented by timely observations
  1. Content FIFO head-of-line blocking [mechanism PROVEN; attribution UNMEASURED]
     1a. Optional mutations occupy the same queue before mandatory heartbeat
     1b. Entire prior send + durable append ACK blocks the next extraction
     1c. Unbounded pending closures accumulate; Stop abandons their tail
  2. Renderer/timer opportunity loss [PLAUSIBLE; actual callback count UNKNOWN]
     2a. Hidden-page throttling / page freeze / host sleep
     2b. Synchronous DOM/layout work delays timers and message continuations
  3. Durable append latency amplifies branch 1 [work PROVEN; duration UNKNOWN]
     3a. Two connection open/close cycles and two transactions per row
     3b. Growing chunk read/rewrite, serialization, transaction contention
  4. Runtime transport / worker scheduling [POSSIBLE; weaker present evidence]
     4a. Worker cold start, scheduling delay, IPC/serialization or delayed ACK
     4b. Multiple producers create background backlog and session interference
  5. Stop/start/export races [hazard PROVEN; occurrence UNKNOWN]
     5a. No producer drain barrier or session-generation fence
     5b. Lifecycle commands outside append FIFO; metadata races
```

Branch 1 is application-level priority inversion in the sense that optional
work delays mandatory work; it is not an OS lock-priority claim. A finite FIFO
does not let later mutations overtake an earlier heartbeat, but enough earlier
work can make the heartbeat arbitrarily late. The queue has no bound, timeout,
or cancellation accounting. An indefinitely pending ACK stalls it indefinitely.

Branch 2b is credible because extraction repeatedly scans document elements and
calls computed-style/rectangle APIs, including header probe construction even
when its output is later deduplicated
([content.js:298–315](https://github.com/RollyDorian/trading-bot/blob/9233cee9fedae02cd38b0b36a9a3eb65774b8937/extensions/mexc_ui_capture/content.js#L298-L315),
[content.js:538–566](https://github.com/RollyDorian/trading-bot/blob/9233cee9fedae02cd38b0b36a9a3eb65774b8937/extensions/mexc_ui_capture/content.js#L538-L566),
[content.js:1128–1160](https://github.com/RollyDorian/trading-bot/blob/9233cee9fedae02cd38b0b36a9a3eb65774b8937/extensions/mexc_ui_capture/content.js#L1128-L1160)).
Layout reads can be expensive depending on page state; their cost is not known.
Body-wide observation also lets unrelated page changes enqueue work, even
though diagnostics must never inspect or export those mutation targets.

Chrome documents hidden chained-timer checks at one-second cadence and, under
additional conditions, once per minute after prolonged hiding. Visible pages
avoid that particular throttle but still have no hard scheduling deadline.
Those documented policies are hypotheses to test on the installed Chrome,
not proof that the 7.510-second tail has a particular throttle signature.
[Chrome timer policy](https://developer.chrome.com/blog/timer-throttling-in-chrome-88/),
[HTML timers](https://html.spec.whatwg.org/multipage/timers-and-user-prompts.html#timers).

Hidden, frozen, discarded, and host-asleep are distinct states. Visibility alone
cannot prove all of them; record lifecycle transitions where delivered and
treat missing tail records as censored. A frozen renderer cannot supply fresh
DOM observations by moving its timer to a different queue.
[Chrome page lifecycle](https://developer.chrome.com/docs/web-platform/page-lifecycle-api).

Normal MV3 inactivity shutdown is not the leading explanation for a stream
sending several messages per second: receiving extension events resets the
documented idle timer. Unexpected termination and cold starts remain possible;
worker globals do not survive restart. An asynchronous response channel does
not provide a 500 ms scheduling guarantee. Worker boot IDs are needed to test
this branch. Chrome alarms' documented minimum period is 30 seconds and is not
a 2 Hz replacement.
[Extension worker lifecycle](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle).

The existing heartbeat gate checks only `n_interval > 0` and
`n_unchanged_interval > 0`
([v2_contract_gate.py:534–535](https://github.com/RollyDorian/trading-bot/blob/9233cee9fedae02cd38b0b36a9a3eb65774b8937/src/trading_bot/research/mexc_shadow/ui_capture/v2_contract_gate.py#L534-L535)).
It proves an unchanged-value example, not cadence or callback conservation.
Python fixture tests and source-string assertions likewise do not exercise
real Chrome timers, renderer load, runtime IPC, or IDB latency. The unchanged
timing gates correctly caught the remaining failure; no test or gate is edited
in this milestone.

## 4. Minimum diagnostic design for one short real capture

Implement these hooks in a separate diagnostic-only milestone. Keep scheduling,
extraction, ACK behavior, and storage algorithms unchanged during that run.
Use fixed-schema numeric/enum metadata; no HTML, selectors, market prices,
mutation text/targets, account DOM, URLs of other tabs, or user identifiers.

### 4.1 Event identity and clocks

Allocate a local `producer_epoch` per content instance and a `worker_boot_id`
per worker start. Carry session ID plus a diagnostic `request_ordinal` assigned
at callback entry through send, append, and ACK. IDs are local correlation
tokens, not account or machine IDs. Record configured interval, extension/
catalog versions and browser version once, with diagnostic format version.

Record `performance.timeOrigin` once per realm and `performance.now()` stamps
for all stage measurements. Content and worker `performance.now()` values have
different zero points: never directly subtract them. Translate with timeOrigin
for correlation and validate using a bounded startup round trip; report clock
precision/uncertainty. Within-realm durations remain authoritative. Record a
paired wall-clock/monotonic anchor at start and end to detect clock jumps or
sleep anomalies. Do not adjust historical raw timestamps from diagnostics.
[High Resolution Time](https://www.w3.org/TR/hr-time-3/).

| Hook | Minimum retained fields |
| --- | --- |
| Timer registration | `timer_registered_mono`, interval=500 ms, initial visibility and last visibility transition |
| Actual timer callback entry, before `emit` | `callback_mono`, incrementing `interval_callback_ordinal`, ideal `expected_deadline_mono`, elapsed ideal slot ordinal, trigger |
| Mutation callback entry | request ordinal, callback timestamp, trigger; count callbacks only, do not read MutationRecords |
| Content enqueue/task entry | depth before enqueue (waiting excludes active), active flag, oldest waiting age, `extract_start_mono`; queue wait derived from callback timestamp |
| Extract end / send | `extract_end_mono`, `send_start_mono`, payload byte count; preserve existing raw timestamp placement |
| Background listener entry, before async work | `background_receive_mono`, worker boot ID, producer token, waiting depth and active flag |
| Append task entry / completion | `append_start_mono`, `append_end_mono` after transaction complete or failure, bounded outcome enum, committed sequence |
| Append substages | durations for active-session metadata lookup, second DB open, session read, chunk read, stringify/put submission, final transaction wait, close; chunk ordinal/occupancy and submitted byte count |
| ACK | `background_response_mono`, `content_ack_mono`, success/failure/timeout/unknown enum; no raw exception strings |
| Lifecycle | visibility state at callback and extraction; `visibilitychange` timestamp; delivered freeze/resume/pagehide/pageshow events; Stop request/ACK timestamps; outstanding counts by trigger |

“Scheduled callback timestamp” must be split into intended deadline and actual
callback entry. Set ideal deadline `t0 + n*500` from timer registration, never
from the previous delayed callback. Keep both callback ordinal and elapsed
ideal slot ordinal: browsers need not deliver one callback per elapsed ideal
slot. Slot jumps quantify missed opportunities, not automatically lost queued
requests. Never fabricate observations for skipped slots or run catch-up reads
stamped as old deadlines.

### 4.2 Counters, resource bounds, and durability of diagnostics

Maintain fixed counters by trigger for callback, enqueued, extracted, sent,
received, append-started, committed, ACKed, failed, abandoned-before-extract,
and currently outstanding. Increment callback counters immediately, not inside
emitChain. Maintain depth/oldest-wait high-water marks for both queues. Record
producer count and lifecycle-command counts so a hidden second producer can be
identified without recording tab URLs.

For a maximum 12-minute diagnostic run, retain at most 2,048 detailed interval
requests, 512 mutation samples (first 128, then every 16th up to the cap), 256
slow-stage/error examples, and 128 lifecycle transitions. All events still
update fixed counters and histograms after detailed caps. Use 16 fixed duration
buckets per stage/trigger/visibility, with overflow bucket; retain min/max/sum/
count. IDs are <=64 characters, enums <=32, no arbitrary arrays; total diagnostic
budget is 16 MiB across contexts and export. Reserve space for final summaries;
record `diagnostic_truncated` and exact suppressed counts. These are diagnostic
capacity choices, not research admission thresholds.

Use bounded in-memory buffers with a separate append-only local diagnostic
sidecar flushed at most once per 5 seconds, outside the observation FIFO.
Piggyback content deltas on existing messages where possible; a capped status
flush may transfer callback counters during stalls but must not enqueue another
observation. Account for diagnostic flush bytes/time/queue wait because it can
contend with IDB and affect worker lifetime. No per-tick console logging or
extra database transaction for each timing stamp. Reserve final flush/summary
at Stop without draining or changing the original observation policy in this
diagnostic run; count abandoned original tasks as they settle.

Append completion cannot be stamped into the same transaction after that
transaction has completed. Store completion records by request ID in a later
sidecar batch, or return them in the ACK and checkpoint afterward. Missing
completion evidence is `UNKNOWN`, not a zero duration or proof of commit loss.
A crash may lose the uncheckpointed diagnostic tail; store checkpoint
watermarks and never claim exact reconciliation across that tail. Diagnostic
buffer saturation drops diagnostics with counters, never market rows.

### 4.3 Analysis that discriminates causes

Compute stage distributions separately for interval/mutation and visible/hidden;
include incomplete requests as censored. Do not infer callback performance only
from committed survivors. Report:

- callback lateness versus ideal deadlines and actual callback interarrivals;
- content wait = extraction start minus callback entry;
- extraction duration = end minus start;
- background wait = append start minus listener receipt;
- append duration and its substage distribution by chunk occupancy;
- content round trip = ACK minus send, and background handling duration;
  their difference estimates combined transport/dispatch/ACK overhead, not
  one-way IPC latency;
- each slow raw gap's preceding ACK, callback, content wait, extraction,
  background wait, append, visibility and worker epoch evidence.

Percentiles of stages cannot be added to reconstruct an end-to-end percentile.
Use aligned individual requests for attribution and label overlapping causes.

| Discriminating observation | Supported explanation / limit |
| --- | --- |
| Few actual callbacks, large deadline lag, small queues | Timer/renderer/lifecycle branch; hidden correlation supports throttling but does not prove policy or exclude host sleep |
| Near-ideal callbacks, increasing content depth/oldest age; interval waits behind mutations | FIFO priority inversion and backlog; Stop counts explain any abandoned tail |
| Callback gaps overlap long extraction, with low content wait | Synchronous extraction or renderer pressure; if own extraction is short, other page tasks remain a cause |
| ACK duration tracks append duration; content wait rises | Storage-mediated backpressure |
| Append duration rises with chunk occupancy and resets on rollover | Growing chunk cost supported; control for visibility and payload size, do not claim physical I/O without measurement |
| Small append/content costs, large send/ACK residual or new worker boot | Messaging/scheduling or restart branch; residual alone cannot separate outbound from return path |
| Background depth >0 repeatedly with multiple producer tokens | Multi-producer contention; inspect session fencing, not private UI |
| Callback/enqueue exceeds extracted at Stop | Pre-extraction abandonment supported; committed sequence continuity remains irrelevant to those missing requests |

On clean termination reconcile, by trigger and epoch:
`enqueued = extracted + abandoned + still_waiting`,
`sent = ACKed + failed_or_unknown + still_in_flight`, and background received
against committed/failed/waiting. A lost ACK may coexist with a committed row;
join by request ID. Do not subtract ideal opportunity count from persisted
sequence and call the difference dropped writes.

### 4.4 One bounded operator run

After instrumentation review, use one producer and the same 500 ms configuration
for 12 minutes: 2 minutes visible, 6 continuously hidden, then 4 visible. This
is a diagnostic experiment, not the final long identification corpus. Record
actual visibility events and phase times; do not exclude the hidden section to
manufacture a timing pass. Keep normal browser settings, no capture DevTools or
worker debugger attached, no reload, export, or repeated popup polling during
capture. Record browser version and operator-attested sleep/debugger state.
Stop once, obtain final accounting, and export locally.

This one run can distinguish callback starvation, queue waiting, extraction,
append cost, and worker changes if they recur. It cannot prove which historical
cause produced the earlier sample or certify 12-hour reliability. If visible
and hidden phases differ, later remediation validation must state the supported
visibility/host operating conditions. If a freeze/crash prevents complete
evidence, report `DIAGNOSTICS_INCONCLUSIVE` and the missing boundary.

## 5. Candidate architectures for a later implementation

No candidate is implemented or benchmarked here. All retain fresh observation
of unchanged values and the exact v2 admission bars. They can be combined after
diagnostics; none can force a suspended renderer to observe market DOM.

| Candidate | Causal time and sequence | Crash / worker suspension loss | Deterministic export | 500 ms grid and 8–12h assessment |
| --- | --- | --- | --- | --- |
| A. Heartbeat-priority scheduler, mutation coalescing | At most one pending mutation intent; choose ready heartbeat ahead of unsampled optional work. Sequence assigned when actual extraction completes, never at intended deadline. No reordering of already-observed rows | If still awaiting every durable ACK, an in-flight mutation remains non-preemptible. Pending heartbeat intent is not a saved historical observation. Existing per-row commit prefix survives ordinary worker restart; outstanding task accounting still needed | Serialized append can retain existing ordering, but add lifecycle barriers/fencing | Removes optional FIFO floods; cannot fix a slow single extraction/append or timer throttle. Bounded pending work required; missed heartbeat deadlines must be visible, not coalesced into fake historical rows |
| B. Observation/persistence decoupling, bounded queue | Fresh complete snapshot at callback execution, immutable timestamp/payload and producer observation sequence before enqueue. Background writes later in that same order | Both content and worker RAM are volatile. ACK receipt differs from durable ACK. Retain unACKed immutable rows, retry by idempotent session/producer/sequence key; after page crash the uncommitted bounded tail is lost and declared | Atomic row/sequence high-water commit; ordered export of committed prefix; retry cannot duplicate or assign a new sequence | Isolates callback cadence from temporary IDB latency. Cap count AND bytes AND oldest age; fail closed at capacity, never silently overwrite mandatory rows. Needs sustained writer capacity and recovery tests for 12h |
| C. Batched IndexedDB persistence | Batch immutable observed rows in sequence, preserving individual receipt times. One transaction commits rows and watermark atomically; batch time never replaces observation time | RAM batch before commit is at risk. ACK only after transaction completion; lost ACK requires idempotent retry. Explicit batch time/count/byte bounds and partial-tail recovery | Immutable persisted batches or individual keyed rows, deterministic reconstruction into export chunks; no rewriting already committed history | Amortizes opens/transactions and growing chunk work. A retained connection needs restart/versionchange recovery. Batching alone does nothing if content still waits for a commit before producing the next row; choose batch timeout to avoid that deadlock. Quota and batch tail must be measured |
| D. Interval-only raw observations; MutationObserver only marks dirty state | Each actual interval rereads all required fields even when not dirty. Dirty notification contains no DOM payload. No mutation rows; optional initial manual row has explicit timestamp/sequence | Lowest offered raw rate; with per-row ACK still exposed to persistence stalls. Combined bounded decoupling/batching has the same declared tail-loss model as B/C | Simplest single-producer order; still requires durable prefix, stop barrier and bounded export memory | Best first simplification if mutation load dominates; 57,600–86,400 heartbeat rows in 8–12h. Dirty must never gate heartbeat or authorize copying cached fields. Short transients between ticks are not observed, but v2 permits optional mutation rows and fixes a 500 ms analysis grid |

For B/C, a proposed bounded capacity is selected from diagnostic row size and
writer outage measurements in a separate reviewed design, not from market
outcomes. Queue caps alone do not make loss disappear: record oldest queued age,
overflow, discarded optional intents, and last durable observation sequence.
Never replace old observed rows with current market state under the same ID.

For every candidate, graceful Stop should later become: stop producers, fence
session, drain already-observed payloads with bounded timeout, commit terminal
metadata at a watermark, then ACK Stop. Unobserved timer intents are counted as
missed, not extracted retroactively. Start must use a new epoch; stale senders
must be rejected instead of relabelled to the active session. Export should
read a finalized prefix or a captured watermark and reconstruct in sequence,
with bounded memory rather than an all-session in-memory Blob where size is
unsafe. These are proposals, not changes authorized by this review.

An IDB transaction-complete ACK establishes the application's committed prefix;
it is not automatically an fsync guarantee against OS crash or power loss.
The current transaction requests no explicit durability hint. Any future strict
versus default durability choice needs its own latency and crash tests; batching
must not be advertised as zero-loss RAM storage.
[IndexedDB durability](https://www.w3.org/TR/IndexedDB/#transaction-durability).

Conditional preference: investigate D (or A if mutation observations must remain)
first when callback counts are healthy and mutation queue wait dominates. Add B
only if durable ACKs block otherwise timely observations; add C if measured
append cost warrants it. If callbacks themselves disappear while hidden, none
of these queue changes establishes background 2 Hz capability. Do not promise
8–12h capture until sustained visible operation, lifecycle recovery, bounded
storage/export, and a short real recapture have been demonstrated.

## 6. Protocol compatibility and handoff

**Yes: protocol v2.0.0 can remain unchanged when only implementation and bounded
diagnostics change**, provided actual observation/receipt semantics are retained.
The 500 ms causal grid, 1000 ms as-of bound, 2000 ms gap rules, p95 raw
interarrival <=1000 ms and >=99% <=2000 ms remain exact. Catalog v1.2, locale
provenance, numeric-scale audit and simultaneous bid/ask/last/mark/index coverage
remain required. Neither a faster commit nor more mutation rows is a substitute.

Persistence decoupling is compatible because `received_at_local` already means
arrival availability in content, not disk commit. Keep it at actual completed
observation, preserve monotonic/session ordering, and retain append timestamps
only as diagnostics. Substituting expected deadlines, backfilled samples,
commit timestamps, cached snapshots, or excluding slow periods to pass would
change the data contract and is not allowed under unchanged v2.

The next deliverable should be an instrumentation-only PR implementing section
4 with deterministic slow-ACK/mutation-burst/Stop accounting tests and a short
Chrome run. Its report must rank measured stage delays, reconcile request
counts, disclose diagnostic overhead and censoring, and recommend a candidate
architecture from evidence. Later scheduler/storage changes need real-browser
tests for bounded backlog, restart/lost ACK, session fencing, export stability,
and long-duration resource use. Unit tests alone do not certify cadence.

This review changes only this document. Existing project instructions and v1/v2
protocol files were reviewed and retained; implementation and long capture
remain pending lead review.

`STOP_FOR_LEAD_REVIEW`
