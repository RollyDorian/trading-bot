# Hibachi ETH perpetual research bot

Safety-first Python service for researching short-horizon strategies on the
Hibachi `ETH/USDT-P` perpetual contract.

## Current milestone

The repository is intentionally **COLLECT-only**. The executable can read public
exchange metadata and validate the configured contract, but it contains no order
placement, cancellation, account, or withdrawal commands.

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

`BOT_MODE` must be `collect`. Any other value fails during configuration loading.
Trading modes will be introduced only after data validation, persistent storage,
paper execution, risk controls, and explicit acceptance criteria are implemented.

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
`55432`, sends one representative public market message through
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
