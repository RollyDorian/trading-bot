# Timestamp and sequence quality invariants

Quality report schema 5 extends schema 4. It validates two distinct clocks instead of
comparing them as one global timeline, classifies per-stream sequence metadata
explicitly, and applies a bounded exchange-clock tolerance at manifest boundaries.

## Valid slice requirements

For admission status `pass`, a dataset must satisfy all of the following:

1. `manifest.json` has a supported schema, matches the dataset directory identity, and
   every expected artifact checksum matches.
2. The dataset contains at least one event.
3. Every `received_at` is timezone-aware UTC, lies inside the manifest half-open interval
   `[start_utc, end_utc)`, and is globally nondecreasing in exported row order.
4. `exchange_at` may be absent. When present, it must be timezone-aware UTC and be
   nondecreasing within its `(source, topic)` stream. Exchange clocks from different
   topics are not compared with one another or with receipt clocks because topic
   callbacks have different transport latency.
5. When present, `exchange_at` must lie inside the manifest interval, except that schema
   5 allows a bounded boundary tolerance (default 5 seconds). Timestamps in
   `[start_utc - tolerance, start_utc)` or `[end_utc, end_utc + tolerance)` are counted
   as `exchange_timestamp_boundary_excursions` and remain pass-eligible. Timestamps
   outside that widened window are rejected. Grossly out-of-range values (for example a
   stale 2024 fixture timestamp inside a 2026 window) still fail.
6. Every `(source, topic)` stream is classified in `sequence_availability`:
   - `present`: every row has an integer `sequence`;
   - `absent`: no row has a sequence (the audited Hibachi public orderbook limitation;
     continuity is unprovable and sequences are never invented);
   - `partial`: mixed presence (metadata inconsistency; status becomes `warning`).
   Admission copies `sequence_availability` into per-dataset admission evidence and
   surfaces `absent` streams in top-level `evidence_limitations`. `absent` preserved
   into admission never means proven continuity.
7. When sequence numbers are present, each `(source, topic)` stream advances by exactly
   one. Missing sequence metadata is reported as unavailable; it is never invented.
8. Exact normalized duplicate events are absent.
9. Trade payloads contain parseable positive prices. Captured Hibachi public trades
    use the known envelope `data.trade.{price,quantity,...}`; quality and candle
    aggregation inspect root, `data`, and nested `data.trade` only. Unknown nesting is
    not recursively accepted, and nonpositive or nonfinite prices remain invalid.
10. Receipt-time gaps do not exceed the configured warning threshold, and configured
    price discontinuity checks do not produce warnings. Admission requires `pass`, so
    warnings remain ineligible without changing any threshold.

Coverage and gap calculations use `received_at`, matching the database export filter
(`received_at ∈ [start_utc, end_utc)`). Exchange coverage is reported separately.
Range, receipt-order, per-stream exchange-order, boundary-excursion, and
sequence-availability fields are reported separately.

## Exchange boundary tolerance rationale

Dataset export filters rows by receipt time, but schema 4 required `exchange_at` inside
the same manifest interval with no slack. Transport latency means a row received just
after `start_utc` can legitimately carry `exchange_at` slightly before `start_utc`, and
small clock skew can place `exchange_at` at or just after `end_utc`. Schema 5 corrects
this clock-domain mismatch with a configurable tolerance (CLI and API default 5 seconds;
`0` restores strict schema-4 behavior). Receipt-timestamp range checks remain strict.

## Collector audit

`MarketCollector` records `received_at = datetime.now(UTC)` immediately when a callback is
handled. It preserves the raw payload, extracts an optional exchange timestamp from known
top-level or nested fields, preserves an optional sequence number, and computes nonnegative
latency when an exchange timestamp exists. Events without exchange timestamps retain
`exchange_at = null`; receipt time is not copied into that field.

Orderbook sequence handling is fail-closed when the feed supplies sequence metadata:

- a snapshot establishes the session baseline;
- an update before a snapshot records `DESYNC` and stops the collector;
- a gap, duplicate, or regression records `DESYNC` and stops the collector;
- the offending raw event remains append-only evidence;
- reconnect creates a new collector, resets sequence state, and requires a new snapshot.

The audited Hibachi orderbook payloads contained no sequence values. Schema 5 records this
as `sequence_availability: absent` for those streams. Continuity cannot be proven for such
sessions; the collector does not fabricate a sequence. Explicit classification replaces the
previous Milestone 4 blocker that required resolving absent metadata through fabrication.

## Schema 3 diagnosis

Schema 3 selected `exchange_at` when present and otherwise `received_at`, then compared
that mixed series globally. Actual orderbook messages had exchange timestamps while mark,
spot, funding, and quote topics did not. Normal callback latency made an orderbook exchange
timestamp slightly earlier than the preceding topic's receipt timestamp, producing false
global regressions even though orderbook exchange timestamps were monotonic within their
own stream.

Schema 4 corrected the clock-domain comparison while retaining strict range and
per-stream ordering rejection. Schema 5 adds explicit sequence-availability classification
and bounded exchange boundary tolerance without weakening rejection of far-out-of-range
timestamps such as the stale 2024 integration-fixture timestamp found in the local 2026
slice.

## Replay ordering by event type

Research replay must not invent a mixed global clock from `exchange_at`. Quality may still
reject exchange-clock regressions within a stream; replay and reconstruction use the
contracts below instead.

| Event type | Ordering contract |
| --- | --- |
| `orderbook`, sequence absent | `received_at`, then `raw_event_id` / `id` |
| `orderbook`, sequence present | `exchange_sequence` (fallback `sequence`), then `received_at`, then `raw_event_id` / `id` |
| `trades`, `mark_price`, `spot_price`, `ask_bid_price`, `funding_rate_estimation`, and all other topics | `received_at`, then `raw_event_id` / `id` |

Rules:

1. `exchange_at` is metadata only. It must never reorder orderbook delta updates.
2. Global mixed-topic replay (`replay_parquet`, `order_events_for_replay`) uses receipt
   order for every topic. This preserves known exchange-clock regressions where a later
   receipt carries an earlier `exchange_at` (for example raw_event_id `1126466` before
   `1126478` with a 310 ms exchange-clock regression).
3. Orderbook reconstruction (`orderbook_replay_rows`) applies the orderbook-specific
   contract above. Mixed present/absent sequence metadata within one orderbook stream
   fails closed.
4. Candle aggregation in dataset export may continue to bucket by
   `exchange_at or received_at`; that path is separate from replay ordering.
