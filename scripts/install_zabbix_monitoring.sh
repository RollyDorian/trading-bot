#!/bin/sh
set -eu

umask 077

fail() {
    printf '%s\n' "monitoring installation failed" >&2
    exit 1
}

[ "$(id -u)" -eq 0 ] || fail
: "${HIBACHI_DEPLOY_DIR:?set HIBACHI_DEPLOY_DIR}"
: "${HIBACHI_RUNTIME_ENV:?set HIBACHI_RUNTIME_ENV}"
: "${HIBACHI_BACKUP_DIR:?set HIBACHI_BACKUP_DIR}"
: "${ZABBIX_AGENT_CONFIG:?set ZABBIX_AGENT_CONFIG}"
: "${ZABBIX_INCLUDE_DIR:?set ZABBIX_INCLUDE_DIR}"

for value in "$HIBACHI_DEPLOY_DIR" "$HIBACHI_RUNTIME_ENV" "$HIBACHI_BACKUP_DIR" \
    "$ZABBIX_AGENT_CONFIG" "$ZABBIX_INCLUDE_DIR"; do
    case "$value" in /*) ;; *) fail ;; esac
done
[ -d "$HIBACHI_DEPLOY_DIR/.git" ] || fail
[ -f "$HIBACHI_RUNTIME_ENV" ] || fail
[ "$(stat -c %a "$HIBACHI_RUNTIME_ENV")" = 600 ] || fail
[ -f "$ZABBIX_AGENT_CONFIG" ] && [ ! -L "$ZABBIX_AGENT_CONFIG" ] || fail
getent group zabbix >/dev/null || fail
command -v zabbix_agentd >/dev/null || fail
command -v systemd-analyze >/dev/null || fail

rollback_dir=/var/lib/hibachi-collect-monitor/rollback
install -d -o root -g root -m 0700 "$rollback_dir"
if [ ! -f "$rollback_dir/zabbix_agentd.conf.original" ]; then
    install -o root -g root -m 0600 "$ZABBIX_AGENT_CONFIG" \
        "$rollback_dir/zabbix_agentd.conf.original"
fi

install -o root -g root -m 0755 \
    "$HIBACHI_DEPLOY_DIR/scripts/zabbix_cache.py" \
    /usr/local/libexec/hibachi-zabbix-cache
install -o root -g root -m 0644 \
    "$HIBACHI_DEPLOY_DIR/deploy/systemd/hibachi-collect-monitor.service" \
    /etc/systemd/system/hibachi-collect-monitor.service
install -o root -g root -m 0644 \
    "$HIBACHI_DEPLOY_DIR/deploy/systemd/hibachi-collect-monitor.timer" \
    /etc/systemd/system/hibachi-collect-monitor.timer
install -d -o root -g root -m 0755 "$ZABBIX_INCLUDE_DIR"
install -o root -g root -m 0644 \
    "$HIBACHI_DEPLOY_DIR/deploy/zabbix/hibachi-collect.conf" \
    "$ZABBIX_INCLUDE_DIR/hibachi-collect.conf"

include_line="Include=$ZABBIX_INCLUDE_DIR/*.conf"
if ! grep -Fqx "$include_line" "$ZABBIX_AGENT_CONFIG"; then
    printf '\n%s\n' "$include_line" >>"$ZABBIX_AGENT_CONFIG"
fi
[ "$(grep -Fxc "$include_line" "$ZABBIX_AGENT_CONFIG")" -eq 1 ] || fail

config_tmp=$(mktemp)
trap 'rm -f -- "$config_tmp"' EXIT HUP INT TERM
{
    printf 'HIBACHI_DEPLOY_DIR=%s\n' "$HIBACHI_DEPLOY_DIR"
    printf 'HIBACHI_RUNTIME_ENV=%s\n' "$HIBACHI_RUNTIME_ENV"
    printf 'HIBACHI_BACKUP_DIR=%s\n' "$HIBACHI_BACKUP_DIR"
    printf 'HIBACHI_MONITOR_COMMAND=%s/scripts/collect_monitor.py\n' "$HIBACHI_DEPLOY_DIR"
    printf 'HIBACHI_OWNER_UID=%s\n' "$(stat -c %u "$HIBACHI_RUNTIME_ENV")"
} >"$config_tmp"
install -o root -g root -m 0600 "$config_tmp" /etc/hibachi-collect-monitor.conf

systemd-analyze verify \
    /etc/systemd/system/hibachi-collect-monitor.service \
    /etc/systemd/system/hibachi-collect-monitor.timer >/dev/null 2>&1 || fail
zabbix_agentd -t hibachi.collect.readiness -c "$ZABBIX_AGENT_CONFIG" \
    >/dev/null 2>&1 || fail
systemctl daemon-reload
printf '%s\n' "monitoring installation staged"
