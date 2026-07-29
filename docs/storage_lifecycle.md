# COLLECT storage lifecycle

## Decision

The constrained deployment uses **RAW PostgreSQL plus direct normalized
Parquet**. PostgreSQL remains the authoritative short hot buffer; verified
external Parquet is the historical system of record. Persistent normalized
PostgreSQL history is disabled.

| Design | Disk/WAL | Recovery and use | Decision |
|---|---|---|---|
| A. RAW and normalized PostgreSQL hot windows | Highest: the pilot measured about 470 MiB/day normalized heap/index and additional WAL | Convenient SQL, but two retention paths and the greatest collector risk | Rejected on 15 GiB |
| B. RAW hot PostgreSQL, normalized event Parquet | One RAW write plus bounded read/export; compressed normalized history has no PostgreSQL WAL | RAW archive can reproduce parser versions; typed Parquet is immediately queryable | Selected |
| C. RAW PostgreSQL, market-state Parquet only | Lowest storage | Rebuilding channel-level history requires replaying RAW; weaker parser auditability | Deferred optimization |

Design B survives external-storage outages by pausing retention and never makes
archive availability part of the collector write path.

## Archive contract

`hibachi-archive export-day` reads one closed UTC day in bounded primary-key
batches and writes Zstandard-compressed Parquet:

```text
<dataset>/date=YYYY-MM-DD/symbol=<slug>/part-<first-id>-<last-id>.parquet
```

Datasets are `raw`, `best_quotes`, `reference_prices`,
`funding_estimates`, `orderbook_events`, and `normalization_errors`. RAW keeps
the complete JSONB value plus v1/v2 envelope. Typed datasets keep provenance,
pipeline version, and quality without copying RAW JSON.

Each chunk is published before the external checkpoint advances. Deterministic
keys make interruption retry idempotent. Local files use atomic replacement.
S3-compatible destinations upload a temporary object, copy to the final key,
and publish the manifest last. There is never one file per database row.

The manifest records interval, symbol, RAW ID range/count/digest, object
checksums, sizes, schemas, row counts, destination kind, pipeline version,
creation time, and `verified` status. Verification reads every object back and
checks SHA-256, Parquet schema/metadata, row count, RAW-ID endpoints, and ordered
RAW-ID digest. Corrupt, truncated, missing, stale, or incompatible state fails
closed.

The first oldest day starts at RAW ID zero. Each later day must receive the
previous verified manifest's maximum RAW ID through `--initial-raw-event-id`.
This keeps daily reads on the primary-key path and avoids rescanning historical
JSONB. Skipping a preceding verified manifest makes the retention gap check fail.

Filesystem storage is development-only. Production retention requires
S3-compatible external storage. Runtime names are:

- `ARCHIVE_S3_BUCKET`
- `ARCHIVE_S3_PREFIX`
- `ARCHIVE_S3_ENDPOINT`
- `ARCHIVE_S3_ACCESS_KEY`
- `ARCHIVE_S3_SECRET_KEY`

Values remain in the existing protected runtime environment and are never
printed. The only archive copy must not remain on the VPS.

## Resource and capacity gates

Export defaults to 5,000 rows and has a 10,000-row hard maximum. Before every
batch it uses the existing 4 GiB pause / 3 GiB stop and 128 MiB pause /
160 MiB stop thresholds. PostgreSQL queries are read-only with a five-second
statement timeout. Any uncertain resource or database state stops export.

The capacity command reports measured inputs separately from extrapolations:

```text
hibachi-archive capacity \
  --disk-free-mib <measured> \
  --raw-mib-day <measured> \
  --normalized-mib-day <measured-or-pilot> \
  --parquet-mib-day <measured> \
  --wal-mib-day <measured> \
  --measured-days <days> \
  --raw-hot-days 2 \
  --normalized-hot-days 0
```

The provisional target is two closed RAW days and zero normalized PostgreSQL
days. This is a maximum, not permission: the planner may reduce or block it.
The 3 GiB reserve is immutable. Actual RAW and Parquet daily rates require a
bounded archive canary before any retention approval.

## Retention

No automatic deletion, timer, or production delete command is enabled.
`hibachi-archive retention-plan` is dry-run only. A closed daily range is
eligible only when its external manifest is verified, its destination is
S3-compatible, RAW count and identity coverage match, adjacent selected UTC
intervals have no gap, and the configured hot window remains.

The library contains a separately guarded bounded executor for synthetic
integration proof. It requires an exact confirmation token, writes an external
audit record before and after a transaction, deletes at most 1,000 locked rows,
and is not exposed through the production CLI. Future production activation
requires separate review and approval. Large DELETE transactions, cascade
deletes, partition drops, VACUUM, and automatic retention are prohibited.

## Existing 2.5M RAW rows

Do not copy or backfill the full table. After external storage and the RAM
upgrade are independently approved:

1. measure RAW/day and WAL/day read-only;
2. archive the oldest complete UTC day in batches;
3. verify every object and manifest from external storage;
4. run a dry-run retention plan;
5. perform an isolated bounded canary on synthetic/test data;
6. request separate approval before any production deletion.

Legacy v1 rows retain null session/sequence fields and schema version 1.
Missing trades, open interest, or funding are never synthesized. RAW Parquet,
not the typed derivative, is the recovery source.

## Normalized rollout gate

Direct normalized Parquet may run only after all conditions pass:

- host RAM is at least 2 GiB and stable;
- durable external storage and credentials are operational;
- interrupted upload/resume and corrupt-object rejection pass there;
- a protected database backup and restore validation are current;
- dry-run retention preserves the requested hot window and 3 GiB reserve;
- measured RAW + Parquet + WAL rates project at least seven days to pause;
- a closed-day canary stays below RSS/disk limits and does not affect collection;
- the operator explicitly approves scheduling.

The merged normalized PostgreSQL migration, backfill, and live tail remain
disabled. If the Parquet canary fails, add capacity; do not lower thresholds.

## Rollback

Archive export has no collector-side state and can be stopped safely. An
unpublished checkpoint or manifest is not deletion evidence. Preserve uploaded
objects for diagnosis and rerun the same day idempotently. Never delete verified
archives or RAW to roll back software.
