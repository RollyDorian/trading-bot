# Hibachi ETH perpetual research bot

Safety-first Python service for researching short-horizon strategies on the
Hibachi `ETH/USDT-P` perpetual contract.

## Current milestone

The repository collects public data and exports immutable, checksummed datasets for
deterministic offline research. It contains no order placement, cancellation, account,
transfer, withdrawal, leverage, or private API commands. `BOT_MODE` remains `collect`.

## Offline baseline research

The baseline is a deliberately simple short-horizon momentum benchmark over exported
one-second candles. It emits research intents only. The report applies configurable
taker costs, funding estimate, slippage, latency penalty, and execution delay. Maker
fees are recorded in configuration but are not used by this taker-only baseline.

This is not a validated strategy and is not evidence of profitability. PAPER remains
blocked until chronological out-of-sample evaluation across multiple versioned
datasets passes acceptance thresholds chosen before viewing those samples.

## Exact research workflow

### 1. Apply migrations

```powershell
.\.venv\Scripts\alembic.exe upgrade head
```

### 2. Collect public events

```powershell
.\.venv\Scripts\hibachi-bot.exe --stream
```

### 3. Export a bounded versioned dataset

The start is inclusive and the end is exclusive. Both timestamps must include a
timezone. The default destination is `data/research/eth-usdt-p/`.

```powershell
.\.venv\Scripts\hibachi-bot.exe --export-dataset `
  --start 2026-07-18T00:00:00Z `
  --end 2026-07-18T08:00:00Z
```

Each dataset contains `manifest.json`, `events.parquet`, `candles_1s.parquet`, and
`README.md`. The ID includes symbol, UTC bounds, and schema version. The manifest
contains row counts, deterministic export timestamp, software revision, and SHA-256
checksums. Re-export to an existing ID fails instead of overwriting it.

### 4. Replay the offline baseline

```powershell
.\.venv\Scripts\hibachi-bot.exe --offline-replay `
  data/research/eth-usdt-p/eth-usdt-p_20260718T000000000000Z_20260718T080000000000Z_v1 `
  --report research-report.json
```

Replay validates the manifest and every artifact checksum before reading candles. It
uses no network or exchange client. Identical dataset and typed configuration produce
an identical report.

### 5. Read the report

The terminal prints a concise `OFFLINE RESEARCH SIMULATION` summary. The JSON report
contains dataset/configuration identity, signals, simulated entries/exits, gross PnL,
fees, funding, slippage plus latency, net PnL, win rate, maximum drawdown, average
holding time, skipped-signal reasons, intents, and simulated trades.

All PnL fields are research simulation outputs. The baseline assumes one position at a
time and a cooldown. It does not model queue position, partial fills, liquidation,
spread dynamics, time-varying fees, exact funding settlements, or market impact.

### VPS/Linux equivalents

Run these from the checked-out repository with `DATABASE_URL` configured outside Git:

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/alembic upgrade head
.venv/bin/hibachi-bot --stream
.venv/bin/hibachi-bot --export-dataset \
  --start 2026-07-18T00:00:00Z \
  --end 2026-07-18T08:00:00Z
.venv/bin/hibachi-bot --offline-replay \
  data/research/eth-usdt-p/eth-usdt-p_20260718T000000000000Z_20260718T080000000000Z_v1 \
  --report research-report.json
```

Do not run collection before migrations complete. Generated datasets and reports are
ignored by Git; copy them to controlled research storage with their manifest intact.

## Versioned exporter, evaluator, and dashboard

The dashboard milestone adds a compact version layout alongside the checksummed
bounded format above:

```powershell
.\.venv\Scripts\hibachi-bot.exe export-dataset `
  --out datasets `
  --version v1_20260718 `
  --start 2026-07-18T00:00:00Z `
  --end 2026-07-19T00:00:00Z

.\.venv\Scripts\hibachi-bot.exe evaluate-dataset `
  datasets/v1_20260718 `
  --window 20 `
  --threshold-bps 5

docker compose up -d --build dashboard
```

Omit `--version` to allocate the next `vN_YYYYMMDD` directory. Each version contains
`ETH-USDT-P.parquet`, `manifest.json`, and, after evaluation,
`eval_momentum.json`. Existing versions are never overwritten.

The dashboard listens on `127.0.0.1:8000` by default and exposes read-only status,
dataset, evaluation, and recent-market endpoints. Its Chart.js asset is loaded from a
public CDN by the browser; API and evaluation code make no exchange requests.

The momentum evaluator reports hypothetical PnL without fees. This deliberately
incomplete benchmark must not be compared with the cost-aware replay report or used
to admit PAPER mode.

## Paper admission research gate

Generate quality and cost-aware replay reports for four chronological datasets:

```powershell
$datasets = @(
  "data/research/eth-usdt-p/eth-usdt-p_20260701T000000000000Z_20260702T000000000000Z_v1",
  "data/research/eth-usdt-p/eth-usdt-p_20260702T000000000000Z_20260703T000000000000Z_v1",
  "data/research/eth-usdt-p/eth-usdt-p_20260703T000000000000Z_20260704T000000000000Z_v1",
  "data/research/eth-usdt-p/eth-usdt-p_20260704T000000000000Z_20260705T000000000000Z_v1"
)
foreach ($dataset in $datasets) {
  .\.venv\Scripts\hibachi-bot.exe validate-dataset --dataset $dataset
  .\.venv\Scripts\hibachi-bot.exe --offline-replay $dataset `
    --report "$dataset/offline_replay.json"
}

.\.venv\Scripts\hibachi-bot.exe admit-paper `
  --datasets $datasets `
  --validation-count 1 `
  --oos-count 2 `
  --report paper-admission-report.json
```

`paper-admission-report.json` is the default dashboard admission-report path; override it
with `ADMISSION_REPORT_PATH`. It records artifact decisions, chronological splits, OOS
aggregates, thresholds, and every criterion result. Existing reports are not overwritten
unless `--force` is explicit. See [the formal policy](docs/paper_admission.md).

An admitted result does not enable PAPER, authorize trading, or provide evidence of
future profitability. `BOT_MODE=collect` remains mandatory and human review is required.
The latest collected-data exercise and its unresolved blockers are documented in
[the Milestone 4 admission review](docs/milestone4_admission_review.md).
Timestamp, clock-domain, and sequence requirements are defined in
[the quality invariants](docs/timestamp_quality_invariants.md).
Research/test PostgreSQL isolation and the safe collection workflow are documented in
[COLLECT-only operations](docs/collect_only_operations.md).
The review-only container/VPS release architecture, health checks, rollback, and backup
requirements are documented in [the deployment plan](docs/deployment_plan.md). No deployment
or deployment-host connectivity is implemented.

## Requirements

- Python 3.13+
- PostgreSQL 16+
- Network access to Hibachi public APIs

The Python requirement follows the current official `hibachi-xyz` SDK rather
than the older `3.12+` assumption in the original specification.

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
hibachi-bot
```

The default configuration uses only public endpoints and requires no API keys.
Secrets must never be committed, logged, or sent to Telegram.

## Safety invariant

`BOT_MODE` remains `collect`: PAPER is an account-free CLI research action, not an
exchange runtime mode. Any non-collect mode fails during configuration loading.

## Database schema

Raw market and system events are stored append-only in PostgreSQL. Apply the
schema after setting `DATABASE_URL`:

```powershell
.\.venv\Scripts\alembic.exe upgrade head
```

The event payload remains JSON so upstream messages can be preserved without
loss, while timestamps, sequence numbers, source, symbol, latency, and event type
are indexed columns for validation and replay.

After the migration, start continuous public market collection explicitly:

```powershell
hibachi-bot --stream
```

Without `--stream`, the command only validates current public contract metadata
and exits. If PostgreSQL becomes unavailable or the WebSocket receive loop stops,
the collector records a `DEGRADED` event and reconnects with bounded exponential
backoff. Repeated failures produce `HALTED` and stop the process. Order book
updates are accepted only after a snapshot; detected sequence gaps or regressions
produce `DESYNC` and restart the stream instead of continuing with invalid state.

## Local PostgreSQL and end-to-end check

Docker Compose starts a PostgreSQL 16 instance bound only to localhost. The
credentials in `compose.yaml` are development-only and match `.env.example`:

```powershell
docker compose up -d --wait postgres
.\.venv\Scripts\alembic.exe upgrade head
```

Run the deterministic end-to-end COLLECT check with:

```powershell
.\scripts\e2e_collect.ps1
```

The check uses the isolated `cryptobot-e2e` Compose project on localhost port
`55432` with database `cryptobot_test`, sends one representative public market message through
`MarketCollector`, verifies the normalized fields and unchanged raw payload in
PostgreSQL, and confirms that an ended stream fails closed. It removes the test
container and its isolated volume afterward; pass `-KeepDatabase` to keep them
for local inspection. The check does not connect to account or trading APIs.

## Replay, data quality, and retention

Read-only maintenance commands use the configured `DATABASE_URL` and do not
connect to account or trading APIs:

```powershell
hibachi-bot --quality-date 2026-07-16
hibachi-bot --replay --start 2026-07-16T00:00:00Z --event-type trades --limit 1000
```

Replay output is deterministic JSON Lines ordered by `received_at` and database
`id`. Daily quality output groups counts, missing timestamps/sequences, and
latency statistics by symbol and topic.

Retention is disabled unless both the timezone-aware cutoff and explicit
confirmation flag are provided:

```powershell
hibachi-bot --retention-before 2026-06-01T00:00:00Z --confirm-retention
```

GitHub Actions runs unit tests, PostgreSQL integration, Ruff, Mypy, and Alembic
validation for every pull request.
