# COLLECT-only deployment plan

Status: reviewable preparation only. Nothing in this plan authorizes deployment,
deployment-host access, network changes, PAPER, LIVE, or trading behavior.

## Architecture and release boundary

The intended release path is:

1. A reviewed change is merged through GitHub.
2. GitHub Actions runs pytest, Ruff, mypy, and offline Alembic SQL validation.
3. Only after verification, CI builds the container. Pull requests build without push.
4. A push to `main` publishes `ghcr.io/rollydorian/trading-bot:sha-<commit>` to GHCR.
5. An operator resolves that tag to a registry digest and records
   `IMAGE_REPOSITORY` plus immutable `IMAGE_DIGEST` on the VPS.
6. A human performs the migration and controlled Compose update.

CI does not SSH, modify the VPS, or deploy. Any future deployment job requires a separate
approval, a protected GitHub Environment, required reviewers, and manual approval.

## PostgreSQL isolation and least privilege

Use a dedicated research database, never the integration-test database. Provision two
database roles outside the repository:

- migration role: owns the research schema and is used only by the one-off migration;
- runtime role: can connect, select and insert into `market_events` and `system_events`,
  and use their sequences; it cannot create/alter/drop schema or update/delete events.

`MIGRATION_DATABASE_URL` belongs only to the migration service. `DATABASE_URL` belongs to
collector/dashboard/health checks. Both use `DATABASE_ROLE=research`; production Compose
contains no test role or test URL. PostgreSQL credentials must be stored only in the VPS
secret environment mechanism or a root-readable local environment file outside the clone.

## GitHub credentials and variables

The implemented CI/GHCR workflow requires no custom repository secret. It uses the
GitHub-provided secret name `GITHUB_TOKEN` with `packages: write` only in the image job.

If a future protected manual deployment workflow is separately approved, its proposed
GitHub Secret names are:

- `VPS_HOST`
- `VPS_USER`
- `VPS_SSH_PRIVATE_KEY`
- `VPS_SSH_HOST_KEY`
- `GHCR_READ_TOKEN`

Database URLs and dashboard credentials should remain on the VPS, not transit through a
deployment workflow. If policy later requires GitHub-managed environment secrets, use
names `RESEARCH_DATABASE_URL`, `MIGRATION_DATABASE_URL`, and `DASHBOARD_TOKEN`.

## VPS prerequisites

- Supported Linux distribution with current security updates.
- Docker Engine with Compose v2.
- Sufficient disk for image layers, PostgreSQL growth, backups, and exported datasets.
- A dedicated non-login service directory writable only by the deployment operator.
- Dataset and report directories owned by container UID/GID `10001`, outside the clone.
- A separately provisioned PostgreSQL 16+ research database and the two roles above.
- GHCR read access if the package is private.
- Time synchronization and log rotation configured by the operator.
- A backup destination outside the active PostgreSQL data volume.

VPN, firewall, DNS, reverse proxy, TLS, SSH policy, and server-user changes are explicitly
out of scope unless the user separately approves them.

For the initial single-user VPS deployment, Compose enforces these memory ceilings:
PostgreSQL 256 MiB, collector 160 MiB, and dashboard 80 MiB. The dashboard profile remains
off; start only PostgreSQL and the collector. No application service publishes a host port.
If dashboard access is separately approved, use a reviewed loopback-only override and an
SSH tunnel; never expose it publicly. Stop deployment or collection when root filesystem
free space falls below 3 GiB. Treat sustained swap use above 256 MiB as a capacity warning
requiring operator review, not as permission to raise limits automatically.

The collector's authoritative persistence is PostgreSQL. Dataset/report mounts are
dashboard-only and therefore `not_applicable` while its profile is disabled. If that
profile is enabled by a separately approved change, its declared mounts become required
and must pass the shared UID 10001 writable-path classifier.

## First deployment sequence

The following is a human procedure and must not be run by CI in this milestone.

1. Select the successful `main` workflow run and record its commit SHA, image tag, and
   resolved GHCR digest.
2. On the VPS, create a release directory containing only the reviewed
   `compose.production.yaml`.
3. Store these values outside Git using the VPS secret mechanism:

   - `BOT_MODE=collect`
   - `DATABASE_ROLE=research`
   - `DATABASE_URL`
   - `MIGRATION_DATABASE_URL`
   - `POSTGRES_PASSWORD`
   - `IMAGE_REPOSITORY`
   - `IMAGE_DIGEST`
   - `DASHBOARD_TOKEN`
   - `DATASETS_PATH`
   - `REPORTS_PATH`

4. Confirm `IMAGE_DIGEST` is the reviewed `sha256` digest, then pull without starting:

   ```bash
   docker compose -f compose.production.yaml pull
   ```

5. Generate and review resolved Compose configuration without printing it into shared
   logs. Confirm `BOT_MODE=collect`, `DATABASE_ROLE=research`, immutable image digests,
   and absence of every `TEST_*` variable.
6. Take and verify a pre-deployment PostgreSQL backup.
7. Run migrations as a one-off command with the migration URL:

   ```bash
   docker compose -f compose.production.yaml --profile tools run --rm migrate
   ```

8. Start only PostgreSQL and the collector; the dashboard profile stays disabled:

   ```bash
   docker compose -f compose.production.yaml up -d postgres collector
   ```

9. Record the deployed commit, digest, migration revision, UTC start time, and operator.

## Health verification

The collector container runs a read-only PostgreSQL freshness probe every 30 seconds. It
returns healthy only when `max(market_events.received_at)` is timezone-aware, not in the
future, and no older than 120 seconds. It prints only `healthy`; connection strings and raw
errors are never emitted. A failed DB query or empty database is unhealthy.

The dashboard healthcheck requests its loopback `/api/status` endpoint without credentials.
Operator verification:

```bash
docker compose -f compose.production.yaml ps
docker compose -f compose.production.yaml logs --since 10m collector
```

Logs must show no credentials. Confirm the collector becomes healthy, receipt timestamps
advance in UTC, event counts increase, and no repeated `DESYNC`, storage failure, or
`HALTED` event is present. An admission `FAIL` or zero replay trades is expected research
output and must not change thresholds.

## Stop and rollback

Graceful operational stop:

```bash
docker compose -f compose.production.yaml stop -t 30 collector
```

This sends the normal termination signal and allows WebSocket/executor cleanup. Verify the
container is stopped before maintenance. Do not use `down -v`; dataset/report volumes must
survive service replacement.

Rollback is image-only unless the migration is explicitly documented as reversible:

1. Stop the collector gracefully.
2. Restore the previous recorded `IMAGE_DIGEST`.
3. Run `docker compose ... pull`, then `up -d postgres collector`.
4. Verify health and timestamp progression.

Never run an automatic Alembic downgrade against append-only research data. If a schema
change is not backward compatible, stop and require a separately reviewed database restore
or forward-fix plan.

## Backup and retention

- Take encrypted PostgreSQL backups before migrations and on an operator-defined schedule.
- Store backups outside the active database volume and test restoration into an isolated
  non-research database.
- Monitor age, size, and successful restore evidence; a created backup alone is insufficient.
- Keep exported datasets with manifests and checksums in controlled storage.
- Do not put dumps, datasets, reports, logs, or credentials in Git or container images.
- Retention remains an explicit one-off maintenance action. The least-privilege runtime role
  has no delete permission; any retention role or schedule requires separate review.
- Use `scripts/collect_ops.sh` and `docs/operations_runbook.md` for provider-neutral status,
  preflight, bounded logs, protected logical backups, isolated restore validation, and
  rollback preparation. These commands do not authorize an update or deployment.
- Use `scripts/collect_monitor.py` and `docs/monitoring.md` for one bounded host-local JSON
  health contract. It opens no port, emits no secrets, and performs no remediation.
- Approved Zabbix integration uses the root-owned bounded oneshot, sanitized cache, and
  fixed-key reader; the agent never receives Docker or sudo access.
- Use `scripts/collect_quality.py` and `docs/retention_readiness.md` for bounded on-demand
  stream continuity and capacity forecasts. Retention remains a human decision only.
- Use `scripts/restart_state.py` through the operations and monitoring interfaces to
  distinguish static restart history from bounded evidence of recent or active looping.

## Explicitly unimplemented

- Automated deployment-host connection or deployment.
- SSH, VPN, firewall, DNS, TLS, or reverse-proxy changes.
- GitHub Environment, deployment secrets, or automatic/manual deployment jobs.
- Automated PostgreSQL backup, restore, retention, remediation, or alert delivery.
- PAPER/LIVE modes, orders, accounts, transfers, withdrawals, leverage, or private APIs.
