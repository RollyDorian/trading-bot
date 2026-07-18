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
- After every meaningful repository change, review and update this `AGENTS.md`
  when project scope, invariants, workflow, branch state, or milestones changed.

## Git and GitHub

- Current integration branch: `codex/integration`.
- Preserve unrelated user changes. Do not reset, force-push, delete branches,
  merge a PR, or push new commits unless the user explicitly requests it.
- Git for Windows must use the system OpenSSH to access the loaded Windows
  `ssh-agent` key:

  ```powershell
  $env:GIT_SSH_COMMAND='"C:\Windows\System32\OpenSSH\ssh.exe"'
  ```

- GitHub CLI is installed at `C:\Program Files\GitHub CLI\gh.exe`. Verify auth
  with `gh auth status` before workflows that require the GitHub API.

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
   coverage, and OOS stability.
5. PAPER remains disabled even when admission criteria pass. Human review and a
   separate explicitly approved implementation milestone are mandatory; keep all
   real trading commands absent.
