# Timestamp and sequence quality invariants

Quality report schema 4 validates two distinct clocks instead of comparing them as one
global timeline.

## Valid slice requirements

For admission status `pass`, a dataset must satisfy all of the following:

1. `manifest.json` has a supported schema, matches the dataset directory identity, and
   every expected artifact checksum matches.
2. The dataset contains at least one event.
3. Every `received_at` is timezone-aware UTC, lies inside the manifest half-open interval
   `[start_utc, end_utc)`, and is globally nondecreasing in exported row order.
4. `exchange_at` may be absent. When present, it must be timezone-aware UTC, lie inside
   the same manifest interval, and be nondecreasing within its `(source, topic)` stream.
   Exchange clocks from different topics are not compared with one another or with receipt
   clocks because topic callbacks have different transport latency.
5. Exact normalized duplicate events are absent.
6. When sequence numbers are present, each `(source, topic)` stream advances by exactly
   one. Missing sequence metadata is reported as unavailable; it is never invented.
7. Trade payloads contain parseable positive prices.
8. Receipt-time gaps do not exceed the configured warning threshold, and configured price
   discontinuity checks do not produce warnings. Admission requires `pass`, so warnings
   remain ineligible without changing any threshold.

Coverage and gap calculations use `received_at`, matching the database export filter.
Exchange coverage is reported separately. Range, receipt-order, and per-stream
exchange-order violation counts are also reported separately.

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

The audited Hibachi orderbook payloads contained no sequence values. Therefore continuity
cannot be proven for those sessions; the collector does not fabricate a sequence. This is
an explicit evidence limitation that must be resolved through supported public feed
metadata before Milestone 4 can complete.

## Schema 3 diagnosis

Schema 3 selected `exchange_at` when present and otherwise `received_at`, then compared
that mixed series globally. Actual orderbook messages had exchange timestamps while mark,
spot, funding, and quote topics did not. Normal callback latency made an orderbook exchange
timestamp slightly earlier than the preceding topic's receipt timestamp, producing false
global regressions even though orderbook exchange timestamps were monotonic within their
own stream.

Schema 4 corrects the clock-domain comparison while retaining strict range and
per-stream ordering rejection. It does not accept the stale 2024 integration-fixture
timestamp found in the local 2026 slice.
