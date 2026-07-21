# Project instructions

## Purpose and current scope

This repository is a safety-first research service for the Hibachi
`ETH/USDT-P` perpetual contract. It is **COLLECT-only** at the current stage.

- `BOT_MODE` must remain `collect`.
- Do not add order placement, cancellation, account, transfer, withdrawal,
  leverage, PAPER, or LIVE behavior unless the user explicitly asks and the
  corresponding risk/acceptance work is complete.
- Do not claim a strategy is profitable or promise returns. Signals require
  research, out-of-sample evaluation, and costs (fees, funding, slippage).

## Architecture and safety invariants

- Python 3.13+ is required because `hibachi-xyz==0.3.1` requires it.
- Use the official Hibachi SDK only for public market metadata and market
  WebSocket collection in this milestone.
- The market stream must fail closed: unexpected stream termination or a DB
  write failure must stop collection rather than drop events silently.
- Keep raw payloads append-only in `market_events`; preserve normalized source,
  topic, symbol, exchange timestamp, sequence, receipt timestamp, and latency.
- Record connectivity, validation, desync, and storage failures in
  `system_events` when adding operational flows.
- The SDK's WebSocket client does not close its `aiohttp` executor by itself;
  `HibachiMarketStream.disconnect()` must continue to close both the client and
  its executor.
- Never add API keys, private keys, account IDs, `.env`, database dumps, or
  logs with secrets to Git, terminal output, or Telegram.

## Local workflow

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe src migrations
.\.venv\Scripts\alembic.exe upgrade head --sql
```

- Run all four checks after meaningful changes. PostgreSQL integration requires
  a running PostgreSQL 16+ instance and `alembic upgrade head`.
- `hibachi-bot` validates public contract metadata and exits.
- `hibachi-bot --stream` is the explicit continuous collection command; do not
  launch it against a database unless migrations have been applied.
- The default database URL is development-only. Replace it locally through
  `.env`; never commit local credentials.
- Collector, exporter, dashboard, and normal migrations use the explicit `research`
  database role. PostgreSQL integration tests require a distinct `TEST_DATABASE_URL`,
  `TEST_DATABASE_ROLE=test`, and test-only database target; never point them at research.
- After every meaningful repository change, review and update this `AGENTS.md`
  when project scope, invariants, workflow, branch state, or milestones changed.

## Git and GitHub

- Current deployment-preparation branch: `codex/collect-only-deployment-plan`.
- Preserve unrelated user changes. Do not reset, force-push, delete branches,
  merge a PR, or push new commits unless the user explicitly requests it.
- Git for Windows must use the system OpenSSH to access the loaded Windows
  `ssh-agent` key:

  ```powershell
  $env:GIT_SSH_COMMAND='"C:\Windows\System32\OpenSSH\ssh.exe"'
  ```

- GitHub CLI is installed at `C:\Program Files\GitHub CLI\gh.exe`. Verify auth
  with `gh auth status` before workflows that require the GitHub API.

## Deployment host policy

- Keep hostnames, SSH aliases, Linux usernames, key paths, provider details,
  installed versions, listener inventories, and bootstrap status outside Git.
- Verify SSH host identity out of band. Routine deployment uses a dedicated
  non-root account with only the minimum container-runtime access and no sudo.
- Deployment and secret directories must be operator-owned with restrictive
  permissions. Dataset/report directories remain writable by container UID/GID
  `10001`; runtime environment files remain outside Git with mode `0600`.
- Audit existing listeners and resource ownership before deployment. Do not
  publish application ports; separately approved dashboard access is
  loopback-only through an SSH tunnel.
- Host preparation does not authorize image pulls, Compose starts, migrations,
  PostgreSQL provisioning, dashboard access, or a collector stream. Each is a
  separate, explicitly approved operational step.
- Both local and production Compose definitions keep PostgreSQL, collector, and
  dashboard internal-only. Memory ceilings are 256 MiB, 160 MiB, and 80 MiB;
  the dashboard is profile-gated and omitted from the initial VPS startup.

## Suggested next milestones

1. **Complete:** Soak tests cover reconnect continuity, desync halt/error recording,
   and propagated PostgreSQL write failures.
2. **Complete:** The dashboard exposes authenticated research export/evaluate
   controls and read-only paper-admission visibility. It never enables execution.
3. **Complete:** A deterministic, fail-closed paper-admission research gate validates
   manifests, checksums, quality status `pass`, chronological splits, compatible
   cost-aware replay reports, and aggregate OOS criteria.
4. **In progress:** Exercise the admission gate across multiple representative,
   versioned datasets and independently review thresholds, cost assumptions, regime
   coverage, and OOS stability. Schema 4 now separates global receipt order from per-topic
   exchange order: two audited slices pass, two warn on data gaps, and one rejects a stale
   fixture timestamp. Only 2 trade events exist and passing slices have zero replay trades.
   The fixture path is now isolated from research storage, but fresh real COLLECT-only
   intervals are still required. Do not lower thresholds or invent regimes to force admission.
   A COLLECT-only immutable-image/VPS deployment plan is prepared for review, but no
   deployment, migration, database provisioning, network change, or collector stream
   is authorized by repository policy.
5. PAPER remains disabled even when admission criteria pass. Human review and a
   separate explicitly approved implementation milestone are mandatory; keep all
   real trading commands absent.
