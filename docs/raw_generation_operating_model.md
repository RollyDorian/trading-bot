# Continuous RAW generation operating model

STATUS: **production generation-1 lifecycle proven** (`GENERATION_DROP_COMPLETE`
for `market_events_g_7471913`). Sustainable continuous COLLECT may run under this
policy with **human-approved physical DROP only**.

## Primary lifecycle (replaces monolithic DELETE reuse)

```text
COLLECT (logical parent market_events)
  → provision successor before ACTIVE boundary
  → rotate metadata: ACTIVE → CLOSED_UNARCHIVED; successor → ACTIVE
  → archive CLOSED generation (bounded id / hourly windows) while N+1 collects
  → verify B2 (storage integrity only)
  → mark DROP_ELIGIBLE
  → operator-approved physical DROP (token DROP_VERIFIED_GENERATION)
  → reclaim filesystem
  → continue collection
```

Bounded DELETE retention remains the **emergency/legacy** fallback. It does not
reliably return relation files to the OS and must not be the normal reclaim path.

## Hard invariants

1. Exactly one `ACTIVE` generation.
2. At least one writable successor must be provisioned before `ACTIVE` reaches
   its id boundary (default lead: `PRE_BOUNDARY_PROVISION_ROWS = 50_000`).
3. No insert may land outside a bounded generation (no DEFAULT partition).
4. Generation id ranges never overlap.
5. Global RAW ids remain monotonic via `market_events_id_seq`.
6. `CLOSED_*` generations are immutable for collector writes.
7. Unverified generations can never be dropped.
8. B2 outage never causes local data deletion.
9. Collector stops fail-closed before safe local capacity is exhausted.
10. Normal runtime (`research`) has no DROP/DDL rights.
11. Physical DROP is **never** automatic.

Crash/restart recovers from PostgreSQL catalog + `market_event_generations`
only. Missing cover or unexpected partitions fail closed (no guessing).

## State machine (permitted edges)

```text
PROVISIONED → ACTIVE
ACTIVE → CLOSED_UNARCHIVED
CLOSED_UNARCHIVED → ARCHIVING | ARCHIVE_FAILED
ARCHIVING → VERIFIED | ARCHIVE_FAILED | VERIFY_FAILED | CLOSED_UNARCHIVED
ARCHIVE_FAILED → CLOSED_UNARCHIVED | ARCHIVING
VERIFY_FAILED → ARCHIVING | CLOSED_UNARCHIVED
VERIFIED → DROP_ELIGIBLE
DROP_ELIGIBLE → DROPPED
DROPPED → (terminal)
```

## Archive operational hardening (production lessons)

### max-rows

First production archive failed because `archive-export-window` defaulted to
**50_000** rows while dense hours contained **~59.5k–59.6k** events.

Fix: `DEFAULT_MAX_ROWS = GENERATION_ARCHIVE_DEFAULT_MAX_ROWS = 100_000` with
immutable `HARD_MAX_ROWS = 200_000`. Bounds are never removed. Batch planning
already defaults to the hard cap.

### `/work` permissions

First attempt mounted host `archive-work` at `/work` while the container ran as
UID/GID **10001**, but the host directory was deploy-user owned →
`PermissionError`. Resume temporarily used `chmod 777` / root — **not** the
lasting contract.

Approved contract (`trading_bot.archive.workdir.ensure_archive_workdir`):

* host path owned by `10001:10001`;
* mode `0700` (never world-writable);
* writable probe before export;
* fail closed with an explicit ownership error.

### Resume

Failed archives must preserve completed windows and failure evidence, never
delete CLOSED RAW, and safely resume / reuse remotely verified windows. The
batch runner already skips verified windows; generation metadata retains
`ARCHIVE_FAILED` until a successful verify marks `DROP_ELIGIBLE`.

## Capacity / DROP backlog (measured)

Production generation-1 physical size: **203546624 B (~194.1 MiB)** for 400000
rows. Rate ~59k events/h → ~6.8 h/generation. Operator emergency floor remains
**5 GiB** (not lowered). Hard archive floor remains **3 GiB**.

At ~6.20 GiB free:

* headroom above 5 GiB ≈ **1.20 GiB**;
* reserve ACTIVE growth (~194 MiB) + WAL cushion (128 MiB);
* remaining budget allows a small DROP_ELIGIBLE backlog (policy cap **2**) and at
  most **1** CLOSED/ARCHIVING generation.

| State | Meaning |
|---|---|
| `READY` | free covers floor + one generation + WAL; no pending closed archive |
| `ARCHIVE_PRESSURE` | closed/unverified generation pending or READY band not met |
| `DROP_APPROVAL_REQUIRED` | ≥1 `DROP_ELIGIBLE`; collect may continue while backlog fits |
| `STOP_REQUIRED` | below floors, backlog caps exceeded, or historical+ACTIVE+WAL no longer fit |

## Automation boundaries

**Automatic (managed, SSH-independent):** capacity monitoring; successor
provisioning; ACTIVE→CLOSED metadata rotation; archive launch/retry/resume; B2
verify; mark `DROP_ELIGIBLE` **only while** `DROP_ELIGIBLE < 2`. When the
human-DROP queue is full the executor persists `DROP_BACKLOG_LIMIT` and
skips the next CLOSED/FAILED generation. It does not auto-DROP.
Production soak `HIBACHI_STORAGE_LIFECYCLE_SOAK_PASS` proved the cycle
`2 → human DROP → 1 → archive → 2` and that a later natural CLOSED stays
`CLOSED_UNARCHIVED` while the cap is full (`docs/hibachi_storage_lifecycle_soak_v1.md`).

**Manual only:** physical DROP (`DROP_VERIFIED_GENERATION` per exact generation);
threshold lowering; restart after hard capacity/integrity stop; B2 deletion;
PostgreSQL destructive cleanup.

Collector runs as Docker Compose service (`restart: on-failure:5`). Generation
maintenance runs as a separate oneshot/timer (`scripts/generation_maintain.py`
and/or host `scripts/hibachi_generation_maintain.sh` via cron every 10m) using
the owner/maintenance DB path — not the research collector role. Interactive
SSH disconnection must not stop either process.

### Provision lead must outrank capacity STOP

Capacity/archive backlog `STOP_REQUIRED` is an operator advisory for reclaim
pressure. It must **never** skip successor CREATE/attach when remaining ids
enter the lead window. Incident `HIBACHI_PARTITION_RECOVERY` proved that
`closed_n > 1` exiting before provision left ACTIVE uncovered at id `9071913`.

Required order: assess → provision (≤50k, idempotent) → rotate metadata →
persist status → report capacity STOP.

Additional urgency (≈59k events/h):

| State | Remaining ids without successor | Intent |
|---|---|---|
| `PROVISION_REQUIRED` | ≤ 50_000 | normal lead CREATE |
| `PROVISION_LATE` | ≤ 10_000 | high-priority retry / alert |
| `COVER_STOP_REQUIRED` | ≤ 1_000 (or past bound) | stop collector deliberately before INSERT partition-miss noise |

Status must surface active bounds, next id, remaining, expected successor,
exists yes/no, urgency, last attempt/error, and action required
(`provision.status.env` on the host tick; operator status fields in-repo).

## Crash / restart policy

| Stop class | Auto-restart? |
|---|---|
| Transient transport failure | Yes (collector supervisor / Compose on-failure) |
| Normal Compose restart | Yes |
| Capacity `STOP_REQUIRED` | **No** — operator review |
| Missing successor / cover | **No** — fail closed |
| Archive integrity / verify hard fail | Collector may continue; archive retries; no DROP |
| Programming / unknown DB failure | **No** — fail closed |

Never trade availability for RAW integrity.

## Operator status (read-only)

`scripts/generation_status.py` shows collector, ACTIVE fill, SUCCESSOR,
CLOSED/ARCHIVE, DROP_ELIGIBLE candidates (exact identity + evidence + token),
filesystem/WAL, and ACTION.

## Monitoring cadence

* Generation horizon ~6.8 h; provision lead remains 50k ids (~51 min).
* `generation_maintain` every **10–15 minutes** (cheap metadata/DDL).
* Capacity/status checks every **10–15 minutes** (read-only).
* Start archive promptly after rotation; **one** generation archive at a time.
* Production `hibachi-auto-archive-tick.sh` runs the bounded oldest-CLOSED
  `archive-export-window` executor (`hibachi_emergency_archive_one.sh
  --require-normal-floor`). It is **not** marker-only. It **never** passes
  `--drop`. While `COLLECT_HOLD` is present the tick skips (emergency oneshot
  owns the loop). Normal operation still requires `free >= 5 GiB` plus the
  128 MiB temp-space preflight. Incident `HIBACHI_EMERGENCY_CAPACITY_RECOVERY`
  used a separate 3 GiB emergency archive floor only while COLLECT was paused.
  The operator 5 GiB floor is unchanged. Physical DROP remains human-approved.

## Human DROP workflow

Each physical DROP requires exact generation identity, archive evidence hash,
state `DROP_ELIGIBLE`, and token `DROP_VERIFIED_GENERATION`. Do not bundle
multiple generations into one authorization.

## Implementation map

| Area | Module |
|---|---|
| Transitions | `storage/generation_transitions.py` |
| Capacity / backlog | `storage/capacity.py` |
| Rotation / recovery | `storage/rotation.py` |
| Closed archive workflow | `storage/generation_archive.py` |
| Archive workdir contract | `archive/workdir.py` |
| Operator status | `storage/operator_status.py` |
| Maintain oneshot | `scripts/generation_maintain.py` |
| Status CLI | `scripts/generation_status.py` |
| DROP gate | `storage/partitions.py` + SECURITY DEFINER SQL |
