# Normalized-data core

## Current contract

The collector remains RAW-only. The normalizer is a separate PostgreSQL process
and is not enabled by Compose, a timer, or collector startup.

Supported RAW event types:

- `orderbook`, with only `Snapshot` and `Update`;
- `ask_bid_price`;
- `mark_price`;
- `spot_price`;
- `funding_rate_estimation`.

Captured shape checks found the exact field names used by fixtures under
`tests/fixtures/hibachi`. The zero-quantity fixture is an explicit boundary
mutation of the confirmed `price`/`quantity` contract; the bounded production
sample did not contain a zero-quantity level. Unknown fields, types, event
types, message types, decimals, and timestamps fail with bounded error codes.

## Tables and retention

The migration creates empty `normalized` and `pipeline` schemas. It does not
alter or index `market_events`.

- Typed tables repeat the required provenance and enforce one row per
  `(raw_event_id, pipeline_version)`.
- `pipeline.normalization_errors` records only fixed codes and bounded safe
  detail, never a payload or raw database exception.
- `pipeline.checkpoints` advances in the same transaction as typed inserts and
  error rows.
- `raw_event_id` is deliberately not a foreign key. Future verified RAW
  archive/retention is therefore not blocked; no retention is implemented here.

## Bounded execution

```text
hibachi-bot normalize \
  --consumer normalized-backfill-v1 \
  --capacity-path <same-backing-filesystem> \
  --batch-size 100 \
  --max-batches 1
```

The default batch is 100, the hard maximum is 1,000, and one invocation runs
one batch unless the operator explicitly requests up to 100. A PostgreSQL
transaction advisory lock rejects concurrent ownership of one consumer.
Transient database retries are bounded. A parser error advances the checkpoint
atomically and is not retried forever.

`--follow` creates a separate live consumer at the current RAW high-water mark,
then tails bounded batches. This gives fresh data priority while a separate
backfill consumer progresses from zero. Overlap is safe because typed/error
unique constraints use `ON CONFLICT DO NOTHING`.

Before every batch, the process verifies PostgreSQL through the batch
transaction and checks process RSS plus free space on the explicitly supplied
capacity path. Defaults:

- pause below 4 GiB free; hard stop below 3 GiB;
- pause at 128 MiB RSS; hard stop at 160 MiB;
- estimate 4 KiB output per input row before admitting a batch.

Unknown resource state fails closed. The capacity path must be on the same
backing filesystem as the intended PostgreSQL growth; otherwise the disk gate
is not meaningful.

## Orderbook reconstruction

Snapshot replaces the state. Update sets a non-zero level and removes a
zero-quantity level. Empty Update is a no-op. A connection boundary or explicit
disconnect invalidates state until Snapshot. Crossed/empty books and verified
sequence gaps invalidate the chain.

Local sequence does not prove exchange continuity. A chain without exchange
sequence remains `valid_sequence_unverified`; legacy data is
`valid_best_effort_legacy`. Best-quote comparison uses a bounded availability
time tolerance and never requires exact row grouping.

## Capacity pilot

`scripts/normalization_pilot.py` accepts only the guarded test database target
and refuses empty or over-limit samples. It emits bounded aggregate JSON or a
short summary; it never prints payloads or connection data.

Measured isolated PostgreSQL 16 pilot:

| Metric | Result |
|---|---:|
| RAW rows / time span | 62,931 / 3,810.816 s |
| Typed rows / errors | 62,931 / 0 |
| Runtime / throughput | 102.123 s / 616.22 rows/s |
| Peak process RSS | 104,542,208 bytes |
| Heap / index growth | 15,245,312 / 6,479,872 bytes |
| WAL generated | 34,929,624 bytes |
| Orderbook events / final state | 12,587 / `valid_sequence_unverified` |
| Linear daily / seven-day estimate | 492,560,133 / 3,447,920,931 bytes |

The estimate is based on one approximately 2.5% contiguous session and assumes
the observed rate and storage ratio continue. It is not a retention guarantee.
At the observed constrained-host free space, projected headroom above the 3 GiB
hard floor was only 2.62 days. Full PostgreSQL backfill and live activation are
therefore blocked pending capacity or a reviewed streaming-Parquet redesign.

No production migration, backfill, REST polling, retention, or automatic
normalizer schedule is part of this work.
