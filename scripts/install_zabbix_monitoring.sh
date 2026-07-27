#!/bin/sh
set -eu

# Root-operated installer. It deliberately has no passwordless-sudo variant:
# the deploy checkout is writable by the deployment account and must not become
# a privileged command source.
umask 077

STATE_DIR=/var/lib/hibachi-collect-monitor
INSTALL_CONFIG=/etc/hibachi-collect-monitor-install.conf
MANIFEST="$STATE_DIR/installed-manifest"
transaction=

fail() {
    printf '%s\n' "monitoring installation failed" >&2
    exit 1
}

safe_path() {
    case "$1" in
        /*)
            case "$1" in *[!A-Za-z0-9_./-]*) return 1 ;; esac
            ;;
        *) return 1 ;;
    esac
}

load_configuration() {
    if [ -f "$INSTALL_CONFIG" ]; then
        [ ! -L "$INSTALL_CONFIG" ] || fail
        [ "$(stat -c %U:%G:%a "$INSTALL_CONFIG")" = root:root:600 ] || fail
        # Values are root-written, absolute paths restricted by safe_path below.
        # shellcheck disable=SC1090
        . "$INSTALL_CONFIG"
    fi
    : "${HIBACHI_DEPLOY_DIR:?set HIBACHI_DEPLOY_DIR}"
    : "${HIBACHI_RUNTIME_ENV:?set HIBACHI_RUNTIME_ENV}"
    : "${HIBACHI_BACKUP_DIR:?set HIBACHI_BACKUP_DIR}"
    : "${ZABBIX_AGENT_CONFIG:?set ZABBIX_AGENT_CONFIG}"
    : "${ZABBIX_INCLUDE_DIR:?set ZABBIX_INCLUDE_DIR}"
    for value in "$HIBACHI_DEPLOY_DIR" "$HIBACHI_RUNTIME_ENV" "$HIBACHI_BACKUP_DIR" \
        "$ZABBIX_AGENT_CONFIG" "$ZABBIX_INCLUDE_DIR"; do
        safe_path "$value" || fail
    done
    export HIBACHI_DEPLOY_DIR HIBACHI_RUNTIME_ENV HIBACHI_BACKUP_DIR ZABBIX_AGENT_CONFIG \
        ZABBIX_INCLUDE_DIR
}

revision() {
    git -C "$HIBACHI_DEPLOY_DIR" rev-parse --verify HEAD 2>/dev/null
}

source_file() {
    printf '%s/%s' "$HIBACHI_DEPLOY_DIR" "$1"
}

asset_lines() {
    cat <<'EOF'
scripts/zabbix_cache.py|/usr/local/libexec/hibachi-zabbix-cache|0755
scripts/collect_failure_retention.py|/usr/local/libexec/hibachi-collect-failure-retention|0755
deploy/systemd/hibachi-collect-monitor.service|/etc/systemd/system/hibachi-collect-monitor.service|0644
deploy/systemd/hibachi-collect-monitor.timer|/etc/systemd/system/hibachi-collect-monitor.timer|0644
deploy/systemd/hibachi-collect-failure-retention.service|/etc/systemd/system/hibachi-collect-failure-retention.service|0644
deploy/zabbix/hibachi-collect.conf|ZABBIX_INCLUDE|0644
EOF
}

snapshot_file() {
    name=$1
    target=$2
    case "$name" in *[!A-Za-z0-9_.-]*) fail ;; esac
    if [ -e "$target" ] && [ ! -L "$target" ]; then
        cp -p -- "$target" "$transaction/$name"
        printf '%s\n' present >"$transaction/$name.state"
    else
        printf '%s\n' absent >"$transaction/$name.state"
    fi
}

restore_file() {
    name=$1
    target=$2
    [ -f "$transaction/$name.state" ] || return 1
    case "$(cat "$transaction/$name.state")" in
        present) install -o root -g root -m "$(stat -c %a "$transaction/$name")" "$transaction/$name" "$target" ;;
        absent) rm -f -- "$target" ;;
        *) return 1 ;;
    esac
}

snapshot_current() {
    transaction=$(mktemp -d "$STATE_DIR/rollback/transaction.XXXXXX")
    snapshot_file agent-config "$ZABBIX_AGENT_CONFIG"
    snapshot_file monitor-config /etc/hibachi-collect-monitor.conf
    snapshot_file install-config "$INSTALL_CONFIG"
    snapshot_file manifest "$MANIFEST"
    asset_lines | while IFS='|' read -r source target mode; do
        [ "$target" = ZABBIX_INCLUDE ] && target="$ZABBIX_INCLUDE_DIR/hibachi-collect.conf"
        snapshot_file "$(basename "$target")" "$target"
    done
}

restore_current() {
    [ -n "$transaction" ] && [ -d "$transaction" ] || return 0
    restore_file agent-config "$ZABBIX_AGENT_CONFIG" || return 1
    restore_file monitor-config /etc/hibachi-collect-monitor.conf || return 1
    restore_file install-config "$INSTALL_CONFIG" || return 1
    restore_file manifest "$MANIFEST" || return 1
    asset_lines | while IFS='|' read -r source target mode; do
        [ "$target" = ZABBIX_INCLUDE ] && target="$ZABBIX_INCLUDE_DIR/hibachi-collect.conf"
        restore_file "$(basename "$target")" "$target" || exit 1
    done
    systemctl daemon-reload >/dev/null 2>&1 || return 1
    systemctl reload zabbix-agent.service >/dev/null 2>&1 || systemctl restart zabbix-agent.service >/dev/null 2>&1 || return 1
}

rollback_on_error() {
    status=$?
    if [ "$status" -ne 0 ]; then
        restore_current >/dev/null 2>&1 || true
    fi
    exit "$status"
}

write_install_config() {
    temporary=$(mktemp)
    {
        printf 'HIBACHI_DEPLOY_DIR=%s\n' "$HIBACHI_DEPLOY_DIR"
        printf 'HIBACHI_RUNTIME_ENV=%s\n' "$HIBACHI_RUNTIME_ENV"
        printf 'HIBACHI_BACKUP_DIR=%s\n' "$HIBACHI_BACKUP_DIR"
        printf 'ZABBIX_AGENT_CONFIG=%s\n' "$ZABBIX_AGENT_CONFIG"
        printf 'ZABBIX_INCLUDE_DIR=%s\n' "$ZABBIX_INCLUDE_DIR"
    } >"$temporary"
    install -o root -g root -m 0600 "$temporary" "$INSTALL_CONFIG" || {
        rm -f -- "$temporary"
        fail
    }
    rm -f -- "$temporary"
}

write_monitor_config() {
    temporary=$(mktemp)
    {
        printf 'HIBACHI_DEPLOY_DIR=%s\n' "$HIBACHI_DEPLOY_DIR"
        printf 'HIBACHI_RUNTIME_ENV=%s\n' "$HIBACHI_RUNTIME_ENV"
        printf 'HIBACHI_BACKUP_DIR=%s\n' "$HIBACHI_BACKUP_DIR"
        printf 'HIBACHI_MONITOR_COMMAND=%s/scripts/collect_monitor.py\n' "$HIBACHI_DEPLOY_DIR"
        printf 'HIBACHI_OWNER_UID=%s\n' "$(stat -c %u "$HIBACHI_RUNTIME_ENV")"
    } >"$temporary"
    install -o root -g root -m 0600 "$temporary" /etc/hibachi-collect-monitor.conf || {
        rm -f -- "$temporary"
        fail
    }
    rm -f -- "$temporary"
}

write_manifest() {
    temporary=$(mktemp)
    printf 'revision=%s\n' "$(revision)" >"$temporary"
    asset_lines | while IFS='|' read -r source target mode; do
        [ -n "$source" ] || continue
        [ "$target" = ZABBIX_INCLUDE ] && target="$ZABBIX_INCLUDE_DIR/hibachi-collect.conf"
        printf '%s|%s|%s\n' "$source" "$target" "$(sha256sum "$target" | awk '{print $1}')"
    done >>"$temporary"
    install -o root -g root -m 0600 "$temporary" "$MANIFEST" || {
        rm -f -- "$temporary"
        fail
    }
    rm -f -- "$temporary"
}

verify_manifest() {
    [ -f "$MANIFEST" ] && [ ! -L "$MANIFEST" ] || fail
    [ "$(stat -c %U:%G:%a "$MANIFEST")" = root:root:600 ] || fail
    expected="revision=$(revision)"
    IFS= read -r actual <"$MANIFEST" || fail
    [ "$actual" = "$expected" ] || fail
    [ "$(tail -n +2 "$MANIFEST" | wc -l)" -eq "$(asset_lines | wc -l)" ] || fail
    asset_lines | while IFS='|' read -r source target mode; do
        [ "$target" = ZABBIX_INCLUDE ] && target="$ZABBIX_INCLUDE_DIR/hibachi-collect.conf"
        awk -F'|' -v source="$source" -v target="$target" \
            'NR > 1 && $1 == source && $2 == target { found += 1 } END { exit found == 1 ? 0 : 1 }' \
            "$MANIFEST" || fail
    done
    tail -n +2 "$MANIFEST" | while IFS='|' read -r source target digest; do
        case "$source:$target:$digest" in *'::'*|*'|'*) fail ;; esac
        [ -f "$(source_file "$source")" ] && [ -f "$target" ] || fail
        [ "$(sha256sum "$(source_file "$source")" | awk '{print $1}')" = "$digest" ] || fail
        [ "$(sha256sum "$target" | awk '{print $1}')" = "$digest" ] || fail
    done
}

agent_test() {
    for item in readiness failure_state failure_age failure_exit failure_oom; do
        zabbix_agentd -t "hibachi.collect.$item" -c "$ZABBIX_AGENT_CONFIG" >/dev/null 2>&1 || fail
    done
}

activate() {
    systemctl daemon-reload
    systemctl start hibachi-collect-monitor.service
    systemctl enable --now hibachi-collect-monitor.timer
    systemctl enable --now hibachi-collect-failure-retention.service
    if ! systemctl reload zabbix-agent.service >/dev/null 2>&1; then
        systemctl restart zabbix-agent.service
    fi
}

install_monitoring() {
    [ "$(id -u)" -eq 0 ] || fail
    load_configuration
    [ -d "$HIBACHI_DEPLOY_DIR/.git" ] || fail
    [ -f "$HIBACHI_RUNTIME_ENV" ] && [ ! -L "$HIBACHI_RUNTIME_ENV" ] || fail
    [ "$(stat -c %a "$HIBACHI_RUNTIME_ENV")" = 600 ] || fail
    [ -f "$ZABBIX_AGENT_CONFIG" ] && [ ! -L "$ZABBIX_AGENT_CONFIG" ] || fail
    getent group zabbix >/dev/null || fail
    command -v zabbix_agentd >/dev/null || fail
    command -v systemd-analyze >/dev/null || fail
    revision | grep -Eq '^[0-9a-f]{40}$' || fail
    git -C "$HIBACHI_DEPLOY_DIR" diff --quiet || fail
    test -z "$(git -C "$HIBACHI_DEPLOY_DIR" status --porcelain)" || fail

    install -d -o root -g root -m 0700 "$STATE_DIR/rollback"
    snapshot_current
    trap rollback_on_error EXIT HUP INT TERM
    install -d -o root -g root -m 0755 /usr/local/libexec
    install -d -o root -g root -m 0755 "$ZABBIX_INCLUDE_DIR"
    if [ ! -f "$STATE_DIR/rollback/zabbix_agentd.conf.original" ]; then
        install -o root -g root -m 0600 "$ZABBIX_AGENT_CONFIG" \
            "$STATE_DIR/rollback/zabbix_agentd.conf.original"
    fi
    asset_lines | while IFS='|' read -r source target mode; do
        [ "$target" = ZABBIX_INCLUDE ] && target="$ZABBIX_INCLUDE_DIR/hibachi-collect.conf"
        install -o root -g root -m "$mode" "$(source_file "$source")" "$target"
    done

    include_line="Include=$ZABBIX_INCLUDE_DIR/*.conf"
    if ! grep -Fqx "$include_line" "$ZABBIX_AGENT_CONFIG"; then
        printf '\n%s\n' "$include_line" >>"$ZABBIX_AGENT_CONFIG"
    fi
    [ "$(grep -Fxc "$include_line" "$ZABBIX_AGENT_CONFIG")" -eq 1 ] || fail
    write_install_config
    write_monitor_config
    systemd-analyze verify \
        /etc/systemd/system/hibachi-collect-monitor.service \
        /etc/systemd/system/hibachi-collect-monitor.timer \
        /etc/systemd/system/hibachi-collect-failure-retention.service >/dev/null 2>&1 || fail
    agent_test
    write_manifest
    verify_manifest
    activate
    verify_manifest
    sh "$(source_file scripts/validate_zabbix_monitoring.sh)" || fail
    trap - EXIT HUP INT TERM
    printf '%s\n' "monitoring installation passed"
}

case "${1:-install}" in
    install) install_monitoring ;;
    verify)
        [ "$(id -u)" -eq 0 ] || fail
        load_configuration
        verify_manifest
        agent_test
        sh "$(source_file scripts/validate_zabbix_monitoring.sh)"
        printf '%s\n' "monitoring verification passed"
        ;;
    *) fail ;;
esac
