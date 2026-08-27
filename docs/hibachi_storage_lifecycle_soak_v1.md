# Hibachi storage lifecycle soak v1

STATUS: **HIBACHI_STORAGE_LIFECYCLE_SOAK_PASS**

Milestone: `HIBACHI_STORAGE_LIFECYCLE_SOAK`  
Continuation: `HIBACHI_STORAGE_LIFECYCLE_SOAK_CONTINUATION_AFTER_ONE_DROP`

Parent recovery: `HIBACHI_EMERGENCY_CAPACITY_RECOVERY_PASS`.

The first soak window (below, Phase A, 18:08–18:16Z 2026-08-17) proved the
`DROP_ELIGIBLE=2` gate and aborted a third archive, but could not PASS: ACTIVE
still had ~312k ids remaining. Elapsed time alone is not PASS.

The continuation (Phase B) executed the one human-authorized physical DROP of
`g_10671913_11071913`, let `*/15` auto-archive resume `g_11471913` by reusing
COMPLETED windows, then observed **two** unattended 50k-lead provisions and
natural rotations. New CLOSED generations stayed `CLOSED_UNARCHIVED` while the
queue was full. No partition-miss. Collector restart delta **0**. External-ref
**OFF**. ML **BLOCKED**. `g_11071913` was **not** DROPped.

Latest re-read: **2026-08-18T09:23:34Z**.

## Phase A — gate proof without natural rotation (historical)

STATUS at close of this window: `SOAK_INCOMPLETE_NO_NATURAL_ROTATION`.
Superseded by Phase B. Tables below are the 18:08–18:16Z 2026-08-17 snapshot.

## PRESTATE (fresh, 2026-08-17T18:08:29Z)

Do not use the 17:45Z recovery snapshot. Live re-read:

| Field | Value |
|---|---|
| UTC | 2026-08-17T18:08:29Z |
| Collector | running, health **healthy**, restart count **0**, `on-failure:5`, started 2026-08-17T17:18:07Z |
| PostgreSQL | healthy |
| Free | **5,753,016,320 B** (≥ READY 5,706,473,472; ≥ 5 GiB floor) |
| ACTIVE | `g_11871913_12271913` `[11871913,12271913)` |
| ACTIVE rows | 80,274 |
| max id / `received_at` | 11952131 / 2026-08-17T18:08:31.731224Z |
| Sequence | `last_value=11952126`, `is_called=true` (inserts racing; next ≈ 11952132) |
| Remaining to ACTIVE end | ~328k (provision.status at 18:00Z: 328,236) |
| Successor expected | `g_12271913_12671913` `[12271913,12671913)` |
| Successor exists | **no** (remaining ≫ 50k; correct) |
| DROP_ELIGIBLE | **2** — `g_10671913_11071913`, `g_11071913_11471913` |
| ARCHIVING | **1** — `g_11471913_11871913` (18:00 `*/15` tick, **third** archive) |
| CLOSED_UNARCHIVED | 0 |
| ARCHIVE_FAILED | 0 (before abort) |
| Cron | `*/10` generation-ops; `*/15` auto-archive `--require-normal-floor` no `--drop` |
| External-ref | OFF |

The 17:45 report (`ARCHIVING=g_11071913`, `CLOSED=g_11471913`, `DROP_ELIGIBLE=1`)
was already stale: `g_11071913` had become DROP_ELIGIBLE at 17:57:48Z, and the
18:00 tick had started `g_11471913`.

## DROP_BACKLOG_GATE

Historical policy `DROP_ELIGIBLE <= 2` existed in `capacity.py` assessment
text but **was not enforced** by `hibachi_emergency_archive_one.sh`. Auto-archive
would have turned CLOSED into an unbounded DROP_ELIGIBLE queue.

| Rule | Behavior |
|---|---|
| `DROP_ELIGIBLE < 2` | oldest CLOSED/FAILED/ARCHIVING may archive |
| `DROP_ELIGIBLE >= 2` | no new/resumed archive; persist `DROP_BACKLOG_LIMIT`; no DROP |
| Auto-DROP | never (`drop_requested=0` on the tick path) |

Implemented in:

* `scripts/hibachi_emergency_archive_one.sh` (gate **before** `ARCHIVING` mutate)
* `scripts/hibachi-auto-archive-tick.sh` (persist `archive_one_rc` + status)
* `scripts/hibachi_generation_maintain.sh` (`DROP_BACKLOG_LIMIT` + `human_drop_approval`)
* `trading_bot.storage.capacity.drop_backlog_blocks_new_archive`

### Third-archive abort

At 18:09:29Z the in-progress `g_11471913` oneshot was stopped **before**
`DROP_ELIGIBLE=3`. Sidecar `crazy_bhaskara` stopped. Collector untouched
(restart count stayed 0). Metadata:

`g_11471913_11871913` → **ARCHIVE_FAILED** (partial B2 hour objects retained;
do not overwrite COMPLETED later). Status `DROP_BACKLOG_LIMIT`.

Six hourly window JSONs were already verified for that generation; they are
resume evidence for a future archive **after** a human DROP lowers the queue.

### Gate proof

Deliberate probe 18:12:34Z and unattended cron 18:15:01Z:

```text
candidate=g_11471913_11871913 ... state=ARCHIVE_FAILED
drop_eligible_count=2 limit=2
DROP_BACKLOG_LIMIT ... skipped_new_archive
drop_requested=0
```

`g_11471913` remained `ARCHIVE_FAILED`. Queue stayed at 2.

Maintain 18:12:33Z: `DROP_BACKLOG_LIMIT drop=2`,
`action_required=human_drop_approval`, `STATUS=ok` (collector not stopped).

## PROVISIONING

| Field | Value |
|---|---|
| 50k trigger | `remaining <= 50000` → `next_id >= 12221913` |
| Successor created | **no** (remaining 315,774 at 18:12Z; 311,929 at 18:16Z) |
| Bounds | expected `[12271913,12671913)` when lead hits |
| Urgency | `NORMAL`; 10k / ≤1k paths unchanged in maintain (not exercised) |
| Order | provision still before capacity STOP (no regression) |

Natural provision/rotation **not observed**. Not forced.

## ROTATION

**Not occurred.** ACTIVE still `g_11871913_12271913`. No partition errors.
Sequence monotonic (11902152 … 11959984+). No invented ids.

## AUTO_ARCHIVE (this soak)

| Tick | Generation | Result |
|---|---|---|
| 17:30 | `g_10671913` | archive → B2 verify → DROP_ELIGIBLE, `skip_physical_drop` (recovery live proof) |
| 17:45 | `g_11071913` | same; DROP_ELIGIBLE at 17:57:48Z; 8 windows; evidence `0378d45b…18e8` |
| 18:00 | `g_11471913` | started after queue already 2; **aborted** 18:09Z; log `Terminated` |
| 18:12 probe | `g_11471913` | `DROP_BACKLOG_LIMIT` skip |
| 18:15 cron | `g_11471913` | `DROP_BACKLOG_LIMIT` skip, `archive_one_rc=0` |

Cron definition unchanged:

```text
*/10 * * * * .../hibachi-generation-ops-tick.sh
*/15 * * * * .../hibachi-auto-archive-tick.sh
```

Last maintain success: 18:12:33Z `STATUS=ok` (18:10 also ok; overlay-typo
fixed earlier at 17:44Z, no recurrence). Last auto-archive **failure**: none
after the gate; 18:00 was operator-aborted mid-window, not a B2 error.

One-at-a-time: lock `~/gen-cycle/archive.lock`; no concurrent archives after abort.

## DROP_CANDIDATES (do not DROP)

| generation | range | rows | physical B | windows | evidence SHA-256 | expected reclaim |
|---|---|---:|---:|---:|---|---:|
| `g_10671913_11071913` | `[10671913,11071913)` | 400000 | 207,478,784 | 7 | `dccd3292a6059b6eec400dfffbac833a73c5766b07358dfd96bced3ba1766050` | ~207.5 MiB |
| `g_11071913_11471913` | `[11071913,11471913)` | 400000 | 206,274,560 | 8 | `0378d45b37fbe11b2e72f5be127f614712a5151482296796c0ba6b14838618e8` | ~206.3 MiB |

B2 verify on both: PASS (`reuse_completed` + `upload_builtin_verify`). Restore
status verified. Reclaim estimate uses `pg_total_relation_size` (prior DROPs
reclaimed ≈ 1.00×).

## B2_CLASS_B

No Class B cap during this soak. Pattern from window JSON:

* `g_10671913`: 1 `reuse_completed` (COMPLETED marker) + 6 `upload_builtin_verify`
* `g_11071913`: 1 `reuse_completed` + 7 `upload_builtin_verify`
* Missing COMPLETED: one small GetObject `NoSuchKey`, then upload path (not a
  second full parquet restore)
* Retries: none observed
* Duplicate full-download restores: none (`class_b_path=upload_builtin_verify`)

Exact Caps & Alerts counters still unavailable in tooling. Not invented.

## DISK

READY target **5,706,473,472 B**. Floor **5 GiB**. Emergency 3 GiB floor is
not active (no `COLLECT_HOLD`).

| Point | Bytes |
|---|---:|
| Soak start 18:08:29Z | 5,753,016,320 |
| Maintain 18:10:02Z (min observed) | 5,713,838,080 |
| After abort/gate 18:12:33Z | 5,749,198,848 |
| Soak snapshot 18:16:24Z | 5,747,089,408 |
| ACTIVE relation 18:08Z → 18:16Z | 41,787,392 → 45,776,896 |
| CLOSED/FAILED local (`g_11471913`) | 204,316,672 |
| DROP_ELIGIBLE local | 207,478,784 + 206,274,560 |

Archive temp peak inferred as the 18:10 dip (~39 MiB vs 18:08), not a
filesystem-floor event. Still ≥ READY and ≥ 5 GiB.

## CAPACITY_STOP

| Field | Value |
|---|---|
| `CAPACITY_STOP_REQUIRED` | absent |
| Triggered | **no** |
| Behavior | disk ≥ 5 GiB; collector remains running `on-failure:5` |

Not filled intentionally. Policy remains: if free < 5 GiB, persist stop and
actually stop COLLECT after 50k-lead provision.

## HEALTHCHECK

`HEALTHCHECK_CODE_PROVEN_OVERLAY_DEPLOYMENT_TEMPORARY`

GHCR digest unchanged (`sha256:fb1267a99f80…`). Overlay bind-mount present.

Live bounded query 18:16:24Z (collector running, 88,097 ACTIVE rows):

* execution **62.0 ms** (planning 6.5 ms)
* CLOSED children `never executed`
* only `market_events_g_11871913` scanned
* Docker health **healthy**

Stopped-state proof from recovery (18.8 ms / parent 1305 ms) still stands.
Image pull still deferred: READY margin is tens of MiB.

## HIBACHI / POSTGRES / QUALITY / EXTERNAL

| Field | Value |
|---|---|
| Hibachi | healthy; restart delta **0** this soak |
| PostgreSQL | healthy |
| QUALITY_GAPS | both registry rows intact: partition `9071913..9072163` and pause `2026-08-14T08:42:22.972216Z` → `2026-08-17T17:18:16.226260Z`; no synthesize/bridge |
| EXTERNAL_REF | OFF |
| ML_STATUS | BLOCKED |

## BLOCKERS

1. **No natural rotation yet** — ACTIVE remaining ~312k ids; 50k provision
   trigger at `next_id >= 12221913`. Soak cannot PASS on clock time.
2. **DROP_ELIGIBLE = 2** — further auto-archive of `g_11471913` is correctly
   blocked until human DROP of one verified generation.
3. Overlay healthcheck is still temporary (disk cannot hold a second 554 MiB
   image).

## NEXT (Phase A, superseded)

Superseded by Phase B. The authorized DROP of `g_10671913` was executed; do
**not** treat this section as still-open work.

---

## Phase B — continuation after one authorized DROP

Continuation identity: `HIBACHI_STORAGE_LIFECYCLE_SOAK_CONTINUATION_AFTER_ONE_DROP`.

Human authorization (this continuation only): physical DROP of **`g_10671913`
ONLY**. `g_11071913` remains DROP_ELIGIBLE and was not DROPped. Auto-DROP stayed
OFF. COLLECT was not accelerated. Generation width was not changed. External-ref
was not started.

### DROP_AUTHORIZATION

| Field | Value |
|---|---|
| Generation | `g_10671913_11071913` (`market_events_g_10671913`) |
| Bounds (metadata **and** `pg_get_expr`) | `FOR VALUES FROM ('10671913') TO ('11071913')` |
| Rows | 400000; min 10671913; max 11071912 |
| Physical bytes before DROP | 207,478,784 |
| State at preflight | exactly `DROP_ELIGIBLE` |
| Evidence SHA-256 (full) | `dccd3292a6059b6eec400dfffbac833a73c5766b07358dfd96bced3ba1766050` |
| Hash sources | DB `archive_evidence_sha256`, `evidence_g_10671913_11071913.sha256`, evidence JSON — all equal. Abbreviated `dccd3292…6050` was **not** used as a command parameter. |
| B2 probe immediately before DROP | COMPLETED marker GetObject for `eth-usdt-p_20260813T120000000000Z_20260813T130000000000Z_v2` (1028 bytes), `B2_CLASS_B_AVAILABLE=yes`. No parquet re-download. |
| Path | `SELECT public.drop_verified_market_event_generation('g_10671913_11071913', 'DROP_VERIFIED_GENERATION', true)` |
| Not used | `DROP TABLE`, DELETE rows, detach of any other child |
| `dropped_at` | 2026-08-17T18:41:31.851314Z |
| Result | metadata **DROPPED**; physical child **absent**; parent `market_events` still partitioned; ACTIVE unchanged at DROP time (`g_11871913_12271913`); `g_11071913` still `DROP_ELIGIBLE` |

Preconditions at the gated DROP (script `scripts/_vps_drop_g106_authorized.sh`):
collector healthy, PostgreSQL healthy, no archive lock, no archive job,
external-ref OFF, identity/hash/bounds/rows match, `g_11071913` still
DROP_ELIGIBLE. Had state not been exactly DROP_ELIGIBLE the path would have
exited `DROP_PRECONDITION_CHANGED` without DROP.

### DROP_RECLAIM

| Field | Bytes |
|---|---:|
| `free_before` | 5,725,130,752 |
| `free_after` | 5,932,617,728 |
| Function-reported reclaim | 207,478,784 |
| Observed reclaim (`after − before`) | **207,486,976** (authoritative filesystem measurement) |

### DROP_QUEUE

| When | DROP_ELIGIBLE | Members |
|---|---:|---|
| Before DROP | 2 | `g_10671913`, `g_11071913` |
| After DROP | 1 | `g_11071913` only |
| After `g_11471913` archive (18:51Z) | 2 | `g_11071913`, `g_11471913` |

Gate saw count 1 at the 18:45 cron tick (`drop_eligible_count=1 limit=2`) and
started archive. It did **not** DROP `g_11071913`.

### G114_RESUME

18:45:02Z `*/15` cron (not a manual bypass) selected oldest
`ARCHIVE_FAILED` `g_11471913_11871913`. Partial 18:00 artifacts were reused;
the job did not restart from scratch and did not overwrite COMPLETED windows.

| Window (UTC 2026-08-14) | Classification | Mode |
|---|---|---|
| 01:00–02:00 | COMPLETED / reusable | `reuse_local_evidence` |
| 02:00–07:00 (5 hours) | local evidence reusable | `reuse_local_evidence` + `upload_builtin_verify` |
| 07:00–08:00 | incomplete (COMPLETED `NoSuchKey`) | `export_upload` + `upload_builtin_verify` |
| 08:00–09:00 | incomplete (COMPLETED `NoSuchKey`) | `export_upload` + `upload_builtin_verify` |

Evidence JSON: `reused_completed_windows=1`, 8 hour buckets, 400000 rows
`[11471913,11871912]`, checksums/manifest/remote/readback verified.

Full evidence hash:

`f960f7f4b9de182149111d605ad96111d613cae8034740ee944e914521ad8129`

Result: `DROP_ELIGIBLE g_11471913_11871913`, `skip_physical_drop`. B2 Class B
did not cap this resume. No second full restore/download.

### AUTO_ARCHIVE (Phase B)

Cron unchanged:

```text
*/10 * * * * .../hibachi-generation-ops-tick.sh
*/15 * * * * .../hibachi-auto-archive-tick.sh
```

Ticks use `--require-normal-floor` and `drop_requested=0`.

| Tick (UTC) | Target | Result |
|---|---|---|
| 18:12 / 18:15 / 18:30 | `g_11471913` `ARCHIVE_FAILED` | `DROP_BACKLOG_LIMIT` skip (queue still 2) |
| 18:41 | — | human DROP `g_10671913` (not cron) |
| 18:45 | `g_11471913` `ARCHIVE_FAILED` | archive **allowed** (`drop_eligible_count=1`); resume reuse + 2 new windows |
| 19:00 / 19:15 … 23:30 | — | `no_archive_candidate` (remaining historical gens already DROP_ELIGIBLE; no CLOSED) |
| 23:30 archive vs 23:30 maintain | — | archive raced **before** ROTATED → still `no_archive_candidate` |
| 23:45 | `g_11871913` `CLOSED_UNARCHIVED` | `DROP_BACKLOG_LIMIT` skip (queue back to 2) |
| 00:00 … 07:15 2026-08-18 | `g_11871913` `CLOSED_UNARCHIVED` | same skip; `g_12271913` also CLOSED after 06:20 and was **not** selected ahead of the oldest CLOSED |
| 07:30 onward | — | `EMERGENCY_ARCHIVE_CAPACITY_BLOCKED` (`free < 5 GiB + 128 MiB` worst-case). Floor 5 GiB **not** breached. Collector **not** stopped. |

This is the intended dynamic gate: `2 → human DROP → 1 → archive → 2 → skip
new CLOSED`. `DROP_BACKLOG_LIMIT` here is policy, not soak failure.

### PROVISIONING

50k lead is `remaining <= 50000`. Width 400000. `*/10` maintain. Provision
still ran **before** capacity-STOP evaluation (`STATUS=ok` on both ticks;
`CAPACITY_STOP_REQUIRED` absent). No DEFAULT child. No overlapping
`pg_get_expr` ranges.

#### Cycle 1 — successor of recovery ACTIVE

Expected successor `[12271913,12671913)`. Confirmed
`FOR VALUES FROM ('12271913') TO ('12671913')`.

| Tick | `next_id` | remaining | successor | urgency | action |
|---|---:|---:|---|---|---|
| 22:30:01Z | 12212346 | 59567 | absent | NORMAL | skip |
| 22:40:01Z | **12222277** | **49636** | absent → **PROVISIONED `g_12271913_12671913`** | PROVISION_REQUIRED | CREATE TABLE + INSERT metadata |
| 22:50:01Z | — | — | present | — | first status after provision |

Trigger id `12221913` was crossed between 22:30 and 22:40. First live
observation past the lead: `next_id=12222277` (364 ids past exact trigger;
10-minute granularity). Free at provision: 5,798,289,408 B.

#### Cycle 2 — successor of the first rotated ACTIVE

Expected successor `[12671913,13071913)`. Confirmed
`FOR VALUES FROM ('12671913') TO ('13071913')`.

| Tick | `next_id` | remaining | successor | urgency | action |
|---|---:|---:|---|---|---|
| 05:20:01Z | 12619569 | 52344 | absent | NORMAL | skip (`closed=1 drop=2`) |
| 05:30:01Z | **12629518** | **42395** | absent → **PROVISIONED `g_12671913_13071913`** | PROVISION_REQUIRED | CREATE TABLE + INSERT metadata |

Trigger id `12621913` was crossed between 05:20 and 05:30. Free at provision:
5,563,064,320 B.

Next expected successor `g_13071913_13471913` `[13071913,13471913)` is **absent**
at 09:23Z (`remaining ≈ 214k` ≫ 50k). Correct.

### ROTATION

#### Rotation 1 — `g_11871913_12271913` → `g_12271913_12671913`

Maintain 23:30:01Z: `next=12271972 remaining=-59 successor_exists=1` then
`ROTATED old=g_11871913_12271913 new_start=12271913`.
`closed_at=2026-08-17T23:30:03.856771Z`. Free: 5,771,771,904 B.

| Field | Value |
|---|---|
| Old last id / `received_at` | **12271912** / 2026-08-17T23:29:59.071664Z |
| Old rows | **400000** |
| New first id / `received_at` | **12271913** / 2026-08-17T23:29:59.073954Z |
| Routing | `COUNT(*) FROM market_events_g_12271913 WHERE id=12271913` = **1** |
| Partition errors | **none** (`NO_PARTITION_MISS=0` in collector logs since resume) |

Metadata rotation lagged the id crossing by the next `*/10` tick (~7 s after
the last old row; sequence already on the successor child because it was
provisioned at 22:40).

#### Rotation 2 — `g_12271913_12671913` → `g_12671913_13071913`

Maintain 06:20:01Z: `next=12679156 remaining=-7243 successor_exists=1` then
`ROTATED old=g_12271913_12671913 new_start=12671913`.
`closed_at=2026-08-18T06:20:03.381704Z`. Free: 5,536,673,792 B.

| Field | Value |
|---|---|
| Old last id / `received_at` | **12671912** / 2026-08-18T06:12:44.617494Z |
| Old rows | **400000** |
| New first id / `received_at` | **12671913** / 2026-08-18T06:12:44.623902Z |
| Routing | `COUNT(*) FROM market_events_g_12671913 WHERE id=12671913` = **1** |
| Partition errors | **none** |

Ids are contiguous across both boundaries. Sequence remaining monotonic
(`last_value` 12858868 at 09:21Z with inserts racing). Collector restart
count stayed **0** from 2026-08-17T17:18:07Z through 09:23Z.

One Hibachi WS line `Unexpected message type: 257` since resume; not a
partition miss; no new collection gap.

### DROP_BACKLOG_GATE (post-rotation)

Limit **2**. After refill, automatic archives stop. New CLOSED must **not**
become a third DROP_ELIGIBLE.

At 09:23Z:

| generation | state | `pg_get_expr` |
|---|---|---|
| `g_11071913_11471913` | DROP_ELIGIBLE | `[11071913,11471913)` |
| `g_11471913_11871913` | DROP_ELIGIBLE | `[11471913,11871913)` |
| `g_11871913_12271913` | **CLOSED_UNARCHIVED** | `[11871913,12271913)` |
| `g_12271913_12671913` | **CLOSED_UNARCHIVED** | `[12271913,12671913)` |
| `g_12671913_13071913` | ACTIVE | `[12671913,13071913)` |

`g_11871913` (oldest CLOSED, the recovery ACTIVE) was the archive candidate
from 23:45Z onward and was skipped. `g_12271913` also stayed CLOSED. Queue
did not go to 3. This is a soak **success** condition.

Steady state now explicitly includes human backpressure:

```text
COLLECT → provision → rotate → CLOSED
  → auto-archive if DROP_ELIGIBLE < 2
  → B2 VERIFIED → DROP_ELIGIBLE
  → queue limit 2 → human DROP → slot opens → archive resumes
```

### DISK

READY target **5,706,473,472 B**. Floor **5 GiB** = 5,368,709,120 B.

| Point | Bytes |
|---|---:|
| Phase A start 18:08:29Z | 5,753,016,320 |
| DROP `free_before` 18:41Z | 5,725,130,752 |
| DROP `free_after` 18:41Z | 5,932,617,728 |
| g_114 resume start 18:45Z | 5,930,749,952 |
| 19:00Z (post-archive, queue=2) | 5,924,474,880 |
| First provision 22:40Z | 5,798,289,408 |
| First rotation 23:30Z | 5,771,771,904 |
| Crossed below READY | between 01:30Z (5,708,378,112) and 01:40Z (5,694,652,416) |
| Second provision 05:30Z | 5,563,064,320 |
| Second rotation 06:20Z | 5,536,673,792 |
| Archive temp-blocked 07:30Z | 5,499,047,936 (`worst_case=5,502,926,848`) |
| Latest 09:23:34Z | **5,430,657,024** (~59 MiB above 5 GiB) |

READY is missed because two extra CLOSED generations (~205.5 + 205.7 MiB) plus
ACTIVE growth remain local while DROP_ELIGIBLE=2 blocks archive. That is
bounded-queue backpressure, not a 5 GiB floor breach.

### CAPACITY_STOP

| Field | Value |
|---|---|
| `CAPACITY_STOP_REQUIRED` | absent |
| Triggered | **no** |
| Collector | still running `on-failure:5` |
| Archive skip from 07:30Z | temp-headroom (`free < floor + 128 MiB`), **not** COLLECT stop |

If free later falls below 5 GiB, expected order remains: provision coverage if
required → persist capacity stop → actually stop COLLECT. Do not override to
keep collecting.

### HEALTHCHECK

`HEALTHCHECK_CODE_PROVEN_OVERLAY_DEPLOYMENT_TEMPORARY`

GHCR digest unchanged:
`sha256:fb1267a99f803dfcc8585a6eeaca61198b8cecb16f7701151bb34a4d05f1bd8e`.
Overlay bind-mount still present. Docker health **healthy**, failing streak 0.
Image pull still deferred (READY already missed; a second ~554 MiB layer would
be unsafe).

| When | ACTIVE child | ACTIVE rows | Execution | CLOSED children |
|---|---|---:|---:|---|
| 18:16:24Z | `g_11871913` | 88,097 | **62.0 ms** | `never executed` |
| 09:21:03Z | `g_12671913` | 187,052 | **204.1 ms** (cold reads) | `g_1107`/`g_1147`/`g_1187`/`g_1227` **never executed** |
| 09:23:34Z | same | — | **231.4 ms** | same pruning |

Still far under the Docker health timeout. Historical children remain
partition-pruned.

### B2_CLASS_B

Readback pattern unchanged: `reuse_completed` / `reuse_local_evidence` then
`upload_builtin_verify`. g_114 07:00 and 08:00 hours: COMPLETED `NoSuchKey`
then upload path — not a second full parquet restore. No Class B cap during
DROP probe or g_114 resume. Integrity gates were not weakened.

### HIBACHI / POSTGRES / QUALITY / EXTERNAL

| Field | Value |
|---|---|
| Hibachi | healthy; RestartCount **0**; started 2026-08-17T17:18:07Z |
| PostgreSQL | healthy |
| QUALITY_GAPS | both registry rows intact; **no new gap**; no synthesize/bridge across the capacity pause or partition incident |
| EXTERNAL_REF | **OFF** |
| ML_STATUS | **BLOCKED** |

### BLOCKERS

None for soak PASS. Factual follow-on pressure (not a soak failure):

1. Free **5,430,657,024 B** is below READY and ~59 MiB above the 5 GiB floor.
   Two CLOSED generations cannot archive while DROP_ELIGIBLE=2.
2. Overlay healthcheck is still temporary (disk cannot hold a second 554 MiB
   image).
3. `g_11071913` remains DROP_ELIGIBLE; a **new** explicit human approval is
   required before any further physical DROP. Auto-DROP stays OFF.

### NEXT

Soak PASS. Do **not** execute the following automatically.

1. Request explicit approval for the already-built **≤15–30 minute** external
   live-offload canary. External-ref stays OFF until that separate approval.
   Disk is currently below READY and close to the 5 GiB floor; that canary
   should not start while Hibachi COLLECT is near the capacity-stop line.
2. Separate human DROP of one verified generation (oldest `g_11071913`,
   evidence `0378d45b37fbe11b2e72f5be127f614712a5151482296796c0ba6b14838618e8`)
   would reopen the archive slot, reclaim ~206 MiB, and allow `g_11871913`
   to archive. That is **not** authorized by this continuation. Do not enable
   auto-DROP.
