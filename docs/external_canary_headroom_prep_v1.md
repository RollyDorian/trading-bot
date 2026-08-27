# External canary headroom prep v1

STATUS: **EXTERNAL_CANARY_HEADROOM_READY**

Milestone: `EXTERNAL_CANARY_HEADROOM_PREP`

Parent soak: `HIBACHI_STORAGE_LIFECYCLE_SOAK_PASS`.

Filesystem free reached **6,050,516,992 B** at 2026-08-19T12:21:21Z, which is
above the pre-canary target **5,974,908,928 B** (margin **75,608,064 B**).
Physical DROP stopped immediately after that measurement. External-ref was
**not** started. ML stayed **BLOCKED**. Auto-DROP stayed **OFF**.

Hibachi COLLECT is **stopped** from the 5 GiB capacity STOP that fired
overnight while two verified DROP_ELIGIBLE generations were still local.
That stop was respected (restart policy `no:0`; no resume in this milestone).
Disk recovery does not close the time-only collection gap.

## TARGET

| Reserve | Bytes |
|---|---:|
| Hibachi READY | 5,706,473,472 |
| External local stop cap | 201,326,592 (192 MiB) |
| Transient safety | 67,108,864 (64 MiB) |
| **Pre-canary minimum** | **5,974,908,928** (~5.565 GiB) |

5 GiB remains the Hibachi COLLECT floor. This milestone does **not** treat
READY alone as enough.

## PRESTATE (2026-08-18T10:31:10Z)

Do not use the soak 09:23Z snapshot.

| Field | Value |
|---|---|
| Collector | running, healthy, RestartCount **0**, `on-failure:5`, started 2026-08-17T17:18:07Z |
| PostgreSQL | healthy |
| Free | **5,394,903,040 B** (below target; shortfall 580,005,888 B) |
| ACTIVE | `g_12671913_13071913` `[12671913,13071913)` |
| `next_id` | 12927355 (provision.status 10:30Z); live max 12928503 |
| Remaining | 144558 |
| Successor | `g_13071913_13471913` **absent** (remaining ≫ 50k; correct) |
| DROP_ELIGIBLE | `g_11071913_11471913`, `g_11471913_11871913` |
| CLOSED_UNARCHIVED | `g_11871913_12271913`, `g_12271913_12671913` |
| ARCHIVING / ARCHIVE_FAILED | 0 |
| Archive lock | absent |
| Cron | `*/10` generation-ops; `*/15` auto-archive `--require-normal-floor` `drop=0` |
| External-ref | OFF |
| `CAPACITY_STOP_REQUIRED` | absent |
| Archive tick | `EMERGENCY_ARCHIVE_CAPACITY_BLOCKED` (`free < 5 GiB + 128 MiB`) |

`HEADROOM_ALREADY_READY` did not apply.

## DROP_CYCLES

Every DROP used `SELECT public.drop_verified_market_event_generation(..., 'DROP_VERIFIED_GENERATION', true)`.
No `DROP TABLE`, no DELETE, no sequence reset. Full 64-hex evidence hashes only.
B2 probe before each DROP was the COMPLETED marker (not parquet). Parent
`market_events` remained partitioned. ACTIVE was never the DROP target.

### 1. `g_11071913_11471913`

| Field | Value |
|---|---|
| Bounds | metadata + `pg_get_expr` `[11071913,11471913)` |
| Rows | 400000; min 11071913; max 11471912 |
| Evidence SHA-256 | `0378d45b37fbe11b2e72f5be127f614712a5151482296796c0ba6b14838618e8` |
| B2 / restore | 8/8 windows verified; 1 `reuse_completed` + 7 `upload_builtin_verify`; window `restore_status=verified` |
| Physical | 206,274,560 |
| When | 2026-08-18T10:33:24Z–10:33:48Z |
| `free_before` / `free_after` | 5,393,432,576 / 5,599,715,328 |
| Reported / observed reclaim | 206,274,560 / **206,282,752** |
| Result | DROPPED; child absent; ACTIVE `g_12671913_13071913`; queue 2→1 |

### 2. `g_11471913_11871913`

| Field | Value |
|---|---|
| Bounds | `[11471913,11871913)` |
| Rows | 400000; 11471913–11871912 |
| Evidence SHA-256 | `f960f7f4b9de182149111d605ad96111d613cae8034740ee944e914521ad8129` |
| B2 / restore | 8/8 verified; 1 `reuse_completed` + 7 `upload_builtin_verify` |
| Physical | 204,316,672 |
| When | 2026-08-18T10:34:28Z |
| `free_before` / `free_after` | 5,599,092,736 / 5,803,421,696 |
| Reported / observed reclaim | 204,316,672 / **204,328,960** |
| Result | DROPPED; queue 1→0; still **171,487,232 B** short of target |

After this DROP, oldest CLOSED `g_11871913` was eligible for no-DROP archive
(queue 0; free above archive temp worst-case).

### 3. `g_11871913_12271913`

10:45 cron archived this generation (`drop_requested=0`, 8 windows, 1
`reuse_completed` for the 08:00 hour that already existed as a **time-window**
object from `g_114`). Export is `received_at` + symbol, not generation id.
At `g_114` archive time both generations' 08:00 rows were in PostgreSQL, so
that COMPLETED object is a time-hour superset that includes `g_118` ids
`11871913–11902151` (30,239 rows). Independent restore of that object already
ran during the 10:45 job. No second parquet download.

| Field | Value |
|---|---|
| Bounds | `[11871913,12271913)` |
| Rows | 400000; 11871913–12271912 |
| Evidence SHA-256 | `6a86a7889af71a7ca8e2f364aa2992b2880f58ec4f5e4387baa03351814cc675` |
| B2 / restore | 8/8 verified; 1 `reuse_completed` + 7 `export_upload`/`upload_builtin_verify` |
| Physical | 205,545,472 |
| When | 2026-08-19T11:42:47Z (after capacity STOP; collector already stopped) |
| `free_before` / `free_after` | 5,237,284,864 / 5,442,854,912 |
| Reported / observed reclaim | 205,545,472 / **205,570,048** |
| Result | DROPPED; disk back **above 5 GiB**; still short of target |

### 4. `g_12271913_12671913`

11:00 cron archived it (`drop_requested=0`) while this milestone's SSH wait
was disconnected. Evidence was already `DROP_ELIGIBLE` at 11:12:08Z.

| Field | Value |
|---|---|
| Bounds | `[12271913,12671913)` |
| Rows | 400000; 12271913–12671912 |
| Evidence SHA-256 | `96eca2c95323f6f7777382d584437d763a9558622bb3666cfd0a6c28b6ac216e` |
| B2 / restore | 8/8 verified; 1 `reuse_completed` (23:00 shared with `g_118`) + 7 upload |
| Physical | 205,668,352 |
| When | 2026-08-19T11:43:19Z |
| `free_before` / `free_after` | 5,442,830,336 / 5,648,506,880 |
| Reported / observed reclaim | 205,668,352 / **205,676,544** |
| Result | DROPPED; queue 0; still **326,402,048 B** short |

### 5. `g_12671913_13071913`

11:45 cron, `drop_requested=0`, 7 hour buckets, 1 `reuse_completed` (06:00
shared with `g_122`).

| Field | Value |
|---|---|
| Bounds | `[12671913,13071913)` |
| Rows | 400000; 12671913–13071912 |
| Evidence SHA-256 | `4b6e4911e892c95bad3ad6a640ea34299bd4cebac4b9d04d77b320abdfffd891` |
| B2 / restore | 7/7 verified |
| Physical | 204,103,680 |
| When | 2026-08-19T11:54:25Z |
| `free_before` / `free_after` | 5,641,469,952 / 5,845,598,208 |
| Reported / observed reclaim | 204,103,680 / **204,128,256** |
| Result | DROPPED; **above Hibachi READY**; still **129,310,720 B** short of canary target |

### 6. `g_13071913_13471913` (final)

12:00 cron, `drop_requested=0`, 8 windows, 1 `reuse_completed` (12:00 shared
with `g_126`).

| Field | Value |
|---|---|
| Bounds | `[13071913,13471913)` |
| Rows | 400000; 13071913–13471912 |
| Evidence SHA-256 | `d2cb3e9203ca546424407d9a6adf617d8cfd2c54a470a4fbefbf0c8831964b5e` |
| B2 / restore | 8/8 verified |
| Physical | 205,742,080 |
| When | 2026-08-19T12:20:00Z |
| `free_before` / `free_after` | 5,844,779,008 / **6,050,545,664** |
| Reported / observed reclaim | 205,742,080 / **205,766,656** |
| Result | DROPPED; **target reached**; no further DROP |

Authorization exhausted at target. `g_13471913` ACTIVE was not DROPped.
No leftover CLOSED/DROP_ELIGIBLE.

## AUTO_ARCHIVE

Executor unchanged: `*/15` `hibachi-auto-archive-tick.sh` →
`hibachi_emergency_archive_one.sh --require-normal-floor`, **`drop_requested=0`**.

| Tick (UTC) | Generation | Result |
|---|---|---|
| 10:45 2026-08-18 | `g_11871913` | archive → DROP_ELIGIBLE 10:57:20Z; 8 windows; reused 1 |
| 11:00 2026-08-18 | `g_12271913` | archive → DROP_ELIGIBLE 11:12:08Z; 8 windows; reused 1 |
| 11:15–11:30 2026-08-18 | — | `no_archive_candidate` while COLLECT continued |
| overnight | — | `DROP_BACKLOG_LIMIT` then `EMERGENCY_ARCHIVE_CAPACITY_BLOCKED` |
| 11:45 2026-08-19 | `g_12671913` | archive → DROP_ELIGIBLE 11:53:52Z; 7 windows; reused 1 |
| 12:00 2026-08-19 | `g_13071913` | archive → DROP_ELIGIBLE 12:18:52Z; 8 windows; reused 1 |

No physical DROP from cron.

## Overnight capacity STOP (respected)

After `g_114` DROP, the planned next human DROP was `g_118` as soon as it
became DROP_ELIGIBLE. The SSH wait for that archive disconnected. COLLECT
kept running and completed two more natural 50k provisions/rotations:

| Event | Maintain tick | `next_id` / remaining (tick) |
|---|---|---|
| PROVISIONED `g_13071913_13471913` | 2026-08-18T12:10:01Z | 50k lead |
| ROTATED `g_12671913` → `g_13071913` | 2026-08-18T13:00:01Z | |
| PROVISIONED `g_13471913_13871913` | 2026-08-18T18:50:01Z | |
| ROTATED `g_13071913` → `g_13471913` | 2026-08-18T19:40:01Z | |

At **2026-08-18T23:10:01Z** free **5,367,803,904 B** (< 5 GiB). Maintain
persisted `CAPACITY_STOP_REQUIRED`, `compose stop collector` (exit 143),
restart policy `no:0`. Last persisted event **id 13682172** at
**2026-08-18T23:10:02.783378Z**. RestartCount stayed **0**. No partition-miss.

`CAPACITY_STOP_REQUIRED` was later cleared automatically once free ≥ READY
(after `g_126`/`g_130` DROPs). Collector was **not** restarted.

## DISK

| Point | Bytes |
|---|---:|
| Initial 10:31:10Z 2026-08-18 | 5,394,903,040 |
| After `g_110` | 5,599,715,328 |
| After `g_114` | 5,803,421,696 |
| `g_118` archive temp min (10:56Z) | ~5,754,241,024 |
| Capacity STOP 23:10:01Z | 5,367,803,904 |
| Re-read 11:39:36Z 2026-08-19 (minimum this continuation) | **5,237,420,032** |
| After `g_118` | 5,442,854,912 |
| After `g_122` | 5,648,506,880 |
| After `g_126` | 5,845,598,208 |
| After `g_130` / final 12:21:21Z | **6,050,516,992** |
| Target | 5,974,908,928 |
| Final margin | **75,608,064** |

## HIBACHI

| Field | Value |
|---|---|
| At prestate | healthy; restart delta 0 |
| After 23:10Z | **stopped**; health unhealthy because exited; RestartCount **0**; policy `no:0` |
| Event progression | last id **13682172** at 2026-08-18T23:10:02.783378Z (frozen) |
| Partition errors | **none** (`NO_PARTITION_MISS=0`) |
| ACTIVE now | `g_13471913_13871913` `[13471913,13871913)` attached; 106,389,504 B; ids through 13682172 |

## POSTGRES

Healthy throughout every DROP and archive.

## PROVISIONING

| Field | Value |
|---|---|
| ACTIVE | `g_13471913_13871913` `[13471913,13871913)` |
| Successor expected | `g_13871913_14271913` `[13871913,14271913)` |
| Successor exists | **no** (`remaining=189740` ≫ 50k; correct while COLLECT is stopped) |
| Urgency | NORMAL |
| 50k / 10k / ≤1k paths | no regression; two additional natural cycles ran overnight before STOP |

## HEALTHCHECK

Overlay bind-mount still present
(`~/gen-cycle/overlay-emergency/trading_bot/healthcheck.py`).
GHCR digest unchanged. Collector image was not pulled. Docker health is
unhealthy only because the container is stopped.

## QUALITY_GAPS

New open time-only outage recorded:

`docs/quality/hibachi_collection_gaps_v1.json`
`capacity_stop_collection_pause_20260818`

`end_utc` is **null** until an explicit COLLECT resume. Do not synthesize or
bridge. Prior partition-incident and 2026-08-14 pause rows unchanged.

## EXTERNAL_REF

**OFF**. Canary not executed.

## ML_STATUS

**BLOCKED**.

## BLOCKERS

None for the disk target. Remaining before any external canary:

1. Hibachi COLLECT is stopped (`restart=no`). Resume is a **separate**
   explicit approval. Next expected id **13682173**. Close the open quality
   gap after first resumed row.
2. External live-offload canary must not start until COLLECT is healthy again
   **and** free is re-read ≥ 5,974,908,928 B (margin is only ~72 MiB; ACTIVE
   growth after resume will consume it).
3. Overlay healthcheck is still temporary. Do not pull a 554 MiB image unless
   a fresh disk preflight shows transient headroom.

## NEXT

Do **not** execute automatically.

1. Request explicit approval to **resume Hibachi COLLECT** (restore
   `on-failure:5`, start collector, confirm first id 13682173, close the open
   gap). Do not enable auto-DROP.
2. After COLLECT is healthy, re-read free bytes. If still ≥ 5,974,908,928 B,
   request/confirm the separate **≤15–30 minute** external live-offload canary
   start. Do not start it from this milestone.
3. If resume would put free back below the canary target, request one more
   verified DROP only after a new inventory — authorization for this milestone
   is exhausted at the target already reached.
