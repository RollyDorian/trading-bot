# COLLECT-only local monitoring contract

Monitoring is a host-local, provider-neutral read-only check. It opens no listener, sends
no alert itself, performs no automatic remediation, and does not start, stop, or recreate
a container. Run it through the existing non-root deployment account with the same
protected variables used by `collect_ops.sh`:

```sh
python3 scripts/collect_monitor.py
```

The command writes exactly one compact JSON object and no stderr. Exit `0` means aggregate
readiness is `1`; every other result exits `1`. Unexpected errors, missing Docker access,
missing or unsafe runtime configuration, malformed state, and uncertain values fail closed.
Keys and values are stable:

| Key | Values |
| --- | --- |
| `postgres_health` | `1` healthy, `0` unhealthy, `-1` unknown |
| `collector_health` | `2` healthy, `1` running but unhealthy, `0` stopped, `-1` unknown |
| `collector_restart_count` | historical Docker restart count, `-1` unknown |
| `collector_restart_state` | fixed stable/history/recent/loop/unhealthy/unknown enum |
| `collector_restart_loop` | `0` stable/history, `1` recent/active/unhealthy, `-1` unknown |
| `data_paths_writable` | `1` UID 10001 permission contract valid, `0` invalid, `-1` unknown |
| `backup_fresh` | `1` latest protected managed backup is at most 26 hours old, otherwise `0` or `-1` |
| `disk_safe` | `1` at least 3 GiB free, `0` below threshold, `-1` unknown |
| `swap_safe` | `1` no more than 256 MiB used, `0` above threshold, `-1` unknown |
| `runtime_safe` | `1` dashboard absent/profile-gated and project ports absent, otherwise `0` or `-1` |
| `readiness` | `1` only when every gate passes, otherwise `0` |

The 26-hour backup window supports a daily backup schedule with two hours of scheduling
jitter. It is a monitoring threshold, not authorization to create or delete backups.

## Zabbix-compatible example

Use a protected local wrapper that exports the three required path variables without
printing them, then executes the command above. A generic agent entry is:

```text
UserParameter=hibachi.collect.monitor,/absolute/protected/monitor-wrapper
```

Create one master text item `hibachi.collect.monitor` with a 60-second interval, ten
numeric dependent items and one text state item using JSONPath `$.<key>`. Keep history for
numeric items; do not store wrapper output in shared logs. Suggested triggers:

1. `postgres_health<>1` for two consecutive polls.
2. `collector_health<>2` for two consecutive polls.
3. `collector_restart_loop<>0` immediately; the raw count is informational.
4. `data_paths_writable<>1` immediately.
5. `backup_fresh<>1` for two consecutive polls.
6. `disk_safe<>1` immediately.
7. `swap_safe<>1` for five consecutive polls.
8. `runtime_safe<>1` immediately.

Treat `-1` as unhealthy, not as missing data. Recover a trigger only after two consecutive
healthy polls, except high swap, which requires five. Alert text must contain only the item
key and fixed numeric value.

The shared bounded classifier samples twice, five seconds apart. Historical static restarts
remain visible but do not alert. `recent_restart`, `restart_loop`, `unhealthy`, and
`unknown` alert; recover only after two consecutive `healthy_stable` or
`historical_restart` states. See the operations runbook for five-minute recent and
three-within-30-minute repeated thresholds.

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
