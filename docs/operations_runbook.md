# COLLECT-only operations runbook

This provider-neutral interface operates an already deployed private PostgreSQL and
collector stack. It never starts the dashboard, publishes ports, changes trading mode, or
updates a deployment. Run it only through the approved non-root Docker-access account.

Set these server-local variables without placing their values in Git or shared logs:

```sh
export HIBACHI_DEPLOY_DIR=/absolute/path/to/reviewed-checkout
export HIBACHI_RUNTIME_ENV=/absolute/path/to/runtime.env
export HIBACHI_BACKUP_DIR=/absolute/protected/backup-directory
```

The runtime env must be owned by the operator and mode `0600`. The backup directory must
be outside the checkout, owned by the operator, and mode `0700`.

## Routine status and update preflight

```sh
scripts/collect_ops.sh status
scripts/collect_ops.sh preflight
```

`status` requires healthy PostgreSQL and collector containers, a stable restart state, no
dashboard container, and no published project ports. `preflight` additionally requires a
clean pinned checkout, valid internal-only Compose policy, at least 3 GiB free disk, at
least 256 MiB available RAM, and no more than 256 MiB swap in use. It performs no pull,
build, migration, restart, or update.

Restart classification takes two Docker-state samples five seconds apart. A zero count is
`healthy_stable`; a static old non-zero count is observable as `historical_restart`, and
both may pass. Any count increase or container replacement is `restart_loop`. A non-zero
count whose process started within five minutes is `recent_restart`; at least three
restarts with a start within 30 minutes is also `restart_loop`. Recent, looping, unhealthy,
missing, malformed, or inconsistent state blocks fail closed. The observation duration may
be set from 2 through 30 seconds with `HIBACHI_RESTART_OBSERVATION_SECONDS`; it never
changes or restarts a service.

## Bounded redacted logs

```sh
HIBACHI_LOG_SINCE=10m HIBACHI_LOG_LINES=200 scripts/collect_ops.sh logs
```

The command accepts only a bounded duration and at most 1000 lines. It redacts URLs,
credential-like assignments, addresses, and hostnames before writing to stdout. Never use
unbounded `docker logs` in shared terminals or reports.

## Logical backup and bounded retention

```sh
HIBACHI_BACKUP_RETENTION=5 scripts/collect_ops.sh backup
```

The command creates a PostgreSQL custom-format logical backup with mode `0600`, validates
its archive list, and only then applies retention. Retention accepts 1 through 20 and removes
only successful files matching the script-owned `hibachi-<UTC>-<revision>.dump` pattern.
Unknown files and incomplete temporary files are never selected for retention.

## Non-destructive restore validation

Select one managed artifact without printing its contents, then run:

```sh
export HIBACHI_BACKUP_FILE="$HIBACHI_BACKUP_DIR/<managed-backup-name>"
scripts/collect_ops.sh validate-backup
```

Validation first checks the archive, then restores it into a temporary PostgreSQL 16
container with no network and no published port, a 192 MiB memory limit, and temporary
storage. It verifies required tables and removes only that temporary validation container.
The production database and active services are not modified or restarted.

## Rollback preparation

Before every future update, record the current revision and immutable image digest outside
Git, keep its checkout unchanged as the prior release, run `preflight`, and create plus
validate a backup. If no prior revision and digest are recorded, rollback is not available
and the update must stop.

For an approved rollback: stop only the collector with its normal grace period, select the
preserved prior checkout and digest, render and review Compose, run no automatic Alembic
downgrade, and start only PostgreSQL plus collector. If the prior image is incompatible with
the current schema, stop and require a separately reviewed forward-fix or isolated restore.
Never use `compose down -v`, delete append-only events, or enable the dashboard as part of
rollback.

## Data quality and capacity review

Run `python3 scripts/collect_quality.py` for bounded JSON or add `--format summary` for a
short operator view. Treat `unknown` as a failure. The default window performs no exact
full-history count; `--full-history` is an explicit, potentially expensive review action.
Follow `docs/retention_readiness.md` for signal interpretation, capacity escalation,
retention alternatives, and bounded sample export. No result authorizes deletion,
archival execution, migration, or automatic remediation.
