# RAW PostgreSQL partition / generation lifecycle

STATUS note: this document is the design and local-proof contract. Production
schema migration, collector restart, B2 deletion, VACUUM FULL, and automatic
DROP are **out of scope until explicit human approval**.

## Architecture comparison

| Option | Disk reclaim | Operational risk on 15 GiB VPS | Fits this repo |
|---|---|---|---|
| **A. Monolithic `market_events` + bounded DELETE** | Poor. Ordinary DELETE reuses pages but does not return relation files to the filesystem (production evidence: empty table still ~641.8 MiB after deleting ids `6207906..7471912`). | Low schema risk; already implemented and gated. Does not solve capacity. | Keep as emergency/legacy fallback |
| **B. Periodically DROP/reCREATE entire `market_events`** | Strong reclaim when empty. | High: sequence ownership, grants, concurrent readers/exporters, and any future non-empty mistake are catastrophic; no generation-level archive gate. | Rejected as routine lifecycle |
| **C. Native RANGE(`id`) partitioned `market_events` with bounded generations** | Strong: `DROP` of a verified closed partition removes heap/index files. Logical parent name stays stable for collector/export. | Medium migration once; then operator-gated rotation. Matches RAW id identity already used by manifests/retention. | **Selected** |

VACUUM FULL / CLUSTER / `pg_repack` are **not** routine lifecycle tools on this
host: they require substantial extra disk and exclusive locks.

## Partition key: `id` (RANGE)

Chosen over `received_at` because:

- RAW deterministic ordering and archive manifests already key on raw event id;
- retention coverage gates already verify contiguous `min_raw_event_id` /
  `max_raw_event_id`;
- `PRIMARY KEY (id)` remains valid under RANGE(`id`) without composite PK changes;
- time partitions can split a single id continuum awkwardly under clock skew,
  backfill, or reconnect bursts, and complicate exact id-span archive identity.

Physical size is an **additional safety signal**, not the sole boundary.
Deterministic boundary is the id/row span.

## Global sequence continuity

One sequence `market_events_id_seq` remains owned by the parent
`market_events.id` default. Partition DROP must never drop the sequence
(`OWNED BY NONE` before destructive parent rewrites; children do not own it).

Production continuity requirement:

```text
last_value = 7471912  (is_called = true)
next insert id = 7471913
```

Local tests set the same cursor and assert routing into
`market_events_g_7471913`.

## Generation size

Production calibration: ~1,264,007 archived rows corresponded to ~612–642 MiB
total relation (~533 B/row including indexes).

Local disposable pilot (`scripts/partition_generation_pilot.py`, synthetic
padded payload ≈593 B/row):

| Span | Total bytes | Heap | Indexes | Insert rows/s | WAL approx |
|---|---|---|---|---|---|
| 25k | 14.2 MiB | 13.0 MiB | 1.2 MiB | ~51k | ~20.6 MiB |
| 50k | 28.3 MiB | 26.0 MiB | 2.2 MiB | ~51k | ~41.3 MiB |
| 100k | 56.5 MiB | 52.1 MiB | 4.4 MiB | ~50k | ~82.6 MiB |

Extrapolated with production 533 B/row:

| Candidate span | Calibrated total size |
|---|---|
| 250k | ~127 MiB |
| **400k (default)** | **~203 MiB** |
| 500k | ~254 MiB |

DELETE vs DROP (20k-row disposable partition):

| Path | Relation bytes before | After | Reclaimed |
|---|---|---|---|
| Ordinary DELETE (rows=0) | 11.4 MiB | 11.4 MiB | **0** |
| DROP partition | 11.4 MiB | 0 (regclass null) | **11.4 MiB** |

Default in code/migration: `DEFAULT_GENERATION_ROW_SPAN = 400_000` aiming for
the 200–300 MiB active-generation band. Soft physical signal:
`GENERATION_PHYSICAL_SOFT_LIMIT_BYTES = 300 MiB`. Synthetic pilot suggested
~450k toward 250 MiB at its heavier payload; production calibration keeps 400k.
## State machine

```text
PROVISIONED → ACTIVE → CLOSED_UNARCHIVED → ARCHIVING → VERIFIED → DROP_ELIGIBLE → DROPPED
                                      ↘ ARCHIVE_FAILED
                                      ↘ VERIFY_FAILED
```

Rules:

- Exactly one `ACTIVE` writer generation in normal operation.
- Collector may continue into a `PROVISIONED` successor (becomes `ACTIVE`) while
  the predecessor is archived, if capacity policy allows.
- Never DROP `ACTIVE`.
- Never DROP unverified RAW.
- Automatic DROP is disabled for the first rollout; operator confirmation token
  `DROP_VERIFIED_GENERATION` is required.

## Archive gate (storage integrity only)

A closed generation becomes `DROP_ELIGIBLE` only when evidence proves:

- exact min/max raw id match for the archived closed range;
- exact row count match;
- contiguous id coverage per stream semantics;
- checksums PASS;
- manifest PASS;
- remote COMPLETED;
- download verification PASS;
- storage reconciliation PASS.

Research quality / paper-admission eligibility must **not** control DROP.
Quarantined but structurally verified RAW may still be retention/drop eligible.

## Failure modes

| Failure | Behavior |
|---|---|
| B2 unavailable | Keep closed generation locally; optionally continue into next generation while capacity allows; stop collector fail-closed when safe buffer exhausted |
| Archive verify failure | Remain non-droppable (`VERIFY_FAILED` / not `DROP_ELIGIBLE`) |
| Process crash | Recover state from `market_event_generations` + `pg_class` |
| Collector restart | `ensure_writable_cover` / startup must see a writable generation for next sequence id |
| Missing future partition | Fail closed before accepting events; no silent default unbounded partition |

## Permissions

| Role | Rights |
|---|---|
| `research` (runtime) | `SELECT`/`INSERT` on `market_events`, sequence `USAGE`/`SELECT`; **no** DROP/DDL |
| `retention` | Existing bounded DELETE path for emergency/legacy monolithic or partial-range cleanup; **no** DROP |
| `partition_maintenance` | `SELECT` + `EXECUTE` on `drop_verified_market_event_generation(...)` only |
| migration/table owner | DDL; owns SECURITY DEFINER DROP function |

Do not broaden `research`.

## Current empty production table (migration opportunity — NOT executed)

Observed:

- collector STOPPED;
- `market_events` row count 0;
- sequence next id `7471913`;
- prior RAW verified in B2;
- empty relation still allocates ~641.8 MiB.

Replacing the empty monolithic heap with a partitioned parent + first generation
and dropping the empty legacy relation is expected to return most of that
~641.8 MiB to the filesystem. This is a uniquely low-risk window because no
RAW rows need rewrite. **Do not migrate automatically.**

### Production migration plan (human review required)

1. Confirm collector stopped, write quiescent, PostgreSQL healthy, B2 archive
   for prior interval COMPLETED + restore PASS.
2. Take a protected logical backup / snapshot evidence.
3. Record `pg_total_relation_size('market_events')`, sequence cursor, grants.
4. Apply `20260809_0004` only while `COUNT(*)=0` (migration fail-closed otherwise).
   Production may be on `20260729_0002`; partition revision revises RAW v2 directly.
   Do **not** enable normalized live tail/backfill (`20260729_0003` remains optional).
5. Verify: partitioned parent, sequence next=`7471913`, first ACTIVE generation
   covering that id, indexes present, research INSERT probe on disposable clone
   first when possible.
6. Measure filesystem free space delta (expect ~600+ MiB reclaim from dropping
   the empty monolithic files).
7. Keep collector stopped until explicit restart approval.
8. Destructive generation DROP remains operator-approved after archive canaries.

## Retention compatibility

The bounded DELETE retention subsystem (`hibachi-archive retention-*`,
`BoundedRetentionRunner`) is **preserved** as:

- emergency fallback on constrained hosts before partition rollout completes;
- legacy monolithic-range handling;
- optional partial-range cleanup inside a partition if ever required;

It is **not** the primary reclaim path after partition rollout. Partition DROP
is the reclaim path. Do not silently remove DELETE retention code or audits.

## Automation target (future)

Monitor → provision next → close → B2 archive → verify → restore as required →
`DROP_ELIGIBLE` → operator-approved DROP → capacity verify → continue.

First rollout: steps through `DROP_ELIGIBLE` may be automated; physical DROP
stays manually approved until repeated canaries succeed.

## Implementation map

| Area | Location |
|---|---|
| Lifecycle library | `src/trading_bot/storage/partitions.py` |
| Archive DROP gate | `src/trading_bot/storage/partition_gate.py` |
| Alembic | `migrations/versions/20260809_0004_raw_market_events_partitions.py` |
| Maintenance role SQL | `deploy/postgres/provision_partition_maintenance_role.sql` |
| Local pilot | `scripts/partition_generation_pilot.py` |
| Tests | `tests/test_partitions.py`, `tests/integration/test_market_events_partitions_postgres.py` |
