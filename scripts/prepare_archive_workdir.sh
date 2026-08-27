#!/usr/bin/env bash
# Prepare least-privilege archive workdir for UID/GID 10001 (container app user).
# Usage: sudo bash scripts/prepare_archive_workdir.sh /var/lib/hibachi-archive/work
set -euo pipefail
TARGET="${1:?usage: prepare_archive_workdir.sh /path/to/work}"
install -d -o 10001 -g 10001 -m 0700 "$TARGET"
# Refuse to leave a world-writable path behind.
chmod 0700 "$TARGET"
OWNER="$(stat -c '%u:%g' "$TARGET")"
MODE="$(stat -c '%a' "$TARGET")"
if [ "$OWNER" != "10001:10001" ] || [ "$MODE" != "700" ]; then
  echo "BLOCKER=archive_workdir_contract owner=$OWNER mode=$MODE" >&2
  exit 2
fi
echo "ARCHIVE_WORKDIR_READY=$TARGET owner=$OWNER mode=$MODE"
