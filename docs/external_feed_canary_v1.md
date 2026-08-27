# External reference feed — technical canary report v1

STATUS: `EXTERNAL_CAPACITY_STOP`

ML_STATUS: `BLOCKED`

QUALITY_PILOT_CAPACITY: `REQUIRES_OFFLOAD_DESIGN`

(Also: **insufficient** for 6–24h on spool-only VPS design — see below.)

## OFFICIAL_CONTRACT

Recorded in `docs/binance_usdm_external_ws_contract_v1.md` (verified 2026-08-11).

- `bookTicker` → `wss://fstream.binance.com/public/ws/ethusdt@bookTicker`
- `aggTrade` → `wss://fstream.binance.com/market/ws/ethusdt@aggTrade`
- Two connections required (routed `/public` vs `/market`)
- Ping every 3m / pong within 10m; ~24h connection lifetime; symbols lowercase
- Identifiers kept separate: `book_update_id` (`u`) vs `agg_trade_id` (`a`)

No `CONTRACT_REVIEW_BLOCKED` — design assumptions matched official docs.

## IMPLEMENTATION

```
src/trading_bot/external_market_data/
  contract.py, envelope.py, binance_parser.py, spool.py,
  metrics.py, runtime.py, cli.py
compose.external-ref.yaml          # profile external-ref, default OFF, restart: no
tests/test_external_market_data.py
tests/fixtures/external_market_data/
```

Entrypoint: `python -m trading_bot.external_market_data.cli`  
Console script: `hibachi-external-ref` (pyproject).  
Canary image: `hibachi-external-ref:canary1` (FROM production digest + overlay).  
No Alembic / no PostgreSQL writes.

## ISOLATION

- Separate Compose service `external-ref-collector` (profile `external-ref`)
- No `depends_on` postgres/collector
- `restart: "no"` (hard-cap stop stays stopped)
- Hibachi collector remained Up/healthy before, during, after
- OFF dry-run (`EXTERNAL_REF_ENABLED=false`) exits 0 without collecting

## PREFLIGHT

| Check | Result |
|---|---|
| Hibachi collector | healthy |
| PostgreSQL | healthy |
| NTP / clock sync | yes / active |
| Disk free before | 6,254,211,072 B (~5.83 GiB) |
| Floor | 5 GiB unchanged |
| Hard cap | 128 MiB |
| Spool | empty, UID 10001, mode 0700 |
| External | OFF until explicit enable |
| Secrets | none (public market data) |

## CANARY

| Field | Value |
|---|---|
| Start (UTC) | 2026-08-11T20:09:32Z (connect ~20:09:37) |
| End (UTC) | 2026-08-11T20:31:04Z (last event) / container exit ~20:31:29 |
| Duration | **~21.4 minutes** (1287 s of events) |
| Stop | `EXTERNAL_CAPACITY_STOP` — spool hard cap 128 MiB |
| Exit code | 3 |

Target was 30 minutes; hard-cap stop fired first (correct fail-closed behavior). Max 60m not reached.

## BOOK_TICKER / AGG_TRADE

| Metric | Value |
|---|---:|
| book_ticker | 218,363 |
| agg_trade | 7,371 |
| total events | 225,734 |
| mean rate | ~175 msg/s |
| reconnects | 0 |
| malformed | 0 |
| connections | 2 (one per stream) |

FIRST_BOOK_TICKER / FIRST_AGG_TRADE: both observed.

## TIMESTAMPS (`received_at − exchange_at`)

| | ms |
|---|---:|
| n | 225,734 |
| min | 113 |
| p50 | 138 |
| p90 | 417 |
| p95 | 531 |
| p99 | 1,099 |
| max | 1,817 |
| negative | **0** |

No sub-250ms trading claim. Arrival-time causality fields present.

## SEQUENCE

- `book_update_id` populated on book_ticker
- `agg_trade_id` / first/last trade ids on agg_trade
- `local_sequence` per connection
- No reconnect during canary (single session each)

## STORAGE

| Metric | Value |
|---|---:|
| spool bytes | 134,217,695 (≈128 MiB hard cap) |
| bytes/event | ~595 |
| projected MiB/hour | **~358** |
| projected GiB/day | **~8.4** |
| hard cap | 128 MiB (enforced; no circular delete) |

Design estimate was ~40–90 MiB/h — **actual ~4–9× higher** (bookTicker dominates).

## RESOURCES

- peak RSS ≈ **54 MiB** (limit 64 MiB)
- CPU: not separately sampled (RSS within budget)

## DISK

| | bytes free |
|---|---:|
| before | 6,254,211,072 |
| after | 6,108,422,144 |
| floor | 5,368,709,120 |
| floor breached? | **no** |

## HIBACHI

| When | Status |
|---|---|
| before | Up ~10h (healthy) |
| during | Up (healthy); no restart |
| after | Up (healthy); postgres healthy |

External stop did **not** stop Hibachi.

## ROLLBACK

Proved:

1. external container stopped/removed
2. `EXTERNAL_REF_ENABLED` left false / service not running
3. Hibachi untouched
4. no DB migration to roll back
5. spool retained for operator review (not auto-deleted)

## QUALITY_PILOT_CAPACITY

`REQUIRES_OFFLOAD_DESIGN`

Rationale:

- ~358 MiB/h × 6h ≈ 2.1 GiB; ×24h ≈ 8.4 GiB
- VPS free headroom above 5 GiB floor was only ~0.7–0.9 GiB at canary time
- Hibachi ACTIVE generation continues to grow concurrently
- Spool-only retention cannot host a 6–24h quality pilot safely

**Do not authorize 6–24h on the current spool-only design.**  
Next storage step: bounded spool→Parquet→B2 offload with tiny on-box retention.

## BLOCKERS

None for declaring the technical canary’s fail-closed capacity behavior successful.  
Longer pilots blocked on storage architecture (offload), not on parser/WS connectivity.

## NEXT (not executed)

`design and prove bounded external spool-to-B2 offload before any longer pilot`
