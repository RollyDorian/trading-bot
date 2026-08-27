#!/usr/bin/env bash
# Sequential emergency archive→DROP until normal READY free disk.
# Collector remains paused (COLLECT_HOLD). PostgreSQL stays up. No external-ref.
set -euo pipefail
source "$HOME/.cache/hibachi-partition-env.sh"
CYCLE_ROOT="$HOME/gen-cycle"
EV="$HOME/hibachi-emergency-capacity-evidence"
READY=$((5 * 1024 * 1024 * 1024 + 203546624 + 128 * 1024 * 1024))
LOG="$EV/recovery_loop.log"
mkdir -p "$EV"
{
  echo "==== LOOP START $(date -u +%Y-%m-%dT%H:%M:%SZ) ready_target=$READY ===="
  while true; do
    FREE=$(df -B1 --output=avail / | tail -n1 | tr -d ' ')
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) free=$FREE ready=$READY"
    if [ "$FREE" -ge "$READY" ]; then
      echo "NORMAL_READY_TARGET_REACHED free=$FREE"
      break
    fi
    set +e
    bash "$CYCLE_ROOT/hibachi_emergency_archive_one.sh" --drop
    rc=$?
    set -e
    echo "archive_one_rc=$rc"
    if [ "$rc" -ne 0 ]; then
      echo "LOOP_STOP rc=$rc"
      exit "$rc"
    fi
    running=$(docker inspect hibachi-collector-collector-1 --format '{{.State.Running}}')
    echo "collector_running=$running postgres=$(docker inspect hibachi-collector-postgres-1 --format '{{.State.Health.Status}}')"
    [ "$running" = false ] || { echo "ABORT collector restarted"; exit 10; }
  done
  echo "==== LOOP DONE $(date -u +%Y-%m-%dT%H:%M:%SZ) ===="
  df -B1 /
} | tee -a "$LOG"
