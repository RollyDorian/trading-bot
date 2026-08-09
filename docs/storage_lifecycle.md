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

Server-local filesystem storage is development/canary-only. Durable external
storage can be S3-compatible or an owner-protected operator PC. S3 runtime
names are:

- `ARCHIVE_S3_BUCKET`
- `ARCHIVE_S3_PREFIX`
- `ARCHIVE_S3_ENDPOINT`
- `ARCHIVE_S3_ACCESS_KEY`
- `ARCHIVE_S3_SECRET_KEY`

Values remain in the existing protected runtime environment and are never
printed. The only archive copy must not remain on the VPS.

### Direct PC archive

`pc-export-day` runs on the operator PC. It invokes the system OpenSSH client
with a reviewed alias, executes a fixed read-only `COPY (SELECT ...)` through
the existing private deployment channel, and writes bounded Zstandard Parquet
directly to the PC. PostgreSQL remains unpublished. Database credentials stay
inside the existing server-local container environment and are neither copied
to the PC nor printed. The remote side creates no archive file.

```powershell
hibachi-archive pc-export-day `
  --start 2026-07-21T00:00:00+00:00 `
  --end 2026-07-22T00:00:00+00:00 `
  --symbol ETH/USDT-P `
  --root <OWNER_ONLY_PC_ARCHIVE_ROOT> `
  --work-dir <OWNER_ONLY_PC_WORK_DIR> `
  --capacity-path <LOCAL_CAPACITY_PATH> `
  --ssh-alias <REVIEWED_ALIAS> `
  --ssh-config <OPTIONAL_SSH_CONFIG> `
  --remote-project-dir <DEPLOYMENT_DIRECTORY> `
  --remote-env-file <PROTECTED_RUNTIME_ENV>
```

The default PC batch is 1,000 rows, the hard maximum is 5,000, and a
ten-second inter-batch delay protects the collector by default. Server canary
runs should also use an explicit measured delay whenever the unpaced pilot
affects collector health. Each SSH batch sets PostgreSQL
`default_transaction_read_only=on` and a five-second statement timeout.
Completed objects and manifests use owner-only modes,
temporary names, atomic finalize, full read-back, checksums, row counts, RAW-ID
digests, and deterministic keys. Re-running a verified day performs validation
without querying or duplicating data. A PC filesystem manifest is external
retention evidence only while that protected archive remains available and
verified; it is not redundant until independently backed up.

Daily operator sequence:

1. Confirm collector and PostgreSQL are healthy, VPS free space exceeds the
   4 GiB pause threshold, and the PC destination is owner-only with sufficient
   capacity.
2. Select the oldest closed UTC day. Use RAW ID zero for the first archive and
   the previous verified manifest maximum for each later day.
3. Run `pc-export-day` with the default throttle. A health, disk, SSH, timeout,
   or format failure stops after the last published checkpoint.
4. Repeat the identical command. Success must report identical object counts,
   bytes, checksums, row counts, RAW-ID digest, timestamps, and event-type
   counts without creating duplicate objects.
5. Copy the PC archive to an independently protected medium before treating it
   as durable. Keep the manifest with its objects.
6. Run `retention-plan --hot-raw-days 3` only as a dry run. Review contiguous
   verified days, PostgreSQL/WAL capacity, and collector health; request
   separate approval before any production delete.

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
  --raw-hot-days 3 \
  --normalized-hot-days 0
```

The normal target is three closed RAW days and zero normalized PostgreSQL
days. The planner blocks rather than shortening this window. A two-day window
is degraded emergency planning only through
`--allow-degraded-two-day`; it remains a warning and requires separate human
retention approval. The 3 GiB reserve is immutable. Actual RAW and Parquet
daily rates require a bounded archive canary before any retention approval.

## Retention

No automatic deletion, timer, or unattended production mutation is enabled.
`hibachi-archive retention-plan` remains dry-run only for manifest-based daily
eligibility. Production bounded RAW deletion is available only through explicit
operator commands:

- `hibachi-archive retention-coverage-gate` — verify B2 archive storage coverage
  (COMPLETED markers, event counts, no INCOMPLETE) independent of research
  admission or quality PASS;
- `hibachi-archive retention-dry-run` — plan bounded batches with filesystem
  audit/progress, requires operator-confirmed guards and measured free disk;
- `hibachi-archive retention-execute` — same as dry-run unless
  `--confirm-delete` and `--confirmation-token DELETE_VERIFIED_ARCHIVE` are both
  supplied. Mutation requires `RETENTION_DATABASE_URL` for the `retention` role;
  `DATABASE_URL` / `research` cannot delete RAW rows.

Deletes are bounded to at most 1,000 locked rows per transaction with optional
inter-batch pauses and health re-checks. A closed daily range in
`retention-plan` is eligible only when its external manifest is verified, its
destination is external (`s3` or `pc_filesystem`), RAW count and identity
coverage match, adjacent selected UTC intervals have no gap, and the configured
hot window remains.

The library also contains a separately guarded bounded executor for synthetic
integration proof. It requires an exact confirmation token, writes an external
audit record before and after a transaction, deletes at most 1,000 locked rows,
and is not exposed through the production CLI. Future production activation
requires separate review and approval. Large DELETE transactions, cascade
deletes, unattended partition drops, VACUUM FULL, `pg_repack`, and automatic
retention are prohibited. A future approved delete uses at most 1,000 rows per
transaction with health pauses. Ordinary DELETE makes PostgreSQL pages reusable
but normally does not return relation files to the operating system; filesystem
recovery requires later natural reuse, not a rewrite on the constrained host.

The designed reclaim path on this VPS is operator-approved DROP of a
**verified closed RANGE(`id`) generation** after B2 storage-integrity gates
pass. See `docs/raw_partition_lifecycle.md`. Bounded DELETE retention remains
the emergency/legacy path and must not be removed. Automatic generation DROP is
not enabled in the first rollout.

## Existing 2.5M RAW rows

Do not copy or backfill the full table. After external storage and the RAM
upgrade are independently approved:

1. measure RAW/day and WAL/day read-only;
2. archive the oldest complete UTC day in batches;
3. verify every object and manifest from external storage;
4. run a dry-run retention plan;
5. perform an isolated bounded canary on synthetic/test data;
6. preserve at least three complete hot days and the 3 GiB reserve;
7. request separate approval before any production deletion.

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
