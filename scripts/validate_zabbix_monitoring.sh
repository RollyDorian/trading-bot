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
        readiness:-1|readiness:0|readiness:1|readiness:2)
            ;;
        collector:-1|collector:0|collector:1|collector:2|storage:-1|storage:0|storage:1|storage:2)
            ;;
        postgres:-1|postgres:0|postgres:1|restart:-1|restart:0|restart:1|backup:-1|backup:0|backup:1|disk:-1|disk:0|disk:1|swap:-1|swap:0|swap:1|dashboard:-1|dashboard:0|dashboard:1|ports:-1|ports:0|ports:1)
            ;;
        *) fail ;;
    esac
done

id zabbix | grep -q docker && fail
sudo -l -U zabbix 2>/dev/null | grep -q hibachi && fail
printf '%s\n' "monitoring validation passed"
