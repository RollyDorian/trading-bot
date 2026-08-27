# External feed offload design v1

Milestone: `EXTERNAL_FEED_OFFLOAD_DESIGN_AND_PROOF`

Date: 2026-08-11 (UTC)

## Verdict

**STATUS: `EXTERNAL_OFFLOAD_READY`**

Offline proof on the preserved 128 MiB Binance canary spool succeeded:
deterministic 16 MiB segments → gzip durable archive → local + B2 verify/readback
→ reclaim eligibility. Hibachi collector/Postgres remained healthy and unchanged.
Original canary NDJSON at `/tmp/ext_canary.ndjson` was **not** deleted.

ML_STATUS remains **`BLOCKED`**. No lead-lag analysis. No 6–24h quality pilot.

## Architecture isolation

External feed stays independent of Hibachi:

- no writes to PostgreSQL / `market_events`
- separate B2 prefix: `external/binance_usdm/ETHUSDT/...`
- B2/upload failure fails closed for external only
- Hibachi generations, DROP, and private APIs untouched

Package: `trading_bot.external_market_data.offload`

Operator CLI: `hibachi-external-offload` (`status`, `split`, `compress-report`, `offload`)

## Segment lifecycle state machine

```text
WS receive (cheap append)
    → ACTIVE (events.active.ndjson)
    → seal (rename + hash) → SEALED_UNVERIFIED
    → gzip + manifest → UPLOADING
    → immutable B2 put (no overwrite)
    → download/hash/count/manifest gate → VERIFIED_REMOTE
    → RECLAIMABLE
    → delete only that segment's bulky local files (audit kept)

FAILED: upload/verify error; local files retained; never auto-deleted
```

Rules:

- never upload an ACTIVE append file
- never delete ACTIVE / FAILED / SEALED_UNVERIFIED
- never wildcard-delete
- never circular-overwrite / drop-oldest
- retries reuse exact segment identity; existing remote objects are reconciled, not overwritten

## Segment identity and format

Identity example:

`binance_usdm_ETHUSDT_<UTC_start>_<seq6>`

Boundary (chosen from measured ~358 MiB/h):

- **16 MiB RAW** per sealed segment (≈2.7 minutes at measured rate)
- also time-capable (default writer supports max_seconds)

Local staging: append-safe NDJSON.

Crash truncation: keep through last `\n`; drop only trailing partial record
(`recover_trailing_partial_ndjson`).

## Compression decision (real canary)

Source: 134,217,695 bytes NDJSON; 225,734 events.

| Artifact | Bytes | Ratio vs RAW | Notes |
|---|---:|---:|---|
| RAW NDJSON | 134,217,695 | 1.00 | durable semantic source before archive |
| gzip NDJSON (full spool) | 6,463,519 | **0.048** | ~4.8%; ~1.9s; RSS Δ ~2.4 MiB |
| Parquet zstd (16 MiB segment) | 708,161 / 16,777,769 | **0.042** | required-field round-trip OK |
| Parquet (50k-line sample) | 1,221,271 / 29,693,250 | 0.041 | 50k events; round-trip equal |

**Decision:** durable SoT = **gzip NDJSON** (exact RAW envelopes). Parquet is an
optional analytical projection with proven required-field / timestamp round-trip.
Do both when useful for research; do not replace gzip SoT with Parquet alone.

Projected sustained archived volume at canary rate:

- RAW ~358 MiB/h → gzip ~**17.2 MiB/h** (~0.40 GiB/day) at 4.8% ratio.

## B2 namespace

```text
external/binance_usdm/ETHUSDT/<segment_id>/events.ndjson.gz
external/binance_usdm/ETHUSDT/<segment_id>/manifest.json
external/binance_usdm/ETHUSDT/<segment_id>/VERIFY_OK.json
external/binance_usdm/_throughput/...   # bounded probes only
```

Separate from Hibachi generation/window identities. No overwrite.

## Integrity gate (before RECLAIMABLE)

1. local sealed content SHA-256
2. remote object exists
3. remote size matches
4. downloaded gzip SHA-256 matches
5. gunzip restore count + content hash match
6. manifest consistency (counts + content hash)
7. required-field spot-check on restore

## Backlog / capacity policy

Measured ingest: ~358 MiB/h RAW ≈ 6 MiB/min.

Default external local footprint budget (active + sealed + convert/verify temp):

| Threshold | Bytes | Action |
|---|---:|---|
| NORMAL | < 128 MiB | `NONE` |
| OFFLOAD_PRESSURE | ≥ 128 MiB | `OFFLOAD_PRESSURE` |
| EXTERNAL_STOP | ≥ 192 MiB **or** free ≤ 5 GiB + 1 GiB margin | `EXTERNAL_STOP_REQUIRED` |

No deletion under pressure. No drop-oldest. External stops fail-closed; Hibachi continues.

At ~17 MiB/h gzip egress vs ~7 MiB/s VPS↔B2 path, offload has large headroom if
local seal/gzip keeps pace (segment gzip ≪ 1s for 16 MiB).

## Throughput proof (VPS)

Segment-sized gzip (~787 KiB):

- upload ≈ **7.12 MiB/s**
- download ≈ **7.29 MiB/s**
- size match: true

End-to-end offload (compress+upload+verify) for one 16 MiB segment on B2:
~3.5s core work (plus one-time pip in proof harness).

Required sustained gzip ingress ≈ 17 MiB/h ≈ 0.005 MiB/s ≪ 7 MiB/s.

**Backlog trend expectation:** shrinking/stable when offloader runs continuously.

## Recovery semantics

| Case | Behavior |
|---|---|
| A ACTIVE + partial trailing line | trim partial; keep complete records |
| B sealed before convert | resume from SEALED_UNVERIFIED |
| C mid-convert | redo gzip into same paths |
| D uploaded before verify | reconcile exists; verify download |
| E verified before reclaim | mark/keep RECLAIMABLE; reclaim idempotent |
| F after local delete | state stays RECLAIMABLE; no other file deletion |

## Operator status

Read-only: `hibachi-external-offload status --root <segments_root>`

Sample fields: EXTERNAL, ACTIVE, SEALED_UNVERIFIED, UPLOADING, VERIFIED_REMOTE,
LOCAL_RECLAIMABLE, LOCAL_TOTAL_MIB, B2, INGEST_RATE, OFFLOAD_RATE, BACKLOG_TREND,
FILESYSTEM (free/floor/margin), ACTION.

## Live offload canary

**Not executed** in this milestone.

Reasons (factual):

1. Offline lifecycle proof already complete on real canary bytes.
2. VPS free space after proof cleanup ≈ **5.5–5.9 GiB**, i.e. inside the
   configured 1 GiB margin above the 5 GiB floor → live ingest would start under
   `EXTERNAL_STOP_REQUIRED` filesystem policy.
3. Production external runtime still uses monolithic spool; wiring ACTIVE segment
   writer + async offloader into the live collector is the next integration step,
   not required to prove storage lifecycle offline.

## Tests

`tests/test_external_offload.py` covers boundaries, seal, partial recovery,
manifest/hash, gzip+parquet round-trip, verify/reclaim gates, B2 outage→FAILED,
backlog stop, crash/resume, connection provenance, timestamps, status, immutable
identity.

## Proof artifacts

Local copies under `docs/external_offload_proof/`:

- `split_report.json`
- `compression_report.json`
- `segment_full_report.json`
- `offload_local_report.json`
- `b2_throughput_report.json`
- `offload_b2_report.json`
- `status_sample.json`

VPS canary spool preserved: `/tmp/ext_canary.ndjson` (134,217,695 bytes).

## NEXT (do not execute automatically)

If human approves after disk margin is healthy **and** live segment+offloader
integration is deployed:

`request separate human approval for a bounded 6–24h external-feed quality pilot using the proven offload lifecycle`
