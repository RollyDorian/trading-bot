#!/bin/sh
set -eu

umask 077

fail() {
    printf '%s\n' "monitoring rollback failed" >&2
    exit 1
}

[ "$(id -u)" -eq 0 ] || fail
: "${ZABBIX_AGENT_CONFIG:?set ZABBIX_AGENT_CONFIG}"
: "${ZABBIX_INCLUDE_DIR:?set ZABBIX_INCLUDE_DIR}"
case "$ZABBIX_AGENT_CONFIG:$ZABBIX_INCLUDE_DIR" in /*:/*) ;; *) fail ;; esac

rollback=/var/lib/hibachi-collect-monitor/rollback/zabbix_agentd.conf.original
[ -f "$rollback" ] || fail
systemctl disable --now hibachi-collect-monitor.timer >/dev/null 2>&1 || true
install -o root -g root -m 0644 "$rollback" "$ZABBIX_AGENT_CONFIG"
rm -f -- "$ZABBIX_INCLUDE_DIR/hibachi-collect.conf"
rm -f -- /etc/systemd/system/hibachi-collect-monitor.service
rm -f -- /etc/systemd/system/hibachi-collect-monitor.timer
rm -f -- /etc/hibachi-collect-monitor.conf
rm -f -- /usr/local/libexec/hibachi-zabbix-cache
rm -f -- /run/hibachi-collect-monitor/metrics.json
systemctl daemon-reload
printf '%s\n' "monitoring rollback staged"
