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
- Classified upstream transport failures reconnect indefinitely with bounded
  backoff and visible degraded evidence. Database, unknown, and programming
  failures remain fatal; cancellation remains immediate and clean.
- Keep raw payloads append-only in `market_events`; preserve source, topic, symbol,
  exchange/receipt timestamps, latency, connection ID, local/exchange sequence, and
  RAW envelope schema version.
- Existing RAW schema-version-1 rows and payloads must never be rewritten. New
  WebSocket rows use schema version 2; exports retain both legacy and v2 envelope fields.
- The normalized-data core is review-only and supports only captured public
  `orderbook`, `ask_bid_price`, `mark_price`, `spot_price`, and
  `funding_rate_estimation` contracts. Do not enable its migration, backfill, or
  live tail in production: the 2.5% pilot projects insufficient disk headroom.
- Normalized provenance stores `raw_event_id` without a restrictive RAW foreign
  key. The approved lifecycle keeps a short RAW PostgreSQL hot buffer and writes
  normalized history directly to verified external Parquet; persistent
  normalized PostgreSQL history remains disabled. Production retention is
  dry-run-only and requires separate approval.
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
- `hibachi-bot normalize` is separate from collection, defaults to one 100-row
  batch, and requires an explicit capacity path. Its 3 GiB disk and 160 MiB RSS
  hard stops must not be lowered.
- `hibachi-archive` is the bounded lifecycle interface. Server-local filesystem
  storage is canary-only. Verified external storage may be S3-compatible or an
  owner-protected PC filesystem reached by bounded read-only SSH transport; the
  latter must never stage completed Parquet on the VPS or expose PostgreSQL.
  Its 4/3 GiB disk and 128/160 MiB RSS pause/stop gates must not be lowered.
  No production delete subcommand or automatic schedule exists.
- The normal RAW hot window is at least three days. Two days is degraded
  emergency planning only and requires an explicit flag, warning, and separate
  retention approval; the planner must not shorten the window automatically.
- Designed RAW reclaim path for the constrained VPS is RANGE(`id`) partition
  generations with operator-approved DROP after B2 storage-integrity verification
  (`docs/raw_partition_lifecycle.md`, `docs/raw_generation_operating_model.md`).
  Continuous loop: collect → provision successor → rotate → archive closed
  generation → verify B2 → `DROP_ELIGIBLE` → operator-approved DROP → reclaim.
  Ordinary DELETE retention remains the emergency/legacy path and does not
  reliably return relation files to the filesystem. Do not run production
  partition migration, collector restart, generation rotation, B2 mutation, or
  automatic generation DROP without explicit approval. VACUUM FULL is not a
  routine lifecycle tool.
- The default database URL is development-only. Replace it locally through
  `.env`; never commit local credentials.
- Collector, exporter, dashboard, and normal migrations use the explicit `research`
  database role. PostgreSQL integration tests require a distinct `TEST_DATABASE_URL`,
  `TEST_DATABASE_ROLE=test`, and test-only database target; never point them at research.
- After every meaningful repository change, review and update this `AGENTS.md`
  when project scope, invariants, workflow, branch state, or milestones changed.

## Git and GitHub

- Monitoring integration is merged to `main`; create a focused `codex/` branch for each
  subsequent monitoring change.
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
- `scripts/collect_ops.sh` is the provider-neutral interface for status, update
  preflight, bounded redacted logs, protected logical backup, and isolated
  restore validation. It must remain fail-closed and must not restart services,
  expose ports, print secrets, or delete unknown backup files.
- `scripts/collect_monitor.py` emits one bounded numeric JSON health contract for local
  monitoring. It must remain read-only, fail closed, open no listener, print no secret or
  host metadata, and perform no automatic remediation. Aggregate readiness is four-state:
  `1` healthy, `2` isolated low-risk swap warning, `0` confirmed critical, and `-1` unknown.
  Unknown, stale, malformed, unsupported, or contradictory state remains strongly alerting;
  the existing 256 MiB swap threshold is unchanged.
- Zabbix integration uses the documented root-owned bounded oneshot and sanitized cache.
  The Zabbix account must never gain Docker-group or sudo access, and fixed UserParameters
  must never invoke Docker, Compose, Git, SQL, or project scripts.
- The root-operated monitoring installer is the only supported host-side update path. It
  verifies a clean reviewed revision plus SHA-256 manifest before activation; do not grant the
  deployment account passwordless sudo for an installer sourced from its writable checkout.
- Collector exit evidence is retained only by the documented root-owned bounded Docker-event
  listener. Records must stay sanitized, fixed-schema, root-only, capped in size and count,
  and diagnostic-only: retention must never restart a service or change readiness.
- `scripts/collect_quality.py` provides bounded read-only stream-quality and capacity
  analysis. Exact full-history scans remain explicit opt-in; retention is a documented
  decision framework and must never delete or archive data automatically.
- `scripts/restart_state.py` is the shared bounded classifier for operations and monitoring.
  Static old restart history is observable but non-blocking; recent, advancing, unhealthy,
  malformed, or uncertain state remains blocking and must never trigger remediation.
- `scripts/storage_state.py` is the shared bounded storage classifier. PostgreSQL is the
  collector's authoritative sink; disabled dashboard dataset/report mounts are not
  applicable. Any enabled or declared filesystem sink must be mounted and writable by
  UID 10001, while inconsistent or uncertain state remains blocking.
- `python -m trading_bot.startup_diagnostic` is a bounded fail-closed, read-only collector
  prerequisite diagnostic. It validates required runtime names, PostgreSQL schema
  compatibility, and dependency construction without connecting a stream, writing events,
  or exposing runtime values.

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
   Offline research pipeline v1 ran the first **full-corpus** validation on verified B2 RAW
   (`prior_continuous` + `g_7471913_7871913`, 1.664M events): STATUS
   `FULL_CORPUS_RESEARCH_VALIDATED`. Follow-on
   `DATA_ACCUMULATION_AND_EDGE_CHARACTERIZATION`: STATUS
   `EDGE_CHARACTERIZATION_READY`, ML_DECISION
   `EDGE_INSUFFICIENT_FOR_CURRENT_HORIZON` (`docs/edge_characterization_v1.md`).
   Milestone `EXECUTION_AND_HORIZON_REASSESSMENT` then tested maker fill bounds
   (optimistic/base/conservative), extended labels through 600s, event selection, and
   execution-style required-move matrix without starting ML: STATUS
   `EXECUTION_HORIZON_REASSESSMENT_READY`, DECISION `STRATEGY_RETHINK_REQUIRED`,
   ML_STATUS `BLOCKED` (`docs/execution_horizon_reassessment_v1.md`). Maker fills show
   adverse post-fill mid; longer-horizon extreme gross stays ~0.8–1.9 bps vs ~11 bps
   taker friction; no event class clears break-even with adequate sample. Extreme
   exploratory gross still peaks ~2.3 bps (15s).    Follow-on `STRATEGY_SPACE_RETHINK`
   screened alternative economic mechanisms (basis, funding, liquidity events,
   volatility/opportunity targets, external relative value) without ML: STATUS
   `STRATEGY_SPACE_RETHINK_READY`, DECISION `DESIGN_EXTERNAL_FEED_PILOT`,
   RECOMMENDED_HYPOTHESIS `EXTERNAL_RELATIVE_VALUE_LEAD_LAG`
   (`docs/strategy_space_rethink_v1.md`).    Design review
   `EXTERNAL_RELATIVE_VALUE_FEED_DESIGN_REVIEW` then selected a minimal
   Binance USD-M `ETHUSDT` public `bookTicker`+`aggTrade` pilot (Bybit fallback),
   spool→Parquet→B2 storage (not `market_events`), isolated Compose service
   default OFF, arrival-time lead-lag protocol and predeclared economic gates:
   STATUS `EXTERNAL_FEED_DESIGN_READY` (`docs/external_relative_value_feed_design_v1.md`).
   Implementation + ≤60m technical canary authorized separately: isolated
   `external-ref-collector` (dual WS `/public` bookTicker + `/market` aggTrade),
   NDJSON spool hard-capped, Hibachi untouched; see
   `docs/binance_usdm_external_ws_contract_v1.md` and canary report.
   Technical canary STATUS `EXTERNAL_CAPACITY_STOP` (~21.4 min, exact 128 MiB
   cap, ~358 MiB/h RAW, ~8.4 GiB/day projected). Follow-on milestone
   `EXTERNAL_FEED_OFFLOAD_DESIGN_AND_PROOF`: STATUS `EXTERNAL_OFFLOAD_READY`,
   durable SoT = gzip NDJSON (~4.8% of RAW), 16 MiB segments, B2 prefix
   `external/binance_usdm/ETHUSDT/...`, integrity gate before reclaim;
   VPS↔B2 ≈7 MiB/s ≫ ~17 MiB/h gzip ingress; QUALITY_PILOT_CAPACITY
   `SAFE_WITH_OFFLOAD` (throughput) with live 15–30m offload canary deferred
   until filesystem margin above the 5 GiB floor is healthy and segment writer
   is wired into the live collector (`docs/external_feed_offload_design_v1.md`).
   Milestone `EXTERNAL_FEED_LIVE_OFFLOAD_CANARY_AND_HEADROOM`: live wiring +
   async offloader landed; headroom reclaimed to ~5.87 GiB free; original canary
   independently archived to B2 then local copy removed. Live canary STATUS
   `EXTERNAL_LIVE_OFFLOAD_BLOCKED` because Hibachi collector was unhealthy
   (missing `market_events` partition for current ids) — P0 preflight; external
   remains OFF (`docs/external_feed_live_offload_canary_v1.md`). Prior enum
   `QUALITY_PILOT_OFFLOAD_UNSTABLE` is semantic only:
   `LIVE_OFFLOAD_NOT_EVALUATED_DUE_TO_HIBACHI_P0` (no live offload run).
   Follow-on `HIBACHI_PARTITION_RECOVERY_AND_PROVISIONING_ROOT_CAUSE`: STATUS
   `HIBACHI_PARTITION_RECOVERY_PASS` — provisioned `market_events_g_9071913`
   `[9071913,9471913)`, restored COLLECT, fixed maintain so capacity STOP no
   longer skips 50k-lead provision (`docs/hibachi_partition_recovery_v1.md`).
   ACTIVE is now `g_14271913_14671913` `[14271913,14671913)` with successor
   `g_14671913_15071913` PROVISIONED. COLLECT resumed
   2026-08-19T18:04:13Z at id **13682173** after the 23:10Z capacity STOP
   (`docs/external_live_offload_canary_v1.md`). Emergency
   capacity recovery
   (`HIBACHI_EMERGENCY_CAPACITY_RECOVERY`) paused COLLECT at id **11902151**
   (2026-08-14T08:42:22Z), then on 2026-08-17 resumed after B2 Class B
   recovered: three more verified DROPs (`g_9471913`…`g_10271913`) restored
   READY disk. STATUS `HIBACHI_EMERGENCY_CAPACITY_RECOVERY_PASS`. COLLECT
   resumed 2026-08-17T17:18:16Z at id **11902152**. Storage-lifecycle soak
   then completed one authorized DROP of `g_10671913` and two natural
   50k-lead provision/rotations. STATUS
   `HIBACHI_STORAGE_LIFECYCLE_SOAK_PASS`
   (`docs/hibachi_storage_lifecycle_soak_v1.md`). Headroom prep then reached
   `EXTERNAL_CANARY_HEADROOM_READY` (`docs/external_canary_headroom_prep_v1.md`).
   Hibachi COLLECT is running. Isolated Binance USD-M live-offload canary ran
   ~11 min (7 B2-verified segments) then fail-closed on a Hibachi 10s
   healthcheck timeout: STATUS `EXTERNAL_CANARY_FAILED`, DECISION
   `CANARY_BLOCKED_HIBACHI` (`docs/external_live_offload_canary_v1.md`).
   Follow-on `EXTERNAL_OFFLOAD_DRAIN_AND_HEALTHCHECK_FIX` landed drain
   idempotency, atomic `state.json`, Hibachi-priority CPU shares, and an
   O(1) ACTIVE `ORDER BY id DESC LIMIT 1` healthcheck overlay (no GHCR pull).
   15-minute retry STATUS `EXTERNAL_CANARY_RETRY_BLOCKED` (free
   5,537,943,552 B < 5,974,908,928); DECISION `NEEDS_RESOURCE_TUNING`
   (`docs/external_offload_drain_and_healthcheck_fix_v1.md`). External-ref
   OFF. **No quality pilot / multi-day collection / ML / PAPER/LIVE.** Hibachi-only
   short-horizon directional hypothesis remains durably rejected. `g_7471913`
   remains contaminated; clean OOS placeholder reserved for the next verified closed
   generation. Feature naming distinguishes `signed_trade_flow_*` from Cont-style
   `ofi_*`. Stale market_state tops no longer bridge archive gaps. The first manual
   private COLLECT-only stack is operational; provider-neutral recoverability and
   local monitoring contracts are prepared. Deployment updates, network changes,
   dashboard access, and any PAPER/LIVE behavior still require separate explicit
   approval.
5. **In progress:** RAW storage lifecycle for the disk-constrained VPS. Bounded
  DELETE retention is the emergency/legacy path. First production generation
  lifecycle completed: `market_events_g_7471913` `[7471913,7871913)` archived to
  B2, verified, and physically DROPped (`DROP_VERIFIED_GENERATION`) with
  ~194.1 MiB filesystem reclaim. Follow-on `HIBACHI_ARCHIVE_BACKLOG_RECOVERY`
  then `HIBACHI_EMERGENCY_CAPACITY_RECOVERY`: seven verified generation DROPs
  (`g_7871913`…`g_10271913`) plus prior `g_7471913`. STATUS
  `HIBACHI_EMERGENCY_CAPACITY_RECOVERY_PASS` with free **≥ READY**
  `5,706,473,472 B` (~5.315 GiB). COLLECT resumed; the time-only pause gap is
  in `docs/quality/hibachi_collection_gaps_v1.json`. Auto-archive tick
  (`*/15`, `--require-normal-floor`, no `--drop`) proved oldest-first
  `g_10671913` → `DROP_ELIGIBLE` without physical DROP. Soak
  `HIBACHI_STORAGE_LIFECYCLE_SOAK` added `DROP_BACKLOG_LIMIT` (cap 2), aborted
  the 18:00 third archive of `g_11471913` before DROP_ELIGIBLE=3, then on
  explicit approval physically DROPped **only** `g_10671913`
  (`DROP_VERIFIED_GENERATION`, evidence
  `dccd3292a6059b6eec400dfffbac833a73c5766b07358dfd96bced3ba1766050`, observed
  reclaim 207,486,976 B). Queue cycled `2 → 1 → archive g_11471913 → 2`. Two
  natural 50k provisions and rotations followed:
  `g_11871913` → `g_12271913` → `g_12671913`. New CLOSED
  generations stayed `CLOSED_UNARCHIVED` under the cap. STATUS
  `HIBACHI_STORAGE_LIFECYCLE_SOAK_PASS`
  (`docs/hibachi_storage_lifecycle_soak_v1.md`). Follow-on
  `EXTERNAL_CANARY_HEADROOM_PREP` gated-DROPped verified generations
  `g_11071913`…`g_13071913` (no auto-DROP) to
  **6,050,516,992 B** (≥ canary target 5,974,908,928). STATUS
   `EXTERNAL_CANARY_HEADROOM_READY`
   (`docs/external_canary_headroom_prep_v1.md`). COLLECT resumed 2026-08-19;
   the 23:10Z capacity-stop time hole is closed in
   `docs/quality/hibachi_collection_gaps_v1.json`. Live offload canary
   STATUS `EXTERNAL_CANARY_FAILED` / DECISION `CANARY_BLOCKED_HIBACHI`
   (`docs/external_live_offload_canary_v1.md`). Retry
   `EXTERNAL_CANARY_RETRY_BLOCKED` on free 5,537,943,552 B with two
   `DROP_ELIGIBLE` children still mounted (`g_13471913`, `g_13871913`).
   External-ref OFF. Physical DROP remains human-approved only. Operator
   floor remains 5 GiB. Healthcheck uses ACTIVE-bounded `ORDER BY id DESC
   LIMIT 1` via overlay bind-mount (GHCR digest unchanged; do not pull a
   second 554 MiB image unless disk preflight allows). Collector live
   `cpu_shares=2048`.
6. PAPER remains disabled even when admission criteria pass. Human review and a
  separate explicitly approved implementation milestone are mandatory; keep all
  real trading commands absent.
