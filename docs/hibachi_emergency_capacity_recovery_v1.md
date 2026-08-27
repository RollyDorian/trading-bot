# Hibachi emergency capacity recovery v1

STATUS: **HIBACHI_EMERGENCY_CAPACITY_RECOVERY_PASS**

Milestone: `HIBACHI_EMERGENCY_CAPACITY_RECOVERY`

Continuation after Backblaze B2 Class B cap (2026-08-17). Class B was
**probed**, not assumed reset by elapsed time. Hibachi COLLECT resumed only
after filesystem free reached the normal READY target. External-ref stayed
**OFF**. Physical DROP after resume returned to human-approved only.

ML_STATUS: **BLOCKED**

## PRESTATE (production, UTC 2026-08-14T08:42:15Z)

| Field | Value |
|---|---|
| Hibachi | running; `restart=on-failure:5`; started 2026-08-12T09:14:35Z |
| PostgreSQL | healthy |
| Disk | **4,592,459,776 B (~4.277 GiB)** |
| Floor | 5 GiB (unchanged) |
| Sequence | after pause **11902151 / is_called=true** |
| ACTIVE | `g_11871913_12271913` `[11871913,12271913)` |
| CLOSED_UNARCHIVED | **10** (`g_7871913` … `g_11471913`) |
| DROP_ELIGIBLE | **0** |
| Successor | `g_12271913_12671913` not yet provisioned |
| External-ref | OFF |

## COLLECT_PAUSE (`CAPACITY_RECOVERY_COLLECTION_PAUSE`)

Authorized stop of **collector only**. PostgreSQL left up. External-ref not started.

| Field | Value |
|---|---|
| Stop | **2026-08-14T08:42:22Z** |
| Hold file | `~/gen-cycle/COLLECT_HOLD` |
| Restart policy during pause | `docker update --restart=no` then `compose stop collector` |
| Last persisted id | **11902151** |
| Last `received_at` | **2026-08-14T08:42:22.972216Z** |
| Quiescence | `max(id) = seq.last_value = 11902151` |
| Collector after stop | `Running=false` |
| PostgreSQL after stop | healthy |
| ACTIVE after stop | still attached `g_11871913_12271913` |

This was an intentional operational pause, not a collector crash. Ids across
the pause were not synthesized. Sequence resumed at **11902152**.

## EMERGENCY_POLICY

| Policy | Value |
|---|---|
| Normal operator floor | **5 GiB** (unchanged, not redefined) |
| Emergency archive floor | **3 GiB**, only while `COLLECT_HOLD` is present |
| Temp-space rule | `free - 2×64 MiB > floor` before every window |
| Window max-rows | 100,000 (hard cap 200,000, not raised) |
| Concurrency | one generation; lock `~/gen-cycle/archive.lock` |
| Physical DROP | gated `drop_verified_market_event_generation(..., 'DROP_VERIFIED_GENERATION', true)` only with `--drop` |
| Auto-archive DROP | **never** (`hibachi-auto-archive-tick.sh` has no `--drop`) |
| COLLECT resume target | `5 GiB + 203,546,624 B + 128 MiB` = **5,706,473,472 B (~5.315 GiB)** |

## Phase 2 — B2 Class B probe (2026-08-17)

Do not assume the cap reset. Smallest Class B check: GetObject of an already
COMPLETED marker (not parquet) for
`eth-usdt-p_20260812T150000000000Z_20260812T160000000000Z_v2`.

| Field | Value |
|---|---|
| `B2_CLASS_B_AVAILABLE` | **yes** |
| Probe | 1028-byte COMPLETED marker GetObject |
| Result | readable; archive/DROP resumed |

If the probe had failed, status would have remained
`HIBACHI_EMERGENCY_CAPACITY_RECOVERY_BLOCKED` / `B2_CAP_STILL_BLOCKED`.

## B2_CAP operational lesson

Observable evidence (not account-console speculation):

1. Failure text on 2026-08-14:
   `Cannot download file, download bandwidth or transaction (Class B) cap exceeded`.
2. Independent `verify_restore_archive` **after** `archive-export-window`
   (which already download-verifies + restore-validates) doubled GetObject
   volume per window.
3. HeadObject 403s were the same cap, less specific.
4. Continuation conserved Class B:
   - `reuse_local_evidence` for already-verified local JSON (no download);
   - `upload_builtin_verify` as the single required readback on new uploads.
5. Exact Caps & Alerts counters were not exposed by the existing tooling;
   no extra Class B listing calls were added to harvest them.

Recommendation for normal continuous archive (integrity gates unchanged):

- Keep one required independent verification path per window; do not add a
  second full restore after upload-builtin verify.
- Reuse local verified evidence and remote COMPLETED markers.
- Raising the B2 Class B allowance is advisable if unattended `*/15`
  archive of ~7–9 windows/generation must coexist with collector growth.
- Do **not** skip restore verification to save Class B.

## RESUME_SOURCE `g_9471913_9871913`

Prior state `ARCHIVE_FAILED` (3/8 windows). Resumed; did not restart.

| Window (UTC 2026-08-12) | Continuation mode | Notes |
|---|---|---|
| 15:00 | `reuse_local_evidence` | COMPLETED marker already on B2; JSON not overwritten |
| 16:00 | `reuse_local_evidence` | prior `export_upload` JSON reused |
| 17:00 | `reuse_local_evidence` | prior `export_upload` JSON reused |
| 18:00–22:00 | `export_upload` / `upload_builtin_verify` | five new windows |

Datasets for the three reused hours:

- `eth-usdt-p_20260812T150000000000Z_20260812T160000000000Z_v2`
- `eth-usdt-p_20260812T160000000000Z_20260812T170000000000Z_v2`
- `eth-usdt-p_20260812T170000000000Z_20260812T180000000000Z_v2`

## RECOVERY_CYCLES

Oldest-first. DROP identity is DB min/max/count via
`drop_verified_market_event_generation`.

| generation | rows | windows | reused COMPLETED/local | B2 verify | evidence SHA-256 | DROP | physical before | free before | free after | observed reclaim |
|---|---:|---:|---:|---|---|---|---:|---:|---:|---:|
| `g_7871913_8271913` | 400000 | 8 | 2 | PASS | `01e8336a8856db84a3ee8d0634ed8b2bc393da96a10b6ffdc7da975e2a5ce2a4` | DROPPED | 204718080 | 4598829056 | 4803584000 | 204754944 |
| `g_8271913_8671913` | 400000 | 9 | 1 | PASS | `0d32a53199253f2952b2f990b10b15b861a8539dd52695420b704516d9887a9d` | DROPPED | 205094912 | 4803014656 | 5008130048 | 205115392 |
| `g_8671913_9071913` | 400000 | 8 | 1 | PASS | `5ec85a15be0a6c1d4e4c135d8f18d4f31376ef488fbfb256544c91462c6b648c` | DROPPED | 205692928 | 4999233536 | 5204930560 | 205697024 |
| `g_9071913_9471913` | **399749** | 7 | 0 | PASS | `9a8699bb0ae62ec82936d68331eaeac6b9083f6f810fe7797aa5e7f40455fbf2` | DROPPED | 209231872 | 5204484096 | 5413736448 | 209252352 |
| `g_9471913_9871913` | 400000 | 8 | 3 local + 1 COMPLETED | PASS | `b8c376c5f47cbba6db4e4d781ae618afb583a3f336c9563504672953f6a0e81f` | DROPPED | 205471744 | 5175300096 | 5380780032 | 205479936 |
| `g_9871913_10271913` | 400000 | 8 | 1 | PASS | `73a661fa6c83576ff45464e7a7a82af6a24f54064f67b48346789849b03bcbb6` | DROPPED | 204259328 | 5380321280 | 5584605184 | 204283904 |
| `g_10271913_10671913` | 400000 | 8 | 1 | PASS | `c48d449aa65c0ad84286965caebbd773166cbeed618dbb174ae159f55faadf33` | DROPPED | 202686464 | 5584171008 | 5786877952 | 202706944 |

Loop stopped at 2026-08-17T17:01:57Z:
`NORMAL_READY_TARGET_REACHED free=5786873856`.

No further emergency DROP. Residual CLOSED backlog is for auto-archive
(no DROP).

## INCIDENT_GENERATION `g_9071913`

Reconciled as persisted **399749** rows, min **9072164**, max **9471912**.
Allocated-not-persisted ids **9071913..9072163** (251) were **not**
synthesized. Then DROPPED after verified B2 evidence.

## DISK

| Point | Bytes | GiB |
|---|---:|---:|
| Initial (pause 2026-08-14) | 4,592,459,776 | 4.277 |
| After `g_9071913` DROP (blocker) | 5,413,044,224 | 5.041 |
| Continuation loop start 2026-08-17T16:29:17Z | 5,184,077,824 | 4.828 |
| After `g_9471913` DROP | 5,380,780,032 | 5.011 |
| After `g_9871913` DROP | 5,584,605,184 | 5.201 |
| After `g_10271913` DROP / READY | **5,786,873,856** | **5.390** |
| Normal READY target | **5,706,473,472** | **5.315** |
| After COLLECT resume + first auto-archive (17:45Z) | 5,765,820,416 | 5.370 |

Free crossed READY after the **seventh** verified DROP (`g_10271913`).
COLLECT was not resumed merely because free exceeded 5 GiB.

## HEALTHCHECK_IMAGE

Live GHCR digest remains
`sha256:fb1267a99f803dfcc8585a6eeaca61198b8cecb16f7701151bb34a4d05f1bd8e`
(~554 MiB). READY margin after DROP was **~74 MiB**. Pulling a second copy
would have put free below READY even after pruning the old digest.

Deployed path: bind-mount
`~/gen-cycle/overlay-emergency/trading_bot/healthcheck.py` over
`/usr/local/lib/python3.13/site-packages/trading_bot/healthcheck.py`
via `compose.collector-healthcheck.yaml`. Image `ACTIVE_RECEIPT_SQL`
absent (`False`); overlay present.

Bounded-query proof (collector still stopped, 2026-08-17T17:13:47Z):

| Query | Execution | Partitions executed |
|---|---|---|
| ACTIVE-bounded `MAX(m.received_at)` JOIN generations | **18.8 ms** | only `market_events_g_11871913` (CLOSED children `never executed`) |
| Parent-wide `MAX(received_at)` | **1305 ms** | all four hot children; 23655 disk reads |

Overlay sidecar healthcheck while stopped: exit **1** (stale, last receipt
2026-08-14T08:42:22.972216Z). After resume: Docker health **healthy**.
Liveness threshold remains 120 s; not weakened.

## CAPACITY_STOP

Host `hibachi-generation-maintain.sh`: provision 50k lead first; if
`free < 5 GiB` write `CAPACITY_STOP_REQUIRED` and
`docker update --restart=no` + `compose stop collector`. Archive backlog is
`ARCHIVE_BACKLOG` and does not keep COLLECT stopped after disk READY.

Proof: collector remained `Running=false` for the entire pause; maintain
ticks did not restart it. After resume, restart policy is `on-failure:5`
and the stop file is absent while free ≥ 5 GiB.

A syntax error (`id=$(compose ps -q postgres))`) in the overlay compose
helper blocked maintain from 17:18Z–17:44Z. Fixed the same day; `bash -n`
passes; one tick at 17:44:19Z wrote `STATUS=ok` without stopping COLLECT.

## AUTO_ARCHIVE

| Field | Value |
|---|---|
| Mechanism | crontab `*/15 * * * * hibachi-auto-archive-tick.sh` |
| Command | `hibachi_emergency_archive_one.sh --require-normal-floor` |
| DROP | **not passed** (`drop_requested=0`) |
| During hold | ticks skipped (`collect_hold_present`) |
| Live proof | 2026-08-17T17:30:01Z selected oldest CLOSED `g_10671913_11071913` |

`g_10671913` result: 400000 rows, 7 windows, B2 PASS, evidence
`dccd3292a6059b6eec400dfffbac833a73c5766b07358dfd96bced3ba1766050`,
state **DROP_ELIGIBLE**, `skip_physical_drop`. Collector stayed healthy
(restart count 0). The 17:45 tick then began the next oldest
(`g_11071913`) one generation at a time.

## COLLECT_RESUME

**Yes.** All Phase 16 gates passed.

| Field | Value |
|---|---|
| Collector start | 2026-08-17T17:18:07Z |
| First persisted id | **11902152** |
| First `received_at` | **2026-08-17T17:18:16.226260Z** |
| Routing | `market_events_g_11871913` |
| Restart policy | `on-failure:5` |
| External-ref | OFF |

## RECOVERY_GAP

| Field | Value |
|---|---|
| Classification | `CAPACITY_RECOVERY_COLLECTION_PAUSE` |
| Last id before | 11902151 |
| First id after | 11902152 (contiguous; time-only hole) |
| Start (exclusive of last event) | 2026-08-14T08:42:22.972217Z |
| End (exclusive first event) | 2026-08-17T17:18:16.226260Z |
| Duration | **290153.254044 s** (~3 d 8 h 35 m 53 s) |
| Registry | `docs/quality/hibachi_collection_gaps_v1.json` |
| Synthesize / bridge | **false** |

## POST-RESUME OBSERVATION (~14 min, 17:32:39Z)

| Field | Value |
|---|---|
| Hibachi | running, health **healthy**, restart count **0** |
| Topics | `orderbook`, `ask_bid_price`, `mark_price`, `spot_price`, `funding_rate_estimation`, `trades` |
| New rows | 14318 by 17:32Z (~61k/h, in family of the 59k/h design) |
| Partition errors | none |
| PostgreSQL | healthy |
| Disk | 5,725,310,976 B then recovered to ~5.37 GiB after archive temp cleanup; still ≥ READY and 5 GiB |

## POSTSTATE (2026-08-17T17:45:09Z)

| Field | Value |
|---|---|
| ACTIVE | `g_11871913_12271913` `[11871913,12271913)` attached |
| next id | **11928897** (growing) |
| Successor | not provisioned (remaining ≫ 50k; trigger `next_id >= 12221913`) |
| DROP_ELIGIBLE | **1** (`g_10671913_11071913`) — no auto-DROP |
| ARCHIVING | **1** (`g_11071913_11471913`, auto-archive tick) |
| CLOSED_UNARCHIVED | **1** (`g_11471913_11871913`) |
| ARCHIVE_FAILED | **0** |
| DROPPED this milestone | 7 (`g_7871913` … `g_10271913`) plus prior `g_7471913` |
| Collector | running, `on-failure:5`, healthcheck overlay mounted |
| PostgreSQL | healthy |
| External-ref | OFF |
| `COLLECT_HOLD` | absent |
| `CAPACITY_STOP_REQUIRED` | absent |

## HIBACHI / POSTGRES / PROVISIONING

| Field | Value |
|---|---|
| Hibachi | healthy; restart delta this continuation = 0 |
| PostgreSQL | healthy throughout |
| Coverage | ACTIVE covers current next id |
| Next successor | `g_12271913_12671913` at `next_id >= 12221913` |
| Scheduler | `*/10` `hibachi-generation-ops-tick.sh` (provision/rotate then capacity stop) |

## BLOCKERS

None remaining for this emergency recovery.

Resolved during continuation:

1. B2 Class B cap — probed available, then archive resumed.
2. READY shortfall — closed by three verified DROPs.
3. Healthcheck SQL not in GHCR image — overlay bind-mount (image pull unsafe).
4. Maintain syntax error after overlay compose helper — fixed 17:44Z.

## NEXT

Observe normal Hibachi storage lifecycle long enough to prove CLOSED
generations no longer silently accumulate before returning to the external
live-offload canary.

Do **not** execute that NEXT automatically. External-ref remains OFF.
Future physical DROP still requires human approval and evidence.
