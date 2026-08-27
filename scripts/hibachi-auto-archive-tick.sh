#!/usr/bin/env bash
# Automatic CLOSED-generation archive executor (no physical DROP).
# Marker-only ARCHIVE_REQUIRED is not sufficient; this tick runs the bounded
# B2 archive oneshot for the oldest CLOSED generation when the normal 5 GiB
# archive policy allows it. Concurrent runs are lock-serialized.
set -euo pipefail
CYCLE_ROOT="$HOME/gen-cycle"
LOG="$CYCLE_ROOT/auto-archive.log"
exec >>"$LOG" 2>&1
echo "==== AUTO ARCHIVE TICK $(date -u +%Y-%m-%dT%H:%M:%SZ) ===="
if [ -f "$CYCLE_ROOT/COLLECT_HOLD" ]; then
  echo "collect_hold_present skip_auto_archive emergency_oneshot_owns_loop"
  exit 0
fi
set +e
"$CYCLE_ROOT/hibachi_emergency_archive_one.sh" --require-normal-floor
rc=$?
set -e
echo "archive_one_rc=$rc"
if [ -f "$CYCLE_ROOT/archive.status.env" ]; then
  echo "archive_status:"
  cat "$CYCLE_ROOT/archive.status.env"
fi
