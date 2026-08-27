# External live offload canary v1

Milestone: `HIBACHI_RESUME_AND_EXTERNAL_LIVE_OFFLOAD_CANARY`

Date: 2026-08-19 (UTC)

This report supersedes the live-run portion of the 2026-08-12 attempt
(`docs/external_feed_live_offload_canary_v1.md`), which never started
external because Hibachi was partition-unhealthy. That file remains
historical.

## Verdict

**STATUS: `EXTERNAL_CANARY_FAILED`**

Hibachi COLLECT resumed cleanly. The isolated Binance USD-M `ETHUSDT`
live-offload canary started, completed **seven** real
ACTIVE→SEALED→gzip→B2→VERIFY_OK→reclaim cycles, then was **fail-closed**
when Hibachi Docker health became `unhealthy` (single 10s healthcheck
timeout). Inserts never stopped. Restart delta remained 0. External was
stopped; Hibachi was left running and returned to `healthy` on the next
check.

**DECISION: `CANARY_BLOCKED_HIBACHI`**

Do **not** start the 6h quality pilot.

ML_STATUS: **`BLOCKED`**

No physical DROP. Auto-DROP remained OFF. No GHCR 554 MiB pull.

## HIBACHI_RESUME

| Field | Value |
|---|---|
| Preflight UTC | 2026-08-19T18:03:07Z |
| Preflight free | 6,025,629,696 B |
| PostgreSQL | healthy |
| Collector before | Running=false, Health=unhealthy, Restarts=0, policy `no:0`, exit 143 |
| CAPACITY_STOP_REQUIRED | absent at resume (cleared via normal recovery after disk ≥ READY) |
| ACTIVE | `g_13471913_13871913` `[13471913,13871913)` |
| `pg_get_expr` | `FOR VALUES FROM ('13471913') TO ('13871913')` |
| Sequence | `last_value=13682172` `is_called=true` → next **13682173** |
| Successor | `g_13871913_14271913` absent; remaining ~189,740; urgency NORMAL |
| CLOSED / ARCHIVING / DROP_ELIGIBLE | 0 / 0 / 0 |
| External-ref | OFF |
| Resume UTC | 2026-08-19T18:04:13Z start; first event 18:04:20Z |
| Restart policy restored | `on-failure:5` |
| First id | **13682173** (sequence untouched; matches expected) |
| First timestamp | 2026-08-19T18:04:20.698871Z |
| First topic | `orderbook` |
| Routing | `market_events_g_13471913` |
| Restart delta | **0** |
| Overlay healthcheck | retained (no GHCR pull) |

`compose up --no-start --force-recreate --pull never collector` then
`docker update --restart=on-failure:5` and `compose start`. First
persisted id after resume is exactly the next sequence value.

## CAPACITY_GAP

Registry: `docs/quality/hibachi_collection_gaps_v1.json`
`capacity_stop_collection_pause_20260818`

| Field | Value |
|---|---|
| start (exclusive of last pre-stop event) | 2026-08-18T23:10:02.783379Z |
| end (exclusive of first post-resume event) | 2026-08-19T18:04:20.698871Z |
| last pre-gap id | 13682172 |
| first post-gap id | 13682173 |
| duration | 68057.915493 s (~18h 54m 18s) |
| id sequence | contiguous (time-only hole) |
| classification | `CAPACITY_STOP_COLLECTION_PAUSE` |
| synthesize / bridge | false / false |

Do not synthesize RAW or 1s `market_state` across this interval.

## HIBACHI_OBSERVATION

External-ref stayed OFF.

| Field | Value |
|---|---|
| Window | 2026-08-19T18:05:43Z → 18:12:46Z (423 s) |
| Events | 7,022 (ids 13683546 → 13690568 in the sample window) |
| Rate | 996.03 events/min |
| Topics since resume | `orderbook`, `ask_bid_price`, `mark_price`, `spot_price`, `funding_rate_estimation`, `trades` |
| Health | healthy |
| Restarts | 0 |
| Partition miss | 0 |
| PostgreSQL | healthy |
| Disk start / min / end | 6,024,773,632 / 6,020,870,144 / 6,020,870,144 B |
| Provisioning | ACTIVE `g_13471913_13871913`; remaining 184,057; successor absent; urgency NORMAL; 18:10 tick skipped |
| CAPACITY_STOP | absent |

ACTIVE-bounded healthcheck `EXPLAIN ANALYZE` ~335–355 ms (overlay retained).

## CANARY_PREFLIGHT

Exact start threshold: **5,974,908,928 B**.

First attempt (18:15:20Z) built `hibachi-external-ref:live-offload1` from the
already-local digest `sha-8f6ba2317102b215a1fe96d7c252da137a14d699`
(`--pull=false`). The boto3 layer grew the image 554→607 MiB and dropped
free to **5,966,389,248 B** (below target). Watch also could not read the
UID-10001 spool from the deploy user. External was stopped after ~3 s.
Hibachi was not stopped.

Safe reclaim (not a generation DROP): deleted leftover
`/tmp/external-headroom-reclaim` (~54.9 MiB pip/boto3 one-shot HOME from
headroom prep). Image kept. Reused local live-offload image (`SKIP_BUILD=1`).

Retry preflight 2026-08-19T18:23:30Z:

| Field | Value |
|---|---|
| free | **6,027,771,904 B** |
| target | 5,974,908,928 B |
| margin | 52,862,976 B |
| free immediately before start | 6,027,571,200 B |
| Hibachi | healthy, restart delta 0 |
| PostgreSQL | healthy |
| Partition miss | 0 |
| next id | 13701244 covered by ACTIVE |
| Provisioning | NORMAL; successor not due |
| Archive lock / CAPACITY_STOP | absent |
| External | OFF |
| `recover_root` | `{actions: []}` |
| Result | **PASS** — canary started |

## EXTERNAL_CANARY

Isolated Compose profile `external-ref`. Streams: Binance USD-M `ETHUSDT`
`bookTicker` + `aggTrade`. Existing wiring:
`SegmentedExternalSpool` + `AsyncOffloadWorker` + `recover_root`.
16 MiB / 300 s segments. B2 prefix `external/binance_usdm/ETHUSDT/`.
Durable SoT: gzip NDJSON. `restart: "no"`. Mem 128 MiB.

| Field | Value |
|---|---|
| start | 2026-08-19T18:23:42Z |
| operator stop | 2026-08-19T18:34:18Z (Hibachi health `unhealthy`) |
| process end | 2026-08-19T18:34:42.898842Z (`signal_SIGTERM`) |
| duration | 656.3 s (~10.9 min; nominal 15, hard max 30) |
| bookTicker | 165,260 / **251.8 /s** |
| aggTrade | 10,557 / **16.1 /s** |
| messages total | 175,817 |
| raw WS bytes | 28,741,832 (43,794 B/s) |
| spool bytes written | 104,372,514 (~546 MiB/h projected) |
| segments sealed | **7** |
| first bookTicker / aggTrade | true / true |
| reconnects | 1 (`state.tmp` race on segment 000003; WS reconnected) |
| malformed | 0 |

Segment wall times were ~90–100 s (time seal at 300 s was not the limiter;
~16 MiB fill dominated).

## OFFLOAD

| Field | Value |
|---|---|
| sealed | 7 |
| verified (worker) | 7 |
| reclaimed (worker) | 7 |
| gzip bytes | 4,945,716 |
| gzip ratio | 4,945,716 / 104,372,514 = **4.74%** |
| upload throughput | 25.9 MiB/h gzip (≪ ~7 MiB/s VPS↔B2) |
| verification | existing `_verify_remote`: one gzip download + gunzip + SHA-256 (`upload_builtin_verify` analogue). No second full restore. Post-canary used Head/exists only. |
| backlog start | 0 |
| backlog max | 20,464,916 B (~19.5 MiB) |
| backlog end | 17,387 B (audit/state/manifest leftovers) |
| backlog slope | non-growing after each reclaim; ACTION `NONE` |
| worker `segments_failed` | 31 |

The 31 `segments_failed` are a **shutdown race**, not live ingest failure.
Segment `...T183205Z_000006` was verified and reclaimed (B2 `VERIFY_OK`
present). `drain_remaining` then retried it after local `events.ndjson` /
`.gz` were deleted and marked local state `FAILED`. Local FAILED directory
has `reclaim_audit.json` and **no** ndjson/gz. **Do not delete it** (unverified
delete policy); remote SoT is already verified.

All seven remote objects:

| segment | gz | manifest | VERIFY_OK | gzip bytes | events |
|---|---|---|---|---:|---:|
| `...T182349Z_000001` | yes | yes | yes | 796,784 | 28,280 |
| `...T182517Z_000002` | yes | yes | yes | 789,377 | 28,253 |
| `...T182641Z_000003` | yes | yes | yes | 796,763 | 28,248 |
| `...T182817Z_000004` | yes | yes | yes | 798,037 | 28,276 |
| `...T183014Z_000005` | yes | yes | yes | 807,909 | 28,252 |
| `...T183205Z_000006` | yes | yes | yes | 786,854 | 28,263 |
| `...T183408Z_000007` | yes | yes | yes | 169,992 | 6,246 |

000007 is the SIGTERM final seal (short segment), then verified and reclaimed.

`recover_root` after stop: `{actions: []}`. No stranded ACTIVE. No unbounded
temp (`roundtrip_work`, verify tmpdirs). Leftover ~19.7 KiB state/manifest/audit.

## B2

| Field | Value |
|---|---|
| errors (live path) | none on the seven successful verify/reclaim cycles |
| Class B | one builtin gzip download per new segment; no extra full restore/download after canary |
| reuse | `store.exists` skipped re-upload when object already present |
| namespace | `external/binance_usdm/ETHUSDT/<segment_id>/` isolated from Hibachi archive prefixes |
| post-canary | Head/exists only; all seven `events.ndjson.gz`, `manifest.json`, `VERIFY_OK.json` present |

## DISK

| Stage | Free bytes |
|---|---:|
| Pre-resume | 6,025,629,696 |
| Observation min | 6,020,870,144 |
| After leftover `/tmp` pip HOME reclaim | 6,027,771,904 |
| Pre-canary start | 6,027,571,200 |
| Minimum during canary | **6,002,860,032** (status) / 6,003,941,376 (watch) |
| Post-canary | 6,021,238,784 (status after) / 6,020,173,824 (harvest) |

Always above 5 GiB floor and READY `5,706,473,472`. Minimum during canary
remained above the 5,974,908,928 start threshold by ~26.7 MiB.

No generation DROP. Auto-archive policy unchanged (`*/15`,
`--require-normal-floor`, `drop=0`).

## RESOURCES

| Process | RSS | CPU (samples) |
|---|---|---|
| Hibachi collector | ~21–86 MiB / 160 MiB | usually 3–7%; spikes to 84% |
| External-ref | **peak 122,007,552 B (~116.4 MiB) / 128 MiB** | often 65–95% |

External sat close to its 128 MiB ceiling. That is not safe for a 6h pilot
on this host even if Hibachi health is quiet.

## HIBACHI_DURING_CANARY

| Field | Value |
|---|---|
| Event progression | 13,701,430 → 13,711,914 at stop (~10,484 events / 631 s ≈ 997/min, same as Hibachi-only) |
| After external stop | 13,714,409 at 18:36:45Z and still advancing |
| Health | one timeout: `Health check exceeded timeout (10s)` at 18:33:53Z; next check 18:34:33Z healthy |
| Restart delta | **0** |
| Partition miss | 0 |
| PostgreSQL | healthy |
| ACTIVE routing | `g_13471913_13871913` unchanged |
| Provisioning | no extra provision (remaining ≫ 50k) |
| CAPACITY_STOP | absent |
| Auto-DROP | OFF |

The healthcheck overlay (ACTIVE-bounded `MAX(received_at)`) is still bind-mounted.
The timeout coincided with external gzip/B2 verify IO, not with a collector
crash or partition miss.

## POSTSTATE

| Field | Value |
|---|---|
| Hibachi | Running, **healthy**, Restarts=0, policy `on-failure:5` |
| PostgreSQL | healthy |
| external-ref | **OFF** (container removed) |
| Disk | ~6.021 GiB free |
| Lifecycle | ACTIVE `g_13471913_13871913`; 0 CLOSED / 0 ARCHIVING / 0 DROP_ELIGIBLE |
| Overlay image | `hibachi-external-ref:live-offload1` retained locally (607 MiB, no GHCR pull) |
| Spool | 7 segment dirs; 6 `RECLAIMABLE` + 1 local `FAILED` (000006) with B2 `VERIFY_OK`; ~20 KiB |

## Interpretation

Live offload **can** keep up: gzip ~4.7%, B2 verify succeeded for every sealed
segment, local backlog peaked at one ~16 MiB segment and shrank after reclaim.

The canary is **not** quality-pilot-ready:

1. Hibachi Docker health failed closed (10s timeout) while sharing the VPS
   with external CPU/IO. Policy requires stopping external.
2. External RSS peaked at 116 MiB of a 128 MiB cap.
3. Ingest hit a `state.tmp` race once; shutdown `drain_remaining` mis-marked
   an already-reclaimed segment `FAILED`.

## BLOCKERS

1. Hibachi healthcheck exceeded 10s at 2026-08-19T18:33:53Z during live
   offload (FailingStreak cleared; inserts continued; restart delta 0).
2. External RSS 116.4 / 128 MiB — insufficient margin for a 6h quality pilot.
3. Shutdown drain retried reclaimed segment 000006 → local `FAILED` despite
   remote `VERIFY_OK` (do not delete local FAILED).
4. One live `FileNotFoundError` on `state.tmp` for segment 000003 (WS
   reconnect; segment still verified on B2).

## NEXT

Milestone: **`EXTERNAL_OFFLOAD_DRAIN_AND_HEALTHCHECK_FIX`** then a clean
15-minute retry of this same isolated canary.

Technically required before any 6h quality pilot:

1. Keep Hibachi COLLECT running. External stays OFF.
2. Fix drain so already-reclaimed / missing-local+remote-VERIFY_OK segments
   are not flipped to `FAILED`.
3. Fix ACTIVE `state.tmp` vs reclaim race.
4. Decide a Hibachi-safe healthcheck budget under concurrent external IO
   (overlay stays; do not pull a second 554 MiB GHCR image).
5. Retry ≤15–30 min live offload only after those fixes and a fresh free
   read ≥ 5,974,908,928 B. No DROP. No auto-DROP. No ML / PAPER / LIVE.

## ML_STATUS

**BLOCKED**

---

## Retry 2026-08-20 — `EXTERNAL_LIVE_OFFLOAD_CANARY_RETRY`

This section is **additive**. The 19 Aug failed-canary tables above are
unchanged.

Fixes from `docs/external_offload_drain_and_healthcheck_fix_v1.md` are on
the host (overlay healthcheck LATERAL query, drain/state/guard code,
Hibachi `cpu_shares=2048`, external Compose `cpu_shares=256` / `cpus=0.45`).
Spool `recover_root` adopted `…T183205Z_000006` from local `FAILED` to
`RECLAIMABLE`. All seven 19 Aug segments are `RECLAIMABLE` with no bulky
local payload.

**STATUS: `EXTERNAL_CANARY_RETRY_BLOCKED`**

The 15-minute live retry was **not started**.

| Gate | Required | Observed 2026-08-20T09:52:32Z |
|---|---|---|
| Filesystem free | ≥ 5,974,908,928 B | **5,537,943,552 B** |
| Hibachi | healthy, restart delta 0, ids advancing | pass |
| PostgreSQL | healthy | pass |
| Partition miss | 0 | pass |
| CAPACITY_STOP | absent | pass |
| External-ref | OFF before start | pass |
| ACTIVE / successor | covered | `g_14271913_14671913` ACTIVE; `g_14671913_15071913` PROVISIONED |

Two `DROP_ELIGIBLE` generations (`g_13471913`, `g_13871913`) remain on disk
under the cap of 2. That occupancy, plus overnight COLLECT, put free below
both the canary start gate and READY. Operator floor (5 GiB) still holds.
Hibachi COLLECT continues.

**DECISION: `NEEDS_RESOURCE_TUNING`**

RSS ≤105 MiB under live ingest is still unmeasured. Do not raise the 128 MiB
limit. Do not start the 6h quality pilot.

Next: operator-approved DROP of verified `DROP_ELIGIBLE` generation(s) to
restore ≥ 5,974,908,928 B, then the same 15-minute retry. No auto-DROP.

ML_STATUS: **BLOCKED**
