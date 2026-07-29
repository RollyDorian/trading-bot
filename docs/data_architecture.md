# COLLECT data architecture

This document is the target contract. Implementation is split into independently
reviewable phases; documenting a channel does not mean it is already collected.

## Layer boundaries

1. **RAW PostgreSQL** is the authoritative append-only history of received public
   Hibachi messages. Collectors write only RAW.
2. **NORMALIZED PostgreSQL** contains reproducible, typed, channel-specific rows
   derived from RAW by a separate idempotent normalizer.
3. **RESEARCH Parquet** contains reconstructed books, as-of market-state frames,
   features, and labels. It is never part of the collector write path.

Decision-time joins use `available_at`, equal to RAW `received_at`. Offline state
construction uses backward as-of joins on availability time; exchange time must
never reveal information before receipt.

## RAW envelope

`market_events.id` is `raw_event_id`. Existing rows remain RAW schema version 1
and their payloads are never rewritten. RAW schema version 2 adds nullable
`connection_id`, `local_sequence`, and `exchange_sequence`, plus a non-null
envelope `schema_version`.

- `connection_id` is opaque and changes for each collector connection/session.
- `local_sequence` is monotonic receipt order within that connection.
- `exchange_sequence` is populated only when Hibachi supplies it.
- Legacy `sequence` remains for compatibility and is not reinterpreted.
- Payload JSON is stored unchanged; absent channels and fields are never invented.

The intended public channel inventory is orderbook snapshots/updates, trades,
best bid/ask, mark price, spot price, funding-rate estimates, optional klines,
open interest, and instrument metadata. REST snapshots/trades are optional
verification or backfill sources. Only captured, sanitized payload fixtures from
the pinned official SDK/API may define a parser.

Actual funding history must not use the SDK's private account endpoint. It remains
unimplemented until a documented unauthenticated source exists.

## NORMALIZED contract

The implemented core is deliberately limited to payloads confirmed in bounded
RAW samples: `normalized.best_quotes`, `normalized.reference_prices`,
`normalized.funding_estimates`, and `normalized.orderbook_events`. Trades,
actual funding, open interest, exchange info, and klines remain unsupported
until their isolated ingestion source and sanitized fixtures exist.

Orderbook events use one compact validated JSONB change set per RAW event. The
normalizer does not materialize one PostgreSQL row per level.

Every typed row identifies its `raw_event_id` and normalizer version. Unique
constraints make retry idempotent. A batch, validation errors, and its checkpoint
commit atomically. Poison events produce bounded errors and advance the checkpoint;
they must not retry forever.

Foreign keys must not permanently prevent an explicitly approved, verified
archive-and-retention operation. Deleting RAW is never automatic. Any future
retention implementation must define typed-row handling, archive verification,
recovery limitations, and human approval.

## Orderbook policy

Book reconstruction is a state machine partitioned by symbol and connection:

1. Snapshot replaces all levels and marks state valid.
2. Update changes a non-zero level or deletes a zero-quantity level.
3. Reconnect invalidates state until another snapshot.
4. Unknown message types are validation errors.
5. A supplied exchange sequence gap/regression invalidates the chain.

Local receipt order alone never proves exchange continuity. Chains without a
verified exchange sequence are `sequence_unverified` or `best_effort`, never
`exact`. Full legacy `orderbook_levels` backfill is prohibited until a bounded
capacity pilot demonstrates safe disk, WAL, duration, and memory usage.

## Normalizer safety

Backfill and live tail are separate from collection. The normalizer must use a
small default batch, a hard maximum, resumable checkpoints, bounded errors, and
disk/WAL/RAM preflight thresholds. Live-tail freshness has priority over bulk
history. One logical consumer owns a checkpoint; concurrent ownership fails
closed.

A 1–5% copied-RAW pilot must measure source rows, produced rows, heap/index
growth, WAL estimate, peak RSS, and duration before full backfill approval.

The first contiguous pilot copied 62,931 RAW rows from the latest snapshot
boundary into isolated PostgreSQL 16. It demonstrated parser and reconstruction
correctness, but estimated roughly 493 MB/day of normalized heap plus indexes.
With the observed constrained-host free space, that is only about 2.6 days above
the existing 3 GiB hard floor. Therefore production migration, live tail, and
historical backfill remain unapproved. Capacity must be added or a compact
streaming Parquet normalized layer must be reviewed before activation.

## REST isolation

REST polling is a separate failure domain from WebSocket collection. REST
timeouts, rate limits, parsing failures, or exhausted retries must record bounded
operational evidence and pause only the affected poller. They must not terminate
the WebSocket collector.

Each request owns its response state. Pollers use explicit timeouts, bounded
exponential backoff with jitter, and documented minimum intervals/rate-limit
handling. Shared mutable response buffers are prohibited.

## Delivery sequence

1. Documentation contract.
2. RAW v2 envelope, low-lock migration, and export compatibility.
3. Fixture-driven normalized schemas and strict parsers.
4. Bounded normalizer and capacity-pilot harness.
5. Isolated REST polling.

No phase enables PAPER/LIVE, order placement, private account access, automatic
retention, or full production backfill.
