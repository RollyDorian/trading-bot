#!/bin/sh
set -eu

umask 077

MIN_DISK_KIB=3145728
MIN_AVAILABLE_MEMORY_BYTES=268435456
MAX_SWAP_USED_BYTES=268435456
DEFAULT_BACKUP_RETENTION=5
MAX_BACKUP_RETENTION=20
DEFAULT_LOG_LINES=200
MAX_LOG_LINES=1000
DEFAULT_LOG_SINCE=10m
temporary_file=
validation_container=

cleanup() {
    if [ -n "$temporary_file" ]; then
        rm -f -- "$temporary_file"
    fi
    if [ -n "$validation_container" ]; then
        docker rm -f "$validation_container" >/dev/null 2>&1 || true
    fi
}

trap cleanup EXIT HUP INT TERM

fail() {
    printf '%s\n' "collect-ops failed: $1" >&2
    exit 1
}

require_absolute_path() {
    case "$2" in
        /*) ;;
        *) fail "$1 must be an absolute path" ;;
    esac
}

require_configuration() {
    : "${HIBACHI_DEPLOY_DIR:?set HIBACHI_DEPLOY_DIR}"
    : "${HIBACHI_RUNTIME_ENV:?set HIBACHI_RUNTIME_ENV}"
    : "${HIBACHI_BACKUP_DIR:?set HIBACHI_BACKUP_DIR}"
    require_absolute_path HIBACHI_DEPLOY_DIR "$HIBACHI_DEPLOY_DIR"
    require_absolute_path HIBACHI_RUNTIME_ENV "$HIBACHI_RUNTIME_ENV"
    require_absolute_path HIBACHI_BACKUP_DIR "$HIBACHI_BACKUP_DIR"
    [ -d "$HIBACHI_DEPLOY_DIR/.git" ] || fail "deployment checkout is missing"
    [ -f "$HIBACHI_DEPLOY_DIR/compose.production.yaml" ] || fail "production Compose is missing"
    [ -f "$HIBACHI_RUNTIME_ENV" ] || fail "runtime environment is missing"
    [ "$(stat -c %a "$HIBACHI_RUNTIME_ENV")" = 600 ] || fail "runtime environment mode must be 600"
    [ "$(stat -c %u "$HIBACHI_RUNTIME_ENV")" = "$(id -u)" ] || fail "runtime environment owner mismatch"
}

compose() {
    docker compose \
        --env-file "$HIBACHI_RUNTIME_ENV" \
        -f "$HIBACHI_DEPLOY_DIR/compose.production.yaml" \
        "$@"
}

service_id() {
    compose ps -q "$1"
}

require_healthy_service() {
    service=$1
    container_id=$(service_id "$service")
    [ -n "$container_id" ] || fail "$service container is absent"
    [ "$(docker inspect --format '{{.State.Running}}' "$container_id")" = true ] \
        || fail "$service is not running"
    [ "$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")" = healthy ] \
        || fail "$service is not healthy"
    [ "$(docker inspect --format '{{.RestartCount}}' "$container_id")" = 0 ] \
        || fail "$service has restarted"
    [ -z "$(docker port "$container_id")" ] || fail "$service publishes a host port"
}

validate_compose_policy() {
    temporary_file=$(mktemp)
    compose config --quiet >/dev/null
    active_services=$(compose config --services)
    [ "$(printf '%s\n' "$active_services" | grep -c '^postgres$')" -eq 1 ] \
        || fail "PostgreSQL is missing from the default service set"
    [ "$(printf '%s\n' "$active_services" | grep -c '^collector$')" -eq 1 ] \
        || fail "collector is missing from the default service set"
    [ "$(printf '%s\n' "$active_services" | wc -l)" -eq 2 ] \
        || fail "unexpected service is enabled by default"
    compose --profile dashboard config --format json >"$temporary_file"
    python3 - "$temporary_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    services = json.load(handle)["services"]

assert {"postgres", "collector", "dashboard"} <= services.keys()
assert all(not service.get("ports") for service in services.values())
assert all(service.get("network_mode") != "host" for service in services.values())
assert int(services["postgres"]["mem_limit"]) == 268435456
assert int(services["collector"]["mem_limit"]) == 167772160
assert int(services["dashboard"]["mem_limit"]) == 83886080
assert services["collector"]["depends_on"]["postgres"]["condition"] == "service_healthy"
assert services["dashboard"]["profiles"] == ["dashboard"]
PY
    rm -f -- "$temporary_file"
    temporary_file=
}

check_resources() {
    disk_kib=$(df -Pk "$HIBACHI_DEPLOY_DIR" | awk 'NR == 2 {print $4}')
    available_memory=$(free -b | awk '/^Mem:/ {print $7}')
    swap_used=$(free -b | awk '/^Swap:/ {print $3}')
    [ "$disk_kib" -ge "$MIN_DISK_KIB" ] || fail "free disk is below 3 GiB"
    [ "$available_memory" -ge "$MIN_AVAILABLE_MEMORY_BYTES" ] \
        || fail "available memory is critically low"
    [ "$swap_used" -le "$MAX_SWAP_USED_BYTES" ] \
        || fail "swap use exceeds 256 MiB"
    printf 'resources=ok disk_kib=%s available_memory_bytes=%s swap_used_bytes=%s\n' \
        "$disk_kib" "$available_memory" "$swap_used"
}

status_command() {
    require_healthy_service postgres
    require_healthy_service collector
    [ -z "$(compose --profile dashboard ps -q dashboard)" ] || fail "dashboard is present"
    printf 'postgres=healthy collector=healthy restarts=0 dashboard=off ports=none\n'
}

preflight_command() {
    [ -z "$(git -C "$HIBACHI_DEPLOY_DIR" status --porcelain)" ] \
        || fail "deployment checkout is dirty"
    validate_compose_policy
    status_command
    check_resources
    printf 'preflight=ok revision=%s\n' "$(git -C "$HIBACHI_DEPLOY_DIR" rev-parse --short=7 HEAD)"
}

logs_command() {
    lines=${HIBACHI_LOG_LINES:-$DEFAULT_LOG_LINES}
    since=${HIBACHI_LOG_SINCE:-$DEFAULT_LOG_SINCE}
    case "$lines" in
        ''|*[!0-9]*) fail "HIBACHI_LOG_LINES must be numeric" ;;
    esac
    [ "$lines" -ge 1 ] && [ "$lines" -le "$MAX_LOG_LINES" ] \
        || fail "HIBACHI_LOG_LINES is outside the safe range"
    case "$since" in
        '') fail "HIBACHI_LOG_SINCE is invalid" ;;
    esac
    since_unit=${since#"${since%?}"}
    since_amount=${since%?}
    case "$since_amount" in
        ''|*[!0-9]*) fail "HIBACHI_LOG_SINCE is invalid" ;;
    esac
    case "$since_unit" in
        s|m|h|d) ;;
        *) fail "HIBACHI_LOG_SINCE is invalid" ;;
    esac
    temporary_file=$(mktemp)
    compose logs --no-color --since "$since" --tail "$lines" collector >"$temporary_file" 2>&1 \
        || fail "collector log inspection failed"
    sed -E \
        -e 's#([[:alpha:]][[:alnum:]+.-]*://)[^[:space:]]+#\1[REDACTED]#g' \
        -e 's/((DATABASE_URL|password|token|secret)[=:])[[:graph:]]+/\1[REDACTED]/Ig' \
        -e 's/([0-9]{1,3}\.){3}[0-9]{1,3}/[REDACTED_IP]/g' \
        -e 's/([[:alnum:]-]+\.)+[[:alpha:]]{2,}/[REDACTED_HOST]/g' \
        "$temporary_file"
    rm -f -- "$temporary_file"
    temporary_file=
}

ensure_backup_directory() {
    if [ ! -e "$HIBACHI_BACKUP_DIR" ]; then
        install -d -m 700 "$HIBACHI_BACKUP_DIR"
    fi
    [ -d "$HIBACHI_BACKUP_DIR" ] || fail "backup path is not a directory"
    [ "$(stat -c %a "$HIBACHI_BACKUP_DIR")" = 700 ] || fail "backup directory mode must be 700"
    [ "$(stat -c %u "$HIBACHI_BACKUP_DIR")" = "$(id -u)" ] || fail "backup directory owner mismatch"
}

retention_count() {
    retention=${HIBACHI_BACKUP_RETENTION:-$DEFAULT_BACKUP_RETENTION}
    case "$retention" in
        ''|*[!0-9]*) fail "HIBACHI_BACKUP_RETENTION must be numeric" ;;
    esac
    [ "$retention" -ge 1 ] && [ "$retention" -le "$MAX_BACKUP_RETENTION" ] \
        || fail "HIBACHI_BACKUP_RETENTION is outside the safe range"
    printf '%s\n' "$retention"
}

prune_managed_backups() {
    retention=$1
    find "$HIBACHI_BACKUP_DIR" -maxdepth 1 -type f \
        -name 'hibachi-????????T??????Z-???????.dump' -printf '%T@ %p\n' \
        | sort -nr \
        | awk -v keep="$retention" 'NR > keep {sub(/^[^ ]+ /, ""); print}' \
        | while IFS= read -r old_backup; do
            [ -n "$old_backup" ] || continue
            rm -f -- "$old_backup"
        done
}

backup_command() {
    status_command >/dev/null
    check_resources >/dev/null
    ensure_backup_directory
    retention=$(retention_count)
    revision=$(git -C "$HIBACHI_DEPLOY_DIR" rev-parse --short=7 HEAD)
    timestamp=$(date -u +%Y%m%dT%H%M%SZ)
    final_file="$HIBACHI_BACKUP_DIR/hibachi-$timestamp-$revision.dump"
    [ ! -e "$final_file" ] || fail "managed backup name already exists"
    temporary_file=$(mktemp "$HIBACHI_BACKUP_DIR/.hibachi-backup.XXXXXX")
    if ! compose exec -T postgres sh -c \
        'exec pg_dump --format=custom --no-owner --no-privileges --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"' \
        >"$temporary_file"; then
        rm -f -- "$temporary_file"
        fail "logical backup failed"
    fi
    [ -s "$temporary_file" ] || {
        rm -f -- "$temporary_file"
        fail "logical backup is empty"
    }
    compose exec -T postgres pg_restore --list <"$temporary_file" >/dev/null \
        || {
            rm -f -- "$temporary_file"
            fail "logical backup archive is invalid"
        }
    chmod 600 "$temporary_file"
    mv "$temporary_file" "$final_file"
    temporary_file=
    prune_managed_backups "$retention"
    managed_count=$(find "$HIBACHI_BACKUP_DIR" -maxdepth 1 -type f \
        -name 'hibachi-????????T??????Z-???????.dump' | wc -l)
    printf 'backup=created file=%s size_bytes=%s retained=%s\n' \
        "$(basename "$final_file")" "$(stat -c %s "$final_file")" "$managed_count"
}

validate_backup_file() {
    : "${HIBACHI_BACKUP_FILE:?set HIBACHI_BACKUP_FILE}"
    require_absolute_path HIBACHI_BACKUP_FILE "$HIBACHI_BACKUP_FILE"
    backup_dir_real=$(realpath "$HIBACHI_BACKUP_DIR")
    backup_file_real=$(realpath "$HIBACHI_BACKUP_FILE")
    case "$backup_file_real" in
        "$backup_dir_real"/hibachi-????????T??????Z-???????.dump) ;;
        *) fail "backup file is not a managed artifact" ;;
    esac
    [ -f "$backup_file_real" ] && [ -s "$backup_file_real" ] \
        || fail "backup file is missing or empty"
    [ "$(stat -c %a "$backup_file_real")" = 600 ] || fail "backup file mode must be 600"
    [ "$(stat -c %u "$backup_file_real")" = "$(id -u)" ] || fail "backup file owner mismatch"
    printf '%s\n' "$backup_file_real"
}

validate_restore_command() {
    status_command >/dev/null
    check_resources >/dev/null
    ensure_backup_directory
    backup_file=$(validate_backup_file)
    compose exec -T postgres pg_restore --list <"$backup_file" >/dev/null \
        || fail "backup archive list validation failed"
    validation_container="hibachi-restore-validate-$$"
    [ -z "$(docker ps -aq --filter "name=^/$validation_container$")" ] \
        || fail "restore validation container already exists"
    docker run -d --rm \
        --name "$validation_container" \
        --network none \
        --memory 192m \
        --tmpfs /var/lib/postgresql/data:rw,nosuid,nodev,size=256m \
        -e POSTGRES_HOST_AUTH_METHOD=trust \
        postgres:16-alpine >/dev/null
    ready=false
    attempts=0
    while [ "$attempts" -lt 30 ]; do
        if docker exec "$validation_container" pg_isready -U postgres >/dev/null 2>&1; then
            ready=true
            break
        fi
        attempts=$((attempts + 1))
        sleep 1
    done
    [ "$ready" = true ] || fail "isolated restore database did not become ready"
    docker exec -i "$validation_container" pg_restore \
        --exit-on-error --no-owner --no-privileges -U postgres -d postgres \
        <"$backup_file" >/dev/null
    restored_tables=$(docker exec "$validation_container" psql -At -U postgres -d postgres \
        -c "SELECT count(*) FROM pg_class WHERE relname IN ('market_events','system_events');")
    [ "$restored_tables" = 2 ] || fail "isolated restore is missing required tables"
    [ -z "$(docker port "$validation_container")" ] \
        || fail "restore validation container published a port"
    docker rm -f "$validation_container" >/dev/null
    validation_container=
    status_command >/dev/null
    printf 'restore_validation=ok isolated=true production_unchanged=true\n'
}

usage() {
    printf '%s\n' \
        'usage: collect_ops.sh {status|preflight|logs|backup|validate-backup}' >&2
    exit 2
}

require_configuration
case "${1:-}" in
    status) status_command ;;
    preflight) preflight_command ;;
    logs) logs_command ;;
    backup) backup_command ;;
    validate-backup) validate_restore_command ;;
    *) usage ;;
esac
