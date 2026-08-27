# Hibachi archive backlog recovery v1

STATUS: **HIBACHI_ARCHIVE_BACKLOG_RECOVERY_BLOCKED**

Milestone: `HIBACHI_ARCHIVE_BACKLOG_RECOVERY`

Primary blocker: **`ARCHIVE_CAPACITY_BLOCKED`**

Filesystem free is **below the 5 GiB operator emergency floor**. The proven
generation-archive path refuses to start windows in that state. This milestone
does not authorize physical DROP, RAW DELETE, or lowering the floor. Archive
was therefore not started.

External reference collector: **OFF / unchanged**.

ML_STATUS: **BLOCKED**

## PRESTATE (production, UTC 2026-08-14T08:25:29Z unless noted)

| Field | Value |
|---|---|
| Hibachi | running; healthcheck flapped **unhealthy→healthy** (10s `MAX(received_at)` timeout under disk pressure; inserts continued) |
| PostgreSQL | healthy |
| Disk | **4.235 GiB** free before housekeeping; **4.284 GiB** after unused-image prune |
| Floor | 5 GiB (not lowered) |
| Shortfall | **≈768 MiB** |
| Sequence / next id | `last_value=11885372`, `is_called=true` → next **11885373** |
| ACTIVE | `g_11871913_12271913` `[11871913,12271913)` |
| ACTIVE rows (08:29Z) | 17_288 |
| Ids remaining | ≈391_937 (50k lead at `next_id >= 12221913`) |
| Successor | `g_12271913_12671913` **not yet provisioned** (lead not reached) |
| CLOSED_UNARCHIVED | **10** |
| DROP_ELIGIBLE | **0** |
| DROPPED | `g_7471913_7871913` (prior milestone) |
| Archive job | not running; `ARCHIVE_REQUIRED` marker only |
| External-ref | OFF |

Prompt-time snapshot (`closed=3`, ~5.86 GiB) is **stale**. Collection continued
for ~47 h after partition recovery. Six additional generations closed. Disk
fell through the floor.

## CLOSED_GENERATIONS (exact)

All except `g_9071913` are full 400_000 contiguous persisted ids. None have B2
generation evidence or `archive_evidence_sha256`. Auto-archive tick has only
written `ARCHIVE_REQUIRED` since 2026-08-11T17:00Z (never uploaded).

| generation | range | rows | min/max id | received_at min/max (UTC) | size | state | B2 evidence |
|---|---|---|---|---|---|---|---|
| `g_7871913_8271913` | `[7871913,8271913)` | 400000 | 7871913–8271912 | 2026-08-10 21:08:34 → 2026-08-11 16:44:23 | 195 MiB | CLOSED_UNARCHIVED | none |
| `g_8271913_8671913` | `[8271913,8671913)` | 400000 | 8271913–8671912 | 2026-08-11 16:44:23 → 2026-08-12 00:28:39 | 196 MiB | CLOSED_UNARCHIVED | none |
| `g_8671913_9071913` | `[8671913,9071913)` | 400000 | 8671913–9071912 | 2026-08-12 00:28:39 → 2026-08-12 07:11:33 | 196 MiB | CLOSED_UNARCHIVED | none |
| `g_9071913_9471913` | `[9071913,9471913)` | **399749** | **9072164–9471912** | 2026-08-12 09:14:40 → 2026-08-12 15:56:22 | 200 MiB | CLOSED_UNARCHIVED | none |
| `g_9471913_9871913` | `[9471913,9871913)` | 400000 | 9471913–9871912 | 2026-08-12 15:56:22 → 2026-08-12 22:38:50 | 196 MiB | CLOSED_UNARCHIVED | none |
| `g_9871913_10271913` | `[9871913,10271913)` | 400000 | 9871913–10271912 | 2026-08-12 22:38:50 → 2026-08-13 05:21:23 | 195 MiB | CLOSED_UNARCHIVED | none |
| `g_10271913_10671913` | `[10271913,10671913)` | 400000 | 10271913–10671912 | 2026-08-13 05:21:23 → 2026-08-13 12:03:59 | 193 MiB | CLOSED_UNARCHIVED | none |
| `g_10671913_11071913` | `[10671913,11071913)` | 400000 | 10671913–11071912 | 2026-08-13 12:03:59 → 2026-08-13 18:46:17 | 198 MiB | CLOSED_UNARCHIVED | none |
| `g_11071913_11471913` | `[11071913,11471913)` | 400000 | 11071913–11471912 | 2026-08-13 18:46:17 → 2026-08-14 01:28:58 | 197 MiB | CLOSED_UNARCHIVED | none |
| `g_11471913_11871913` | `[11471913,11871913)` | 400000 | 11471913–11871912 | 2026-08-14 01:28:58 → 2026-08-14 08:11:56 | 195 MiB | CLOSED_UNARCHIVED | none |

`g_9071913` is the partition-incident generation: 251 allocated-not-persisted
ids `9071913..9072163` at the lower bound. Persisted coverage is contiguous
from `9072164`. Do not treat that hole as deleted market events.

Oldest candidate hourly windows (`g_7871913`, max-rows 100k sufficient):

| hour (UTC) | rows |
|---|---|
| 2026-08-10 21:00 | 2828 |
| 2026-08-11 10:00 | 54823 |
| 2026-08-11 11:00–15:00 | 59619–59693 each |
| 2026-08-11 16:00 | 44100 |

Eight windows. The empty hours 22:00Z–09:00Z are the gen-1 DROP stop (collector
was stopped), not missing ids. Dense hours remain under the 100k default /
200k hard cap.

## Derived safe target (actual evidence ≠ prompt `closed=3`)

Operating policy: `CLOSED* <= 1` and `DROP_ELIGIBLE <= 2`.

With **10** `CLOSED_UNARCHIVED` and **0** `DROP_ELIGIBLE`, one non-destructive
milestone can archive **at most two** oldest generations (DROP_ELIGIBLE cap).
That would leave `CLOSED=8`, `DROP_ELIGIBLE=2` — still outside `CLOSED<=1`.

Restoring `CLOSED<=1` requires later human DROP cycles (archive 2 → DROP 2 →
repeat). This milestone does not DROP.

Even if those two were already DROP_ELIGIBLE, reclaim ≈195+196 MiB would move
free from 4.28 GiB to ≈4.66 GiB — **still below 5 GiB**. About **four**
generation DROPs (≈768 MiB) are needed to recover the floor from current free.

## ARCHIVE_ACTIONS

| generation | windows | status |
|---|---|---|
| none | — | **not started** (`ARCHIVE_CAPACITY_BLOCKED`) |

No B2 Hibachi generation objects were created or mutated. No restore ran.

Local unused Docker layers only (not RAW, not B2):

* dangling images + unused `hibachi-external-ref:live-offload1` + unused
  `amneziavpn/amneziawg-go`
* apparent reclaim ≈55 MiB (shared layers); collector/postgres/amnezia-awg2
  images retained
* collector healthcheck returned **healthy** after prune
* temporary-space peak for archive: **n/a** (not started)

## Why auto-archive did not reduce `closed=10`

`*/15` cron runs `hibachi-auto-archive-tick.sh`. That script is **marker-only**:
it writes `ARCHIVE_REQUIRED` and chowns `/work`, then exits. Comment in the
tick: “full window export remains the proven oneshot.”

The proven oneshot (`hibachi-archive-resume.sh`) was never generalized off
`g_7471913`, requires collector **stopped**, and fail-closes below 5 GiB.

So the 50k provision/rotate fix kept creating successors (good), while archive
never uploaded, and closed generations stacked until disk crossed the floor.

## Coverage / provisioning (fix remains active)

Scheduler: `*/10 * * * * hibachi-generation-ops-tick.sh` enabled.

Recent ticks (disk already below floor) still provisioned then rotated:

* `PROVISIONED g_10671913_11071913` … `ROTATED` → `g_10271913`
* `PROVISIONED g_11071913_11471913` … `ROTATED` → `g_10671913`
* `PROVISIONED g_11471913_11871913` … `ROTATED` → `g_11071913`
* `PROVISIONED g_11871913_12271913` … `ROTATED old=g_11471913_11871913` at
  2026-08-14T08:20:01Z with `remaining=-8063`, `successor_exists=1`, then
  `STOP_REQUIRED disk_below_floor`

`provision.status.env` at 08:20Z: urgency `NORMAL`, remaining 391937 after
rotate, `action_required=capacity_disk`. Coverage outranked capacity STOP as
designed. Next 50k trigger: `next_id >= 12221913`. Empty successor CREATE
remains cheap; do not skip it when the lead arrives.

Hibachi restart delta this milestone: **0**. Started 2026-08-12T09:14:35Z.
Event progression continues into `market_events_g_11871913`. Partition-miss
errors in `docker logs --tail` are **historical** (pre-recovery). Healthcheck
timeouts were `MAX(received_at)` over 11 id-partitions exceeding 10s, not a
missing partition.

## INCIDENT_GAP

Committed registry: `docs/quality/hibachi_collection_gaps_v1.json`

Loader: `trading_bot.research.collection_gaps`

| Field | Value |
|---|---|
| time | `2026-08-12T07:11:33.499641Z` → `2026-08-12T09:14:40.860289Z` |
| ids | `9071913..9072163` (251) |
| class | `ALLOCATED_NOT_PERSISTED_DURING_PARTITION_FAILURE` |
| synthesize | **false** |
| bridge_normalization | **false** |

`g_9071913` archive (future) must reconcile `observed=399749` against persisted
min/max `9072164..9471912`, with this gap documented separately — not filled.

## DROP_APPROVAL_CANDIDATES

**none** — no generation is `DROP_ELIGIBLE`. Do not DROP.

## EXTERNAL_CANARY_READINESS

**`EXTERNAL_CANARY_STILL_BLOCKED`**

Hibachi is collecting and PostgreSQL is healthy, but: disk below 5 GiB;
`CLOSED*=10` outside policy; no archive job should start; capacity
`STOP_REQUIRED` is active (advisory, coverage still maintained).

## BLOCKERS

1. **`ARCHIVE_CAPACITY_BLOCKED`**: free 4.284 GiB < 5 GiB floor. Proven archive
   oneshot fail-closes. Archive temp (local bundle + verify download,
   `3 GiB + 2×64 MiB` operational gate) would drive free further below the
   operator floor.
2. **Deadlock vs DROP**: DROP requires DROP_ELIGIBLE; DROP_ELIGIBLE requires
   verified archive; archive requires ≥5 GiB; reclaim to ≥5 GiB requires DROP
   of ~4 generations. This milestone cannot break that loop.
3. Auto-archive tick does not execute the proven B2 export (marker only).

Not blockers: successor arithmetic, 50k-lead ordering (proven on this host),
external-ref (OFF), B2 credentials (not exercised).

## ACTION REQUIRED (human)

To resume archive without lowering the 5 GiB **operator** floor, one of:

1. **Exception (preferred next archive authorization):** allow Hibachi
   generation archive while free is **above the 3 GiB archive operational
   floor** and below 5 GiB, one generation at a time, per-window temp cleanup,
   collector stays running. Then request DROP of the two verified generations.
   Repeat until free ≥5 GiB. Four DROPs are the expected reclaim to the floor.
2. **Other non-RAW reclaim** large enough to restore 5 GiB (not identified on
   this host after unused-image prune).

Do not start the external live-offload canary until backlog/disk policy holds
and no archive job is active.

## Tests

* `tests/test_collection_gaps.py` — incident identity, 251-id hole, no
  synthesize/bridge.
* No archive-code rewrite (none required; the fail-closed 5 GiB gate is the
  correct behavior).
