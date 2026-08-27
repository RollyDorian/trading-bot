# External feed live offload canary v1

Milestone: `EXTERNAL_FEED_LIVE_OFFLOAD_CANARY_AND_HEADROOM`

Date: 2026-08-12 (UTC)

## Verdict

**STATUS: `EXTERNAL_LIVE_OFFLOAD_BLOCKED`**

Live 15–30 minute offload canary was **not started**.

P0 preflight failure: Hibachi collector is **unhealthy** with fail-closed PostgreSQL
writes due to missing `market_events` partition for ids around `907212x`
(`CheckViolationError: no partition of relation "market_events" found for row`).
PostgreSQL itself remains healthy. This is outside the external-feed failure domain
and must not be “fixed” by restarting Hibachi from this milestone without a separate
partition provision/rotate approval.

Live wiring + tests + headroom reclaim completed. External service remains **OFF**.

ML_STATUS: **`BLOCKED`**

QUALITY_PILOT_DECISION: **`LIVE_OFFLOAD_NOT_EVALUATED_DUE_TO_HIBACHI_P0`**
(legacy report label `QUALITY_PILOT_OFFLOAD_UNSTABLE` did not mean proven
offloader instability — no live offload canary ran. Re-evaluate only after a
real live offload observation.)

Post-note: Hibachi partition recovery later PASSED
(`docs/hibachi_partition_recovery_v1.md`); this canary report remains
historical for the BLOCKED attempt.

## Headroom

| Stage | Free |
|---|---|
| Before housekeeping | ~5.18 GiB |
| After canary B2 archive + image rm + journal vacuum + spool clear | **~5.87 GiB** |
| Safe (≥5.75 GiB preferred) | **yes** |

Actions taken (safe only):

1. Uploaded independent durable copy of 128 MiB canary to B2:
   - `external/binance_usdm/_canary_evidence/ext_canary_20260811.ndjson.gz`
   - raw SHA-256 `e8ee989acd14211c5ae6b7253ca52804fd4e99a0983e07580385bf88c9f9ad67`
   - gzip SHA-256 `7dd7c05845451dfbdb19f5ffa8bc8a4bff46ce6633fc1ba8a2b47743082422ec`
   - verify JSON retained under `~/external-ref-canary-evidence/canary_b2_verify.json`
2. Deleted local `/tmp/ext_canary.ndjson` only after B2 verify (root).
3. Removed unused `hibachi-external-ref:canary1` image (production digest retained).
4. Vacuumed archived journals (~332 MiB).
5. Cleared old monolithic spool file from `external-ref-spool` (UID 10001).

5 GiB floor unchanged. No Hibachi/PG/VPN/B2 production deletes.

## Live wiring (code complete)

Implemented in-process (Compose option A):

- `SegmentedExternalSpool` — ACTIVE `events.active.ndjson`, seal at 16 MiB
- `AsyncOffloadWorker` — filesystem discovery of sealed states; thread offload;
  auto-reclaim after verify
- Runtime: recovery before ingest, capacity policy (pressure 128 / stop 192 MiB,
  floor 5 GiB + 512 MiB margin), burst peaks (1s/10s), operator status loop
- Durable SoT remains **gzip NDJSON** (no Parquet on hot path)
- Compose: profile `external-ref`, default OFF, `restart: "no"`, mem 128 MiB
- Image build recipe: `docker/Dockerfile.external-live-offload` (+ boto3)

CLI: `hibachi-external-offload status|split|compress-report|offload`

## Tests

`tests/test_external_live_offload.py` + prior offload tests:

- active writer seal/next ACTIVE
- worker handoff + reclaim-after-verify
- concurrent producer/offloader
- backlog includes temp files
- upload failure → FAILED, no reclaim
- capacity stop
- restart partial-record recovery
- B2 outage simulation (unit)

Final local: pytest / ruff / mypy clean for external packages.

## Canary

| Field | Value |
|---|---|
| Executed | **no** |
| Reason | Hibachi collector unhealthy (partition missing) — P0 |
| Image built | `hibachi-external-ref:live-offload1` (built, unused for run) |
| External | remains OFF |

## B2 outage simulation

Covered by unit test `test_upload_failure_keeps_failed_no_reclaim` /
`test_b2_outage_leaves_failed_and_no_delete`: FAILED retained, no delete,
Hibachi domain untouched.

## Hibachi / Postgres (preflight snapshot)

- Postgres: **healthy**
- Collector: **unhealthy** since `2026-08-11T10:04:44Z` (restart delta: none during this work)
- Error: no partition for `market_events` id ≈ 9072120+
- External work did **not** restart Hibachi

## Operator status sample shape

See `hibachi-external-offload status` / runtime `operator_status` fields:
EXTERNAL, ACTIVE, SEALED_UNVERIFIED, UPLOADING, FAILED, VERIFIED_REMOTE,
LOCAL_RECLAIMABLE, LOCAL_TOTAL_BYTES, INGEST/OFFLOAD rates, BACKLOG_TREND,
B2, FILESYSTEM, ACTION.

## BLOCKERS (factual)

1. Hibachi collector cannot insert into `market_events` (partition range exhausted /
   successor not ACTIVE) — separate generation lifecycle approval required.
2. Live offload canary therefore not executed; concurrent producer+B2 proof still pending.
3. (Resolved) Disk headroom was below prefer band; now ~5.87 GiB free.

## NEXT (do not execute automatically)

1. Separately approve Hibachi generation provision/rotate so collector returns healthy.
2. Re-run only the minimal live offload preflight + ≤15–30m canary.
3. Only if that canary PASSes: request separate human approval for a bounded
   6–24h external-feed quality pilot.
