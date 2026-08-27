# Hibachi partition recovery v1

STATUS: **HIBACHI_PARTITION_RECOVERY_PASS**

Milestone: `HIBACHI_PARTITION_RECOVERY_AND_PROVISIONING_ROOT_CAUSE`

External reference collector: **OFF / unchanged** throughout.

ML_STATUS: **BLOCKED**

Prior external live-offload semantic correction: no live external offload run
occurred; treat earlier `QUALITY_PILOT_OFFLOAD_UNSTABLE` as
`LIVE_OFFLOAD_NOT_EVALUATED_DUE_TO_HIBACHI_P0` until a live canary actually runs.

## Incident timeline (UTC)

| Time (UTC) | Event |
|---|---|
| 2026-08-11T16:00:01Z | Automation `PROVISIONED g_8271913_8671913` at remaining≈44065 (50k lead working). |
| 2026-08-11T16:50:01Z | `ROTATED` → ACTIVE `g_8271913_8671913`. |
| 2026-08-11T23:40:02Z | Automation `PROVISIONED g_8671913_9071913` at remaining≈48287. |
| 2026-08-12T00:30:01Z | `ROTATED` → ACTIVE `g_8671913_9071913`; `closed_n` becomes **2**. |
| 2026-08-12T00:40:02Z→09:10:01Z | Every 10m tick: `STOP_REQUIRED backlog … closed=2` then **exit before provision**. |
| 2026-08-12T07:11:33.499641Z | Last persisted RAW id **9071912** (`received_at`). |
| 2026-08-12T07:11:33Z+ | First insert failures: `no partition of relation "market_events" found for row` for id **9071913** (ACTIVE upper bound). |
| ~07:11–09:14 | Collector remains running **unhealthy**; failed INSERTs continue allocating sequence values. |
| 2026-08-12T09:14:25Z | Recovery DDL: create/attach `market_events_g_9071913` `[9071913,9471913)`, metadata rotate ACTIVE. |
| 2026-08-12T09:14:35Z | Minimal Hibachi collector restart; health → **healthy**. |
| 2026-08-12T09:14:40.860289Z | First persisted id in successor: **9072164**. |
| 2026-08-12T09:19:37Z | Fixed maintain script deployed; status file written; cron still every 10m. |

## Evidence before mutation

* PostgreSQL: healthy.
* Disk free ≈ 5.87 GiB (floor 5 GiB unchanged).
* External-ref: OFF / absent.
* Sequence before recovery DDL: `last_value=9072163`, `is_called=true` → next **9072164**.
* Max persisted RAW id: **9071912**.
* Attached children:

  * `market_events_g_7871913` `[7871913,8271913)`
  * `market_events_g_8271913` `[8271913,8671913)`
  * `market_events_g_8671913` `[8671913,9071913)`
* Metadata ACTIVE: `g_8671913_9071913` `[8671913,9071913)`.
* Orphan physical successor for `9071913`: **none**.
* Cron: `*/10 * * * * hibachi-generation-ops-tick.sh` → `hibachi-generation-maintain.sh`.

## EXPECTED_SUCCESSOR (derived, not assumed)

From ACTIVE upper bound **9071913**, failed id **9071913**, sequence next ≥ bound, and 400k span arithmetic matching prior generations:

* name: `market_events_g_9071913`
* key: `g_9071913_9471913`
* range: `[9071913,9471913)`

Hypothesis matched production evidence → provisioned. No `PARTITION_RANGE_AMBIGUOUS`.

## Recovery

* Method: exact empty child `CREATE TABLE … PARTITION OF market_events FOR VALUES FROM (9071913) TO (9471913)` + metadata insert `PROVISIONED` → rotate exhausted ACTIVE to `CLOSED_UNARCHIVED` and activate successor.
* No DEFAULT partition, no DROP, no DELETE RAW, no B2 mutation, no sequence reset.
* DDL locking: brief parent attach of an empty child (observed inserts resumed immediately after).

## SEQUENCE / allocated-not-persisted gap

| Metric | Value |
|---|---|
| Last persisted before failure | **9071912** @ 2026-08-12T07:11:33.499641Z |
| Sequence before recovery | **9072163** (`is_called=true`) |
| First persisted after recovery | **9072164** @ 2026-08-12T09:14:40.860289Z |
| Gap class | `ALLOCATED_NOT_PERSISTED_DURING_PARTITION_FAILURE` |
| Gap ids | **9071913–9072163** inclusive (**251** ids) |

Do not reset the sequence. Research quality tools must treat this as an incident allocation gap, not deleted market events.

## COLLECTION_GAP

* Start: 2026-08-12T07:11:33.499641Z (last good row)
* End: 2026-08-12T09:14:40.860289Z (first recovered row)
* Duration: **≈ 2h 3m 7s**
* Missing market data cannot be reconstructed; mark as a production collection gap for future quality masks.

## HIBACHI collector

* Before: running, **unhealthy**, restarts=0, started 2026-08-11T10:04:44Z
* After: **healthy**, restarted 2026-08-12T09:14:35Z
* Topics observed in successor (payload topic): `orderbook`, `ask_bid_price`, `mark_price`, `spot_price`, `funding_rate_estimation`, `trades`
* source=`hibachi_ws` only

## ROOT_CAUSE

Exact mechanism:

1. Host cron maintain (`~/gen-cycle/hibachi-generation-maintain.sh`) successfully provisioned successors **twice** under the 50k lead (`g_8271913_*`, `g_8671913_*`).
2. After the second rotation, **two** `CLOSED_UNARCHIVED` generations remained (archive backlog not cleared).
3. The maintain script treated `closed_n > 1` as hard `STOP_REQUIRED` and **`exit 3` before the provision block**.
4. When ACTIVE `g_8671913_9071913` entered the 50k lead window, provision never ran.
5. Sequence crossed `9071913` → PostgreSQL rejected inserts with partition-miss; sequence still advanced on failed INSERT attempts.

Same class of bug existed in repo `scripts/generation_maintain.py`: capacity `STOP_REQUIRED` refused all maintenance mutations, including successor provision.

This was a **regression after successful automation**, not “automation never ran”.

## AUTOMATION_FIX

1. **Host** `scripts/hibachi_generation_maintain.sh` (deployed to production gen-cycle):
   * Order: assess → **provision (≤50k)** → rotate → then report capacity/backlog STOP.
   * Urgency: `NORMAL` / `PROVISION_REQUIRED` (50k) / `PROVISION_LATE` (10k) / `COVER_STOP_REQUIRED` (≤1k or past bound without cover).
   * Persist `~/gen-cycle/provision.status.env` every tick (next id, remaining, successor expected/exists, last attempt/error, action_required).
   * On `COVER_STOP_REQUIRED` with missing successor: deliberately stop Hibachi collector rather than leave INSERT partition-miss noise.
   * Idempotent already-present successor; fail closed on orphan physical child.
2. **Repo** `scripts/generation_maintain.py`: always run `maintain_writable_generations` even under capacity STOP; exit 3 still signals capacity pressure after cover maintenance.
3. **Repo** operator status: provision urgency outranks capacity STOP; surfaces expected successor, next id, urgency, last attempt fields.

Threshold rationale (~59k events/h): 50k≈51m, 10k≈10m, 1k≈1m to recover from a transient failed CREATE before id exhaustion.

## SCHEDULER proof (production)

* Mechanism: user crontab of `trading-deploy`
* Cadence: `*/10 * * * *` → `hibachi-generation-ops-tick.sh` → `hibachi-generation-maintain.sh`
* Archive tick: `*/15 * * * *` (separate; does not DROP)
* Enabled: yes (cron present; tick executed at 09:19:37Z after deploy)
* Privileges: deploy user + Docker group; SQL as Compose postgres superuser inside container (same path as prior ticks)
* Exit 3 after fix with `closed=3` is **expected capacity advisory**; provision path already ran (skipped: remaining≫50k)

## FUTURE_BOUNDARY forecast

| Field | Value |
|---|---|
| ACTIVE | `g_9071913_9471913` / `market_events_g_9071913` |
| Range | `[9071913, 9471913)` |
| Next expected successor | `market_events_g_9471913` / `g_9471913_9871913` / `[9471913, 9871913)` |
| 50k provision trigger | when `next_id >= 9421913` (remaining ≤ 50_000) |
| Late trigger | remaining ≤ 10_000 (`next_id >= 9461913`) |
| Cover stop | remaining ≤ 1_000 without successor |

## Disk / capacity note

* Free after recovery ≈ 5.9 GiB; floor remains 5 GiB.
* Empty successor creation was cheap.
* Three `CLOSED_UNARCHIVED` generations remain → capacity `STOP_REQUIRED` backlog advisory. Archive/DROP is a **separate** milestone; not performed here.

## External pipeline

* `external-ref = OFF` unchanged.
* Do **not** auto-start live offload canary from this milestone.

## Tests

* Unit: urgency thresholds; missing-successor outranks capacity STOP; existing operating-model/partition tests.
* Integration assertion updated for cover-stop visibility under low disk.
* Final local gates: pytest / ruff / mypy (clean).

## NEXT (do not execute automatically)

If recovery PASS (this report):

`re-run only the already-approved ≤15–30 minute external live-offload canary; do not start the 6–24h quality pilot`

Also separately prioritize archive backlog reduction so capacity STOP clears without relying on human DROP during the next lead windows.
