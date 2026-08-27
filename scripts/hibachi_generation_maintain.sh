#!/usr/bin/env bash
# Managed Hibachi generation maintain tick (provision + rotate metadata).
# Never DROP. Never reset sequences. Never touch external-ref.
#
# Incident fix (HIBACHI_PARTITION_RECOVERY): capacity/archive backlog STOP must
# NOT skip successor provisioning. Order is:
#   1) assess lead / urgency
#   2) provision successor when remaining <= 50k (idempotent)
#   3) rotate metadata when sequence crossed ACTIVE end
#   4) persist operator-visible status
#   5) report capacity STOP / COVER_STOP afterwards (non-silent)
set -euo pipefail
source "$HOME/.cache/hibachi-partition-env.sh"
CYCLE_ROOT="$HOME/gen-cycle"
LOG="$CYCLE_ROOT/generation-maintain.log"
STATUS_ENV="$CYCLE_ROOT/provision.status.env"
# Lead thresholds (ids). ~59k events/h → 50k≈51m, 10k≈10m, 1k≈1m.
PROVISION_LEAD=50000
LATE_LEAD=10000
COVER_STOP_LEAD=1000
ROW_SPAN=400000

FREE=$(df -B1 --output=avail "$HIBACHI_DEPLOY_DIR" | tail -n1 | tr -d ' ')
UTC_NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
LAST_ERR=""
PROVISION_RESULT="skipped"
ROTATE_RESULT="skipped"
URGENCY="NORMAL"
EXIT_CODE=0

write_status() {
  cat >"$STATUS_ENV" <<EOF
updated_utc=${UTC_NOW}
next_id=${next:-}
active_key=${active_key:-}
active_start=${active_start:-}
active_end=${active_end:-}
remaining=${remaining:-}
provision_urgency=${URGENCY}
successor_expected=g_${active_end:-}_${succ_end:-}
successor_exists=${succ_exists:-unknown}
provision_result=${PROVISION_RESULT}
rotate_result=${ROTATE_RESULT}
last_provision_attempt_utc=${UTC_NOW}
last_provision_error=${LAST_ERR}
capacity_note=${CAPACITY_NOTE:-none}
closed_n=${closed_n:-}
drop_n=${drop_n:-}
free_bytes=${FREE}
action_required=${ACTION_REQUIRED:-none}
capacity_stop_file=$([ -f "$CYCLE_ROOT/CAPACITY_STOP_REQUIRED" ] && echo present || echo absent)
collect_hold=$([ -f "$CYCLE_ROOT/COLLECT_HOLD" ] && echo present || echo absent)
EOF
}

compose() {
  # Keep the ACTIVE-bounded healthcheck bind-mount if the operator overlay exists.
  # A second 554 MiB GHCR copy would breach READY on the constrained VPS.
  local args=(-f "$HIBACHI_DEPLOY_DIR/compose.production.yaml")
  if [ -f "$HIBACHI_DEPLOY_DIR/compose.collector-healthcheck.yaml" ]; then
    args+=(-f "$HIBACHI_DEPLOY_DIR/compose.collector-healthcheck.yaml")
    export HIBACHI_HEALTHCHECK_OVERLAY="${HIBACHI_HEALTHCHECK_OVERLAY:-$HOME/gen-cycle/overlay-emergency/trading_bot/healthcheck.py}"
  fi
  docker compose --env-file "$HIBACHI_RUNTIME_ENV" "${args[@]}" "$@"
}

{
  echo "==== ${UTC_NOW} free=${FREE} ===="
  id=$(compose ps -q postgres)
  db=$(docker exec "$id" printenv POSTGRES_DB)
  user=$(docker exec "$id" printenv POSTGRES_USER)
  sql() { docker exec -i "$id" psql -v ON_ERROR_STOP=1 -At -U "$user" -d "$db" -c "$1"; }

  next=$(sql "SELECT CASE WHEN is_called THEN last_value+1 ELSE last_value END FROM market_events_id_seq")
  active_end=$(sql "SELECT id_end FROM market_event_generations WHERE state='ACTIVE'")
  active_start=$(sql "SELECT id_start FROM market_event_generations WHERE state='ACTIVE'")
  active_key=$(sql "SELECT generation_key FROM market_event_generations WHERE state='ACTIVE'")
  remaining=$((active_end - next))
  succ_end=$((active_end + ROW_SPAN))
  drop_n=$(sql "SELECT COUNT(*) FROM market_event_generations WHERE state='DROP_ELIGIBLE'")
  closed_n=$(sql "SELECT COUNT(*) FROM market_event_generations WHERE state IN ('CLOSED_UNARCHIVED','ARCHIVING','ARCHIVE_FAILED','VERIFY_FAILED')")
  succ_exists=$(sql "SELECT COUNT(*) FROM market_event_generations WHERE id_start=${active_end} AND state <> 'DROPPED'")

  if [ "$succ_exists" != "0" ]; then
    URGENCY="NORMAL"
  elif [ "$remaining" -le "$COVER_STOP_LEAD" ]; then
    URGENCY="COVER_STOP_REQUIRED"
  elif [ "$remaining" -le "$LATE_LEAD" ]; then
    URGENCY="PROVISION_LATE"
  elif [ "$remaining" -le "$PROVISION_LEAD" ]; then
    URGENCY="PROVISION_REQUIRED"
  else
    URGENCY="NORMAL"
  fi

  echo "next=$next active_end=$active_end remaining=$remaining urgency=$URGENCY successor_exists=$succ_exists closed=$closed_n drop=$drop_n"

  # 1–2) Provision BEFORE capacity STOP — empty successor DDL is cheap and
  # prevents uncovered-id INSERT failures while archive backlog is unresolved.
  if [ "$remaining" -le "$PROVISION_LEAD" ]; then
    if [ "$succ_exists" = "0" ]; then
      part="market_events_g_${active_end}"
      key="g_${active_end}_${succ_end}"
      if sql "SELECT COUNT(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname='${part}'" | grep -qx 1; then
        # Orphan physical table: fail closed rather than CREATE duplicate.
        LAST_ERR="orphan_physical_successor_${part}"
        PROVISION_RESULT="conflict_orphan_physical"
        URGENCY="COVER_STOP_REQUIRED"
        ACTION_REQUIRED="reconcile_orphan_successor"
        EXIT_CODE=4
      else
        if sql "CREATE TABLE ${part} PARTITION OF market_events FOR VALUES FROM (${active_end}) TO (${succ_end})"; then
          sql "INSERT INTO market_event_generations (generation_key, partition_name, id_start, id_end, state, row_span) VALUES ('${key}', '${part}', ${active_end}, ${succ_end}, 'PROVISIONED', ${ROW_SPAN})"
          echo "PROVISIONED $key"
          PROVISION_RESULT="created"
          succ_exists=1
          if [ "$URGENCY" = "COVER_STOP_REQUIRED" ] || [ "$URGENCY" = "PROVISION_LATE" ] || [ "$URGENCY" = "PROVISION_REQUIRED" ]; then
            URGENCY="NORMAL"
          fi
        else
          LAST_ERR="create_partition_failed_${key}"
          PROVISION_RESULT="failed"
          ACTION_REQUIRED="provision_retry"
          EXIT_CODE=5
        fi
      fi
    else
      echo "SUCCESSOR_PRESENT"
      PROVISION_RESULT="already_present"
    fi
  fi

  # 3) Rotate metadata if sequence crossed ACTIVE end and successor is writable.
  if [ "$next" -ge "$active_end" ]; then
    succ_state=$(sql "SELECT state FROM market_event_generations WHERE id_start=${active_end} AND state <> 'DROPPED' LIMIT 1")
    if [ "$succ_state" = "PROVISIONED" ] || [ "$succ_state" = "ACTIVE" ]; then
      old_key=$(sql "SELECT generation_key FROM market_event_generations WHERE state='ACTIVE'")
      bytes=$(sql "SELECT pg_total_relation_size(partition_name) FROM market_event_generations WHERE generation_key='${old_key}'")
      sql "UPDATE market_event_generations SET state='CLOSED_UNARCHIVED', physical_bytes_at_close=${bytes}, closed_at=now(), updated_at=now() WHERE generation_key='${old_key}' AND state='ACTIVE'"
      sql "UPDATE market_event_generations SET state='ACTIVE', updated_at=now() WHERE id_start=${active_end} AND state='PROVISIONED'"
      echo "ROTATED old=$old_key new_start=$active_end"
      ROTATE_RESULT="rotated"
      # Refresh active after rotation for status file.
      active_end=$(sql "SELECT id_end FROM market_event_generations WHERE state='ACTIVE'")
      active_start=$(sql "SELECT id_start FROM market_event_generations WHERE state='ACTIVE'")
      active_key=$(sql "SELECT generation_key FROM market_event_generations WHERE state='ACTIVE'")
      remaining=$((active_end - next))
      succ_end=$((active_end + ROW_SPAN))
      succ_exists=$(sql "SELECT COUNT(*) FROM market_event_generations WHERE id_start=${active_end} AND state <> 'DROPPED'")
    else
      LAST_ERR="rotation_blocked_missing_successor"
      ROTATE_RESULT="blocked_no_successor"
      URGENCY="COVER_STOP_REQUIRED"
      ACTION_REQUIRED="provision_cover_before_rotate"
      EXIT_CODE=6
    fi
  fi

  # 4) Capacity / cover reporting AFTER mutations (never silent).
  # Disk below the 5 GiB operator floor must persist CAPACITY_STOP_REQUIRED and
  # actually stop COLLECT. Archive backlog is operator-visible but must not keep
  # COLLECT stopped after disk READY (residual CLOSED drain is non-destructive).
  READY_TARGET=$((5 * 1024 * 1024 * 1024 + 203546624 + 128 * 1024 * 1024))
  DISK_STOP=0
  if [ "$FREE" -lt $((5 * 1024 * 1024 * 1024)) ]; then
    echo "STOP_REQUIRED disk_below_floor"
    echo "CAPACITY_STOP_REQUIRED disk_below_floor"
    printf 'reason=disk_below_floor\nupdated_utc=%s\nfree_bytes=%s\n' "$UTC_NOW" "$FREE" \
      >"$CYCLE_ROOT/CAPACITY_STOP_REQUIRED"
    CAPACITY_NOTE="disk_below_5gib_floor"
    ACTION_REQUIRED="${ACTION_REQUIRED:-capacity_disk}"
    EXIT_CODE=3
    DISK_STOP=1
  else
    if [ "$FREE" -ge "$READY_TARGET" ] && [ ! -f "$CYCLE_ROOT/COLLECT_HOLD" ]; then
      rm -f "$CYCLE_ROOT/CAPACITY_STOP_REQUIRED"
    fi
    if [ "$drop_n" -ge 2 ]; then
      echo "DROP_BACKLOG_LIMIT drop=$drop_n"
      CAPACITY_NOTE="drop_backlog_limit=${drop_n}"
      ACTION_REQUIRED="${ACTION_REQUIRED:-human_drop_approval}"
    fi
    if [ "$drop_n" -gt 2 ] || [ "$closed_n" -gt 1 ]; then
      echo "ARCHIVE_BACKLOG drop=$drop_n closed=$closed_n"
      CAPACITY_NOTE="backlog_drop=${drop_n}_closed=${closed_n}"
      ACTION_REQUIRED="${ACTION_REQUIRED:-archive_or_drop_backlog}"
    fi
  fi

  # Arrest COLLECT on disk capacity STOP or emergency hold. Provisioning already ran.
  if [ -f "$CYCLE_ROOT/COLLECT_HOLD" ] || [ "$DISK_STOP" -eq 1 ] || [ -f "$CYCLE_ROOT/CAPACITY_STOP_REQUIRED" ]; then
    docker update --restart=no hibachi-collector-collector-1 >/dev/null 2>&1 || true
    if docker inspect hibachi-collector-collector-1 --format '{{.State.Running}}' 2>/dev/null | grep -qx true; then
      compose stop collector || true
      echo "COLLECTOR_STOPPED_CAPACITY_STOP"
    fi
    ACTION_REQUIRED="${ACTION_REQUIRED:-capacity_stop_collect_paused}"
  fi

  if [ "$URGENCY" = "COVER_STOP_REQUIRED" ] && [ "$succ_exists" = "0" ]; then
    echo "COVER_STOP_REQUIRED remaining=$remaining successor_missing"
    ACTION_REQUIRED="cover_stop_successor_missing"
    # Prefer deliberate collector stop over repeated partition-miss INSERT noise.
    if docker inspect hibachi-collector-collector-1 --format '{{.State.Running}}' 2>/dev/null | grep -qx true; then
      compose stop collector || true
      echo "COLLECTOR_STOPPED_FOR_MISSING_COVER"
    fi
    EXIT_CODE=7
  elif [ "$URGENCY" = "PROVISION_LATE" ]; then
    echo "PROVISION_LATE remaining=$remaining"
    ACTION_REQUIRED="provision_late"
    if [ "$EXIT_CODE" -eq 0 ]; then
      EXIT_CODE=8
    fi
  elif [ "$URGENCY" = "PROVISION_REQUIRED" ]; then
    echo "PROVISION_REQUIRED remaining=$remaining"
    ACTION_REQUIRED="provision_required"
  fi

  write_status
  if [ "$EXIT_CODE" -eq 0 ]; then
    echo STATUS=ok
  else
    echo "STATUS=degraded exit=$EXIT_CODE urgency=$URGENCY"
  fi
  exit "$EXIT_CODE"
} >>"$LOG" 2>&1
