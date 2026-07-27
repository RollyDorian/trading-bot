# COLLECT-only local monitoring contract

Monitoring is a host-local, provider-neutral read-only check. It opens no listener, sends
no alert itself, performs no automatic remediation, and does not start, stop, or recreate
a container. Run it through the existing non-root deployment account with the same
protected variables used by `collect_ops.sh`:

```sh
python3 scripts/collect_monitor.py
```

The command writes exactly one compact JSON object and no stderr. Exit `0` means aggregate
readiness is `1`; Warning, Critical, and unknown results exit `1`. Unexpected errors, missing
Docker access, missing or unsafe runtime configuration, malformed state, and uncertain values
fail closed.
Keys and values are stable:

| Key | Values |
| --- | --- |
| `postgres_health` | `1` healthy, `0` unhealthy, `-1` unknown |
| `collector_health` | `2` healthy, `1` running but unhealthy, `0` stopped, `-1` unknown |
| `collector_restart_count` | historical Docker restart count, `-1` unknown |
| `collector_restart_state` | fixed stable/history/recent/loop/unhealthy/unknown enum |
| `collector_restart_loop` | `0` stable/history, `1` recent/active/unhealthy, `-1` unknown |
| `data_paths_writable` | `2` not applicable, `1` required paths ready, `0` invalid, `-1` unknown |
| `storage_state` | `ready`, `not_applicable`, `required_path_missing`, `required_path_unwritable`, `inconsistent`, or `unknown` |
| `backup_fresh` | `1` latest protected managed backup is at most 26 hours old, otherwise `0` or `-1` |
| `disk_safe` | `1` at least 3 GiB free, `0` below threshold, `-1` unknown |
| `swap_safe` | `1` no more than 256 MiB used, `0` above threshold, `-1` unknown |
| `dashboard_disabled` | `1` dashboard profile inactive, `0` active, `-1` unknown |
| `ports_safe` | `1` no project host ports, `0` exposed, `-1` unknown |
| `runtime_safe` | `1` dashboard absent/profile-gated and project ports absent, otherwise `0` or `-1` |
| `failure_summary_state` | `0` no retained exit, `1` retained sanitized exit summary, `-1` unreadable/unsafe summary |
| `failure_summary_age_seconds` | non-negative age when retained, otherwise `-1` |
| `failure_exit_code` | retained collector exit code `0..255`, otherwise `-1` |
| `failure_oom_killed` | retained OOM flag `0` or `1`, otherwise `-1` |
| `readiness` | `1` healthy, `2` isolated low-risk swap warning, `0` confirmed critical, `-1` unknown |

The 26-hour backup window supports a daily backup schedule with two hours of scheduling
jitter. It is a monitoring threshold, not authorization to create or delete backups.

## Aggregate readiness states

`readiness` is the canonical four-state decision produced by the repository, not a Zabbix
calculated item. It retains the existing `256 MiB` swap threshold and adds bounded read-only
context from two samples taken during the existing five-second observation window.
Pressure is sustained when available RAM falls below `256 MiB`, swap usage or combined swap
page activity rises by more than `1 MiB` in that window, or memory PSI `full avg10` exceeds
`1%`. These are readiness-context guards; they do not change the `256 MiB` swap threshold.

- `1` is healthy only when every hard gate passes, swap is within its threshold, available RAM
  is at least `256 MiB`, and no sustained pressure is detected.
- `2` is Warning only when the swap threshold alone is exceeded, all other hard gates pass,
  available RAM is at least `256 MiB`, and the bounded pressure evidence is inactive.
- `0` is Critical for confirmed database, collector, restart, storage, backup, disk, runtime,
  port, dashboard, low-RAM, or sustained-pressure failure.
- `-1` is unknown for missing, stale, malformed, unsupported, or contradictory inputs. It is
  never converted to healthy or Warning.

Historical restart remains neutral, and `not_applicable` storage remains neutral for the
database-backed collector while the dashboard profile is disabled.

## Least-privilege Zabbix integration

The supported architecture never grants the Zabbix account Docker or sudo access. A
root-owned bounded oneshot runs every 60 seconds and atomically replaces a
`0640 root:zabbix` cache under `/run`. The fixed-key reader accepts no arbitrary item name.
The cache has a 150-second maximum age, 2 KiB size limit, exact schema, and only bounded
integers/enums plus an epoch timestamp. Missing, stale, malformed, oversized, duplicate-key,
unsafe-owner, unsafe-mode, or inconsistent data returns `-1`.

Repository assets include `scripts/zabbix_cache.py`, `scripts/collect_failure_retention.py`,
the monitor timer and bounded failure-retention service templates,
`deploy/zabbix/hibachi-collect.conf`, and the install, validation, and rollback scripts.
Installation is separately approved privileged work. Supply required absolute paths without
printing them:

```sh
HIBACHI_DEPLOY_DIR=... HIBACHI_RUNTIME_ENV=... HIBACHI_BACKUP_DIR=... \
ZABBIX_AGENT_CONFIG=... ZABBIX_INCLUDE_DIR=... \
sh scripts/install_zabbix_monitoring.sh
```

The installer preserves the original agent configuration, installs only project files,
adds one exact include line when absent, validates agent and unit syntax, and reloads
systemd. It does not enable the timer or restart the agent. After a manual oneshot succeeds,
validate the cache, restart only the agent if reload is unsupported, then enable the timer.

Create ten readiness signals, two restart-history diagnostics, and four neutral failure-retention diagnostics at a 60-second interval:

| Item key | Healthy value |
| --- | --- |
| `hibachi.collect.postgres` | `1` |
| `hibachi.collect.collector` | `2` |
| `hibachi.collect.restart` | `0` |
| `hibachi.collect.restart_count` | non-negative bounded count |
| `hibachi.collect.restart_state` | `0` stable, `1` history, `2` recent, `3` loop, `4` unhealthy |
| `hibachi.collect.storage` | `1` or neutral `2` |
| `hibachi.collect.backup` | `1` |
| `hibachi.collect.disk` | `1` |
| `hibachi.collect.swap` | `1` |
| `hibachi.collect.dashboard` | `1` |
| `hibachi.collect.ports` | `1` |
| `hibachi.collect.readiness` | `1` |
| `hibachi.collect.failure_state` | `0` none, `1` retained, `-1` unsafe/unknown |
| `hibachi.collect.failure_age` | non-negative seconds or `-1` |
| `hibachi.collect.failure_exit` | `0..255` or `-1` |
| `hibachi.collect.failure_oom` | `0` or `1`, otherwise `-1` |

Suggested triggers:

1. `postgres_health<>1` for two consecutive polls.
2. `collector_health<>2` for two consecutive polls.
3. `collector_restart_loop<>0` immediately; the raw count is informational.
4. `data_paths_writable<1` immediately; `2` is neutral DB-only storage.
5. `backup_fresh<>1` for two consecutive polls.
6. `disk_safe<>1` immediately.
7. `swap_safe<>1` for five consecutive polls.
8. `dashboard<>1` immediately.
9. `ports<>1` immediately.
10. `readiness<>1` for two consecutive polls.

Treat `-1` as unhealthy, not as missing data. Keep the swap trigger at Warning. Replace the
single readiness trigger with a Warning trigger for `readiness=2` and a High trigger for
`readiness=0` or `readiness=-1`; both recover only after two consecutive `readiness=1` polls.
The stale-cache trigger remains High. Alert text must contain only the item key and fixed
numeric value.

Roll out in this order: deploy the repository/cache implementation, deploy the exact merged
revision, update the Zabbix readiness triggers, validate `2` as Warning and `0|-1` as High,
then confirm recovery on `1`. Existing binary cache values `0` and `1` remain valid. During a
mixed rollout, a new `2` can temporarily match the old `readiness<>1` High trigger; this is
safe and must not be suppressed. Roll back by restoring the previous repository/cache revision
and previous readiness trigger; mixed rollback also fails closed as High until binary output is
restored.

The shared bounded classifier samples twice, five seconds apart. Historical static restarts
remain visible but do not alert. `recent_restart`, `restart_loop`, `unhealthy`, and
`unknown` alert; recover only after two consecutive `healthy_stable` or
`historical_restart` states. See the operations runbook for five-minute recent and
three-within-30-minute repeated thresholds.

Storage classification is also shared with operational preflight. The DB-backed collector
has no filesystem sink, so disabled dashboard mounts are `not_applicable` and healthy.
If an enabled service declares dataset/report storage, every expected mount must exist and
be writable by UID 10001. Missing, unwritable, inconsistent, malformed, or unavailable
state alerts fail closed. Recovery requires two consecutive `ready` or `not_applicable`
polls; monitoring never creates or repairs a directory.

## Alert response

Run `collect_ops.sh status`, then `collect_ops.sh preflight`. For collector or database
failure, inspect only bounded redacted logs. For stale backup, verify the protected backup
directory and separately authorize a backup; never delete unknown files. Low disk, high
swap, path-permission failure, dashboard presence, or port publication requires stopping
the next operational change and human review. Monitoring never authorizes a restart,
restore, migration, deployment, dashboard activation, or trading action.

Stream continuity and capacity forecasting remain a separate on-demand inspection. Use
`collect_quality.py` as documented in `docs/retention_readiness.md`; do not embed its
potentially expensive full-history mode in a frequent monitoring item.

## Bounded failure retention

The root-owned retention listener observes only Docker `die` events for the collector. It
does not restart, recreate, or otherwise alter a container. For each observed exit it stores
one root-only JSON summary: UTC timestamp, exit code, restart count, fixed failure stage and
error class, at most twelve sanitized stderr lines, OOM flag, and deployed revision. It
redacts credentials, URLs, addresses, hostnames, and paths; tracebacks are discarded. The
state directory retains at most 20 exact service-owned records plus `latest.json`, each no
larger than 2 KiB. The four Zabbix fields above are neutral diagnostics and never alter
readiness or trigger recovery logic. If a summary is malformed, stale cache collection
continues fail-closed for that diagnostic field without exposing its contents.

## Rollback

Disable the project timer, run `rollback_zabbix_monitoring.sh` with the same two Zabbix
configuration path variables, validate the restored configuration, and restart only the
agent if required. Rollback never touches PostgreSQL, collector, Docker resources, backups,
or application data.
