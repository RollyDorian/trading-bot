#!/bin/sh
set -eu

fail() {
    printf '%s\n' "monitoring validation failed" >&2
    exit 1
}

: "${ZABBIX_AGENT_CONFIG:?set ZABBIX_AGENT_CONFIG}"
cache=/run/hibachi-collect-monitor/metrics.json
[ -f "$cache" ] && [ ! -L "$cache" ] || fail
[ "$(stat -c %a "$cache")" = 640 ] || fail
[ "$(stat -c %U "$cache")" = root ] || fail
[ "$(stat -c %G "$cache")" = zabbix ] || fail

for item in postgres collector restart restart_count restart_state storage backup disk swap dashboard ports readiness; do
    value=$(zabbix_agentd -t "hibachi.collect.$item" -c "$ZABBIX_AGENT_CONFIG" 2>/dev/null \
        | sed -n 's/.*\[t|\([^]]*\)\]$/\1/p')
    case "$item:$value" in
        restart_count:[0-9]|restart_count:[0-9][0-9]*)
            [ "$value" -le 1000000 ] || fail
            ;;
        restart_state:-1|restart_state:0|restart_state:1|restart_state:2|restart_state:3|restart_state:4)
            ;;
        *:-1|*:0|*:1|*:2)
            ;;
        *) fail ;;
    esac
done

id zabbix | grep -q docker && fail
sudo -l -U zabbix 2>/dev/null | grep -q hibachi && fail
printf '%s\n' "monitoring validation passed"
