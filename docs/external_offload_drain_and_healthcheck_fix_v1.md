# External offload drain and healthcheck fix v1

Milestone: `EXTERNAL_OFFLOAD_DRAIN_AND_HEALTHCHECK_FIX`

Follow-on: `EXTERNAL_LIVE_OFFLOAD_CANARY_RETRY`

Date: 2026-08-20 (UTC)

This does **not** redesign the proven B2 pipeline (16 MiB gzip NDJSON, builtin
readback verify, reclaim only after `VERIFY_OK`, pressure 128 MiB / stop 192 MiB,
global floor 5 GiB). It fixes the defects that stopped the 2026-08-19 live
canary and the shutdown-state races around it.

## Verdict

**STATUS: `EXTERNAL_CANARY_RETRY_BLOCKED`**

Code, tests, overlay healthcheck, CPU shares, and spool recovery landed.
External stayed **OFF**. The 15-minute retry did **not** start because
filesystem free was **5,535,256,576 B**, below the canary start gate
**5,974,908,928 B**.

**DECISION: `NEEDS_RESOURCE_TUNING`**

Disk, not Hibachi data-path death, blocked the retry. The 105 MiB RSS target
is therefore still unproven under live ingest. Do **not** raise the 128 MiB
limit. Do **not** start the 6h quality pilot.

ML_STATUS: **`BLOCKED`**

No physical DROP. No GHCR pull. No second 554 MiB image.

## Phase 1 — production preflight (2026-08-20T09:52:32Z)

External was not started.

| Field | Value |
|---|---|
| UTC | 2026-08-20T09:52:32Z |
| Filesystem free | **5,537,943,552 B** (~5.16 GiB) |
| Canary start gate | 5,974,908,928 B (**not met**, −437 MiB) |
| READY gate | 5,706,473,472 B (also **not met**, −161 MiB) |
| Operator floor | 5,368,709,120 B (still above, +169 MiB) |
| nproc | **1** |
| loadavg | 0.27 / 0.45 / 0.48 |
| RAM | 1,008,185,344 B total; ~317 MiB available |
| Swap | 2 GiB file, **~603 MiB used** |
| Hibachi | Running, **healthy**, Restarts=**0**, ~33.9 MiB / 160 MiB, ~2.9% CPU |
| PostgreSQL | healthy, ~122 MiB / 256 MiB |
| External-ref | **OFF** |
| CAPACITY_STOP / archive.lock | absent / absent |
| ACTIVE | `g_14271913_14671913` `[14271913,14671913)` |
| next / max id | 14,627,303 / 14,627,302 (advancing) |
| remaining | ~44,610 |
| Successor | `g_14671913_15071913` **PROVISIONED** (lead already covered) |
| Partition miss (30m logs) | 0 |
| CLOSED / ARCHIVING / DROP_ELIGIBLE | 0 / 0 / **2** |
| DROP_ELIGIBLE keys | `g_13471913_13871913`, `g_13871913_14271913` |
| Images | base digest `sha-8f6ba231…` 554 MB local; `hibachi-external-ref:live-offload1` 607 MB local |

Overnight COLLECT plus two `DROP_ELIGIBLE` children still on disk explain the
drop from the 19 Aug ~6.0 GiB post-canary free figure. Auto-DROP stayed OFF
(`DROP_BACKLOG_LIMIT` = 2).

## Phase 3 — 2026-08-19T18:33:53Z timeout (best-supported cause)

Not a Hibachi insert failure. Evidence from the failed canary:

| Signal | At timeout |
|---|---|
| PROCESS_LIVE | yes (container Running, restart delta 0) |
| DATA_PROGRESS | yes (ids 13,701,430 → 13,711,914 during canary; continued after stop) |
| POSTGRES_LIVE | healthy |
| PARTITION_COVERED | yes (no miss; ACTIVE child valid) |
| DOCKER_HEALTH | one probe **exceeded 10s**; next check 18:34:33Z healthy |
| CAPACITY_SAFE | yes |

The host is **1 vCPU**. External gzip/B2 sat at **65–95% CPU**. The collector
healthcheck SQL was `SELECT MAX(m.received_at)` over the whole ACTIVE child
(~200k+ rows). Idle EXPLAIN was hundreds of milliseconds; under gzip+verify
contention the same scan exceeded Docker's **10s** timeout.

Primary cause: **CPU starvation of the healthcheck process** by external
gzip/B2 on a single vCPU, amplified by an **O(n) freshness scan**. Secondary:
IO/CPU overlap during segment verify. Not DB deadlock, not partition miss,
not a silent insert stall.

Calling it a "false positive" is incomplete: Docker's probe really timed out.
It was not equivalent to `HIBACHI_DATA_PATH_DOWN`.

## Phases 4–5 — health semantics

Distinct signals (watch + `classify_hibachi_guard`):

| Signal | Meaning |
|---|---|
| PROCESS_LIVE | collector container Running |
| DATA_PROGRESS | Hibachi `MAX(id)` advanced since last 30s sample |
| POSTGRES_LIVE | Postgres Docker health = healthy |
| PARTITION_COVERED | next id inside ACTIVE range and no partition-miss logs |
| DOCKER_HEALTH | Docker probe status |
| CAPACITY_SAFE | no `CAPACITY_STOP_REQUIRED` |

Immediate external STOP (hard):

- process dead
- PostgreSQL unhealthy
- partition uncovered / miss
- capacity STOP
- two consecutive samples with **no** id advance (~60s at ~1000 events/min)

Docker-only degradation:

- one `unhealthy`/`timeout` sample **and** live ids → `HIBACHI_HEALTH_TRANSIENT` (observe)
- `docker_unhealthy_streak >= 2` (~60s) → STOP `HIBACHI_DOCKER_UNHEALTHY_SUSTAINED`

Docker probe interval is 30s with 10s timeout and 3 retries, so the first
`unhealthy` inspect already aggregates failed probes. The retry watch still
requires a **second** sample before aborting, because the 19 Aug incident
recovered on the next check while inserts continued.

## Phase 6 — CPU isolation

| Actor | Setting | Applied |
|---|---|---|
| Host | 1 vCPU | observed |
| Hibachi collector | `cpu_shares: 2048` (Compose) | **live** `docker update --cpu-shares 2048` (was 0/default 1024); no recreate |
| External-ref | `cpu_shares: 256`, `cpus: 0.45` | Compose overlay; not started this retry |
| gzip | `compresslevel=4` (still streaming 1 MiB chunks) | code; SoT remains gzip NDJSON |
| Ingest | no per-event spool `rglob` | code |

Hibachi stays preferred. External CPU quota is not applied until the retry
actually starts. Backlog-vs-quota remains a live measurement for the next
15-minute run.

## Phase 7 — cheap healthcheck

Overlay `~/gen-cycle/overlay-emergency/trading_bot/healthcheck.py` now uses
ACTIVE-bounded `JOIN LATERAL … ORDER BY m.id DESC LIMIT 1` (PK backward
lookup). It does **not** `MAX(received_at)` over the child and does **not**
scan historical parents.

Docker healthcheck timeout stays **10s**. Freshness window stays 120s.
No collector restart. Next probe uses the bind-mounted file.

No separate heartbeat file: latest persisted `id` in the ACTIVE range **is**
the persist signal.

## Phases 8–10 — drain, 000006, atomic state

Shutdown order in `ExternalRuntime`:

1. cancel ingest / watchdog / status
2. `spool.close()` (seal ACTIVE)
3. `shutdown_drain` (single owner; wait in-flight; bounded 35s; **does not fabricate FAILED**)
4. cancel worker task
5. `recover_root` (idempotent)

`AsyncOffloadWorker.process_one` is per-process locked. Remote `VERIFY_OK` +
local reclaim (`reclaim_audit.json` / `verify_ok.json` / remote objects) maps
to `RECLAIMABLE`, never `FAILED` because ndjson/gz are gone.

**Live proof:** overlay `recover_root` on the existing spool adopted
`…T183205Z_000006` `FAILED` → `RECLAIMABLE` (`err=None`). All seven canary
segments are now `RECLAIMABLE` with no local bulky payload.

`write_state` uses a unique `.state.{pid}.{ns}.tmp`, per-segment lock, fsync,
`os.replace`. Shared `state.tmp` is gone. `recover_root` deletes stale temps.

## Phases 11–12 — memory

Limit stays **128 MiB**. Target for the next live retry is peak RSS **≤105 MiB**
(prefer ≤100). Previous peak **122,007,552 B (~116.4 MiB)**.

Reductions (not a limit bump):

- gzip default level 4 (CPU; streaming already file→gzip file)
- metrics latency samples 50k → 2048
- seal recover reads last 1 MiB tail, not the whole 16 MiB
- B2 `upload_file`/`download_file` single-thread TransferConfig
- botocore `max_pool_connections=4`
- websocket `max_msg_size` 64 KiB; connector limit 4
- no per-event spool tree walk
- local `verify_ok.json` so drain need not re-read bulky files

Whether this clears 105 MiB is **unmeasured** until a live retry. If it does
not, keep `NEEDS_RESOURCE_TUNING` and do not raise `mem_limit`.

## Phase 13 — tests

`pytest`, `ruff check .`, `mypy src migrations` clean after the changes.

Coverage includes: transient Docker + live ids; sustained Docker/stale ids;
Postgres / partition / capacity STOP; 000006 remote-VERIFY_OK not FAILED;
worker double-process; concurrent `write_state`; idempotent `recover_root`;
no re-upload when remote exists.

## Phase 14 — deploy

| Action | Result |
|---|---|
| GHCR pull | **not done** |
| Image rebuild / pip layer | **not done** |
| Overlay tar | 948,224 B |
| Healthcheck bind-mount | LATERAL SQL live for next probe |
| External code overlay | `$BUILD/trading_bot` bind-mount path |
| Free delta | 5,535,596,544 → 5,535,510,528 B (~86 KiB) |
| Collector recreate | **not done** |
| External start | **not done** |

## Phase 15–17 — retry not started

Soak + 15-minute canary require free ≥ 5,974,908,928 B. Preflight missed that
gate, so the retry script was **not** launched.

## NEXT

`CANARY_HEADROOM_DROP_THEN_RETRY`: operator-approved physical DROP of one or
both verified `DROP_ELIGIBLE` generations (`g_13471913`, `g_13871913`) after
B2 Class B integrity, restore free ≥ 5,974,908,928 B, 5-minute Hibachi-only
soak, then the same 15-minute isolated canary (guard + CPU isolation + overlay).

Do not auto-DROP. Do not start a 6h quality pilot from this milestone.
