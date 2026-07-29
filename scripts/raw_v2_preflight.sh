#!/bin/sh
set -eu

umask 077

fail() {
    printf '%s\n' "raw-v2-preflight failed" >&2
    exit 1
}

: "${HIBACHI_DEPLOY_DIR:?set HIBACHI_DEPLOY_DIR}"
: "${HIBACHI_RUNTIME_ENV:?set HIBACHI_RUNTIME_ENV}"

[ -f "$HIBACHI_DEPLOY_DIR/compose.production.yaml" ] || fail
[ -f "$HIBACHI_RUNTIME_ENV" ] || fail

compose() {
    timeout 15 docker compose \
        --env-file "$HIBACHI_RUNTIME_ENV" \
        -f "$HIBACHI_DEPLOY_DIR/compose.production.yaml" \
        "$@"
}

service_id() {
    compose ps -q "$1" 2>/dev/null
}

service_health() {
    container_id=$(service_id "$1")
    [ -n "$container_id" ] || fail
    timeout 5 docker inspect \
        --format '{{if and .State.Running .State.Health}}{{.State.Health.Status}}{{else}}unhealthy{{end}}' \
        "$container_id" 2>/dev/null
}

postgres_health=$(service_health postgres)
collector_health=$(service_health collector)
[ "$postgres_health" = healthy ] || fail
[ "$collector_health" = healthy ] || fail

database_metrics=$(
    compose exec -T postgres sh -eu -c '
        exec psql -X -qAt -v ON_ERROR_STOP=1 \
            -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
                BEGIN READ ONLY;
                SET LOCAL statement_timeout = '\''5s'\'';
                SELECT
                    current_setting('\''server_version_num'\''),
                    count(*)::bigint,
                    pg_relation_size('\''market_events'\''::regclass),
                    pg_indexes_size('\''market_events'\''::regclass),
                    (
                        SELECT count(*)
                        FROM pg_stat_activity
                        WHERE xact_start IS NOT NULL
                          AND pid <> pg_backend_pid()
                    ),
                    (
                        SELECT count(*)
                        FROM pg_locks
                        WHERE relation = '\''market_events'\''::regclass
                          AND pid <> pg_backend_pid()
                    ),
                    (
                        SELECT count(*)
                        FROM pg_locks
                        WHERE relation = '\''market_events'\''::regclass
                          AND NOT granted
                    )
                FROM market_events;
                COMMIT;
            "
    ' 2>/dev/null
) || fail

metrics_line=$(printf '%s\n' "$database_metrics" | awk -F '|' 'NF == 7 {print; count++} END {if (count != 1) exit 1}') \
    || fail
IFS='|' read -r postgres_version row_count heap_bytes index_bytes active_transactions relation_locks waiting_locks <<EOF
$metrics_line
EOF

for value in \
    "$postgres_version" "$row_count" "$heap_bytes" "$index_bytes" \
    "$active_transactions" "$relation_locks" "$waiting_locks"
do
    case "$value" in
        ''|*[!0-9]*) fail ;;
    esac
done

disk_free_kib=$(df -Pk "$HIBACHI_DEPLOY_DIR" | awk 'NR == 2 {print $4}')
case "$disk_free_kib" in
    ''|*[!0-9]*) fail ;;
esac

printf '%s\n' \
    "postgres_health=healthy collector_health=healthy postgres_version=$postgres_version row_count=$row_count heap_bytes=$heap_bytes index_bytes=$index_bytes active_transactions=$active_transactions relation_locks=$relation_locks waiting_locks=$waiting_locks disk_free_kib=$disk_free_kib"
