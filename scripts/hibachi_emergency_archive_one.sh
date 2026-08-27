#!/usr/bin/env bash
# Archive one oldest CLOSED_UNARCHIVED generation to B2, mark DROP_ELIGIBLE,
# optionally physically DROP (emergency recovery only).
#
# Never archives ACTIVE. Never auto-starts collector. External-ref untouched.
# Normal operator floor remains 5 GiB. Emergency archive floor is 3 GiB and is
# only used when COLLECT_HOLD is present (this recovery). Ordinary auto-archive
# must pass --require-normal-floor (5 GiB).
#
# Physical DROP requires --drop. Automatic ticks must never pass that flag.
set -euo pipefail
source "$HOME/.cache/hibachi-partition-env.sh"

CYCLE_ROOT="$HOME/gen-cycle"
EV="${EVIDENCE_DIR:-$HOME/hibachi-emergency-capacity-evidence}"
ARCHIVE_WORK="$CYCLE_ROOT/archive-work"
# Current reviewed trading_bot tree; do not clobber the older proven overlay.
OVERLAY="$CYCLE_ROOT/overlay-emergency"
HELPER="$CYCLE_ROOT/hibachi_emergency_archive_window.py"
B2_ENV="${HIBACHI_B2_ENV:-$HOME/.config/trading-bot/b2.env}"
LOCK="$CYCLE_ROOT/archive.lock"
STATUS="$CYCLE_ROOT/archive.status.env"
LOG="${ARCHIVE_LOG:-$CYCLE_ROOT/emergency-archive.log}"
MAX_ROWS=100000
MAX_BUNDLE=$((64 * 1024 * 1024))
EMERGENCY_FLOOR=$((3 * 1024 * 1024 * 1024))
NORMAL_FLOOR=$((5 * 1024 * 1024 * 1024))
# Bounded queue between automatic archive and human DROP. Never auto-DROP.
MAX_DROP_ELIGIBLE=2
DO_DROP=0
REQUIRE_NORMAL_FLOOR=0
for arg in "$@"; do
  case "$arg" in
    --drop) DO_DROP=1 ;;
    --require-normal-floor) REQUIRE_NORMAL_FLOOR=1 ;;
  esac
done

mkdir -p "$EV/windows"
exec >>"$LOG" 2>&1

compose() {
  docker compose --env-file "$HIBACHI_RUNTIME_ENV" \
    -f "$HIBACHI_DEPLOY_DIR/compose.production.yaml" "$@"
}
pg_id() { compose ps -q postgres; }
sql() {
  local id db user
  id=$(pg_id)
  db=$(docker exec "$id" printenv POSTGRES_DB)
  user=$(docker exec "$id" printenv POSTGRES_USER)
  docker exec -i "$id" psql -v ON_ERROR_STOP=1 -At -U "$user" -d "$db" -c "$1"
}
free_bytes() { df -B1 --output=avail / | tail -n1 | tr -d ' '; }
image_ref() {
  local repo dig
  repo=$(grep '^IMAGE_REPOSITORY=' "$HIBACHI_RUNTIME_ENV" | cut -d= -f2-)
  dig=$(grep '^IMAGE_DIGEST=' "$HIBACHI_RUNTIME_ENV" | cut -d= -f2-)
  echo "${repo}@${dig}"
}

if [ -f "$LOCK" ]; then
  oldpid=$(awk '{print $1}' "$LOCK")
  if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then
    echo "==== $(date -u +%Y-%m-%dT%H:%M:%SZ) archive_already_running pid=$oldpid ===="
    exit 11
  fi
  echo "stale_lock_removed pid=${oldpid:-unknown}"
  rm -f "$LOCK"
fi
echo "$$ $(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$LOCK"
trap 'rm -f "$LOCK"' EXIT

FLOOR=$EMERGENCY_FLOOR
if [ "$REQUIRE_NORMAL_FLOOR" = 1 ] || [ ! -f "$CYCLE_ROOT/COLLECT_HOLD" ]; then
  FLOOR=$NORMAL_FLOOR
fi

FREE=$(free_bytes)
WORST=$((FLOOR + 2 * MAX_BUNDLE))
echo "==== ARCHIVE_ONE $(date -u +%Y-%m-%dT%H:%M:%SZ) free=$FREE floor=$FLOOR worst=$WORST drop=$DO_DROP ===="

if [ "$FREE" -le "$FLOOR" ] || [ "$FREE" -lt "$WORST" ]; then
  echo "EMERGENCY_ARCHIVE_CAPACITY_BLOCKED free=$FREE floor=$FLOOR worst_case=$WORST"
  cat >"$STATUS" <<EOF
updated_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
state=EMERGENCY_ARCHIVE_CAPACITY_BLOCKED
free_bytes=$FREE
floor_bytes=$FLOOR
worst_case_temp=$WORST
EOF
  exit 3
fi

[ -d "$OVERLAY/trading_bot" ] || { echo "ABORT overlay-emergency missing"; exit 12; }
[ -f "$HELPER" ] || { echo "ABORT window helper missing"; exit 12; }

pg_health=$(docker inspect hibachi-collector-postgres-1 --format '{{.State.Health.Status}}')
[ "$pg_health" = healthy ] || { echo "postgres_unhealthy"; exit 4; }

if docker ps --format '{{.Names}}' | grep -qi external; then
  echo "ABORT external running"
  exit 2
fi

# Oldest CLOSED first. ARCHIVING is resumable after an interrupted oneshot.
CAND=$(sql "SELECT generation_key || ' ' || partition_name || ' ' || id_start || ' ' || id_end || ' ' || state FROM market_event_generations WHERE state IN ('CLOSED_UNARCHIVED','ARCHIVING','ARCHIVE_FAILED','VERIFY_FAILED') ORDER BY id_start LIMIT 1")
if [ -z "$CAND" ]; then
  echo "no_archive_candidate"
  cat >"$STATUS" <<EOF
updated_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
state=no_archive_candidate
EOF
  exit 0
fi
set -- $CAND
KEY=$1
PART=$2
ID_START=$3
ID_END=$4
ST=$5
echo "candidate=$KEY $PART [$ID_START,$ID_END) state=$ST"

drop_n=$(sql "SELECT COUNT(*) FROM market_event_generations WHERE state='DROP_ELIGIBLE'")
echo "drop_eligible_count=$drop_n limit=$MAX_DROP_ELIGIBLE"
# Block starting or resuming another archive once the human-DROP queue is full.
# Completing this candidate would make DROP_ELIGIBLE = drop_n+1.
if [ "$drop_n" -ge "$MAX_DROP_ELIGIBLE" ]; then
  echo "DROP_BACKLOG_LIMIT drop_eligible=$drop_n limit=$MAX_DROP_ELIGIBLE candidate=$KEY state=$ST skipped_new_archive"
  cat >"$STATUS" <<EOF
updated_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
state=DROP_BACKLOG_LIMIT
drop_eligible_count=$drop_n
drop_eligible_limit=$MAX_DROP_ELIGIBLE
candidate_generation=$KEY
candidate_state=$ST
action=skip_new_archive
action_required=human_drop_approval
drop_requested=$DO_DROP
EOF
  exit 0
fi

PHYS=$(sql "SELECT COUNT(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_inherits i ON i.inhrelid=c.oid JOIN pg_class p ON p.oid=i.inhparent WHERE n.nspname='public' AND p.relname='market_events' AND c.relname='${PART}'")
[ "$PHYS" = "1" ] || { echo "ABORT physical child missing $PART"; exit 5; }

BOUND=$(sql "SELECT pg_get_expr(c.relpartbound, c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname='${PART}'")
echo "bound=$BOUND"
echo "$BOUND" | grep -q "$ID_START" || { echo "ABORT bound mismatch"; exit 6; }

if [ "$ST" = "CLOSED_UNARCHIVED" ] || [ "$ST" = "ARCHIVE_FAILED" ] || [ "$ST" = "VERIFY_FAILED" ]; then
  sql "UPDATE market_event_generations SET state='ARCHIVING', updated_at=now() WHERE generation_key='${KEY}' AND state='${ST}'"
fi

RMIN=$(sql "SELECT MIN(id) FROM market_events WHERE id >= ${ID_START} AND id < ${ID_END}")
RMAX=$(sql "SELECT MAX(id) FROM market_events WHERE id >= ${ID_START} AND id < ${ID_END}")
RCNT=$(sql "SELECT COUNT(*) FROM market_events WHERE id >= ${ID_START} AND id < ${ID_END}")
BYTES=$(sql "SELECT pg_total_relation_size('${PART}')")
echo "db_min=$RMIN db_max=$RMAX db_count=$RCNT bytes=$BYTES"

# g_9071913 incident: persisted 399749, allocated hole 9071913..9072163.
if [ "$KEY" = "g_9071913_9471913" ]; then
  [ "$RCNT" = "399749" ] || { echo "ABORT unexpected g_9071913 count $RCNT"; exit 7; }
  [ "$RMIN" = "9072164" ] || { echo "ABORT unexpected g_9071913 min $RMIN"; exit 7; }
  [ "$RMAX" = "9471912" ] || { echo "ABORT unexpected g_9071913 max $RMAX"; exit 7; }
else
  EXPECT_SPAN=$((ID_END - ID_START))
  SPAN=$((RMAX - RMIN + 1))
  [ "$RCNT" = "$SPAN" ] || { echo "ABORT non-contiguous persisted ids count=$RCNT span=$SPAN"; exit 7; }
  [ "$RCNT" = "$EXPECT_SPAN" ] || echo "WARN partial_fill count=$RCNT capacity=$EXPECT_SPAN"
fi

SYMBOL=$(sql "SELECT symbol FROM market_events WHERE id >= ${ID_START} AND id < ${ID_END} LIMIT 1")
net=$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}' "$(pg_id)" | head -n1)

docker run --rm -v "$ARCHIVE_WORK:/work" --user 0:0 --entrypoint bash "$(image_ref)" \
  -lc 'chown -R 10001:10001 /work && chmod 0700 /work'

# Root sidecar installs boto3 once, then runs the reviewed overlay + window helper.
# Host workdir stays UID 10001 / 0700.
tooling() {
  local inner=$1
  docker run --rm --network "$net" \
    -u 0:0 \
    -v "$OVERLAY:/overlay:ro" \
    -v "$HELPER:/opt/hibachi_emergency_archive_window.py:ro" \
    -v "$ARCHIVE_WORK:/work" \
    -v "$EV:/evidence" \
    -e PYTHONPATH=/overlay \
    --env-file "$HIBACHI_RUNTIME_ENV" \
    --env-file "$B2_ENV" \
    --entrypoint bash \
    "$(image_ref)" \
    -lc "set -euo pipefail; python -c 'import boto3' 2>/dev/null || pip install --no-cache-dir --root-user-action=ignore 'boto3>=1.35,<2' >/tmp/pip-boto3.log; ${inner}"
}

echo "sidecar_image=$(image_ref)"

mapfile -t HOURS < <(sql "SELECT to_char(h AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS+00:00') FROM (SELECT DISTINCT date_trunc('hour', received_at) AS h FROM market_events WHERE id >= ${ID_START} AND id < ${ID_END}) s ORDER BY h")
echo "archive_hour_buckets=${#HOURS[@]}"
COMPLETED_OK=0
WINDOW_FAIL=0
REUSED=0
PEAK_FREE=$FREE
MIN_FREE=$FREE
for hstart in "${HOURS[@]}"; do
  FREE=$(free_bytes)
  [ "$FREE" -lt "$MIN_FREE" ] && MIN_FREE=$FREE
  echo "filesystem_free_bytes=$FREE"
  if [ "$FREE" -le "$FLOOR" ] || [ "$FREE" -lt $((FLOOR + 2 * MAX_BUNDLE)) ]; then
    sql "UPDATE market_event_generations SET state='ARCHIVE_FAILED', updated_at=now() WHERE generation_key='${KEY}'"
    echo "EMERGENCY_ARCHIVE_CAPACITY_BLOCKED mid_generation free=$FREE"
    exit 3
  fi
  hend=$(python3 - <<PY
from datetime import datetime, timedelta
s=datetime.fromisoformat("${hstart}")
print((s+timedelta(hours=1)).isoformat())
PY
)
  safe=$(printf '%s' "$hstart" | tr -c 'A-Za-z0-9._-' '_')
  evjson="/evidence/windows/${KEY}/${safe}.json"
  echo "archive_window $hstart -> $hend max_rows=$MAX_ROWS floor=$FLOOR"
  inner=$(printf '%s' "python /opt/hibachi_emergency_archive_window.py --start '${hstart}' --end '${hend}' --symbol '${SYMBOL}' --work-dir /work --evidence-json '${evjson}' --max-rows ${MAX_ROWS} --min-disk-bytes ${FLOOR}")
  set +e
  tooling "$inner"
  rc=$?
  set -e
  echo "archive_window_rc=$rc"
  if [ "$rc" -ne 0 ]; then
    WINDOW_FAIL=$((WINDOW_FAIL + 1))
    echo "WARN window_failed $hstart"
  else
    COMPLETED_OK=$((COMPLETED_OK + 1))
    if grep -q '"mode": "reuse_completed"' "$EV/windows/${KEY}/${safe}.json" 2>/dev/null; then
      REUSED=$((REUSED + 1))
    fi
  fi
  # Reclaim local bundle/restore temp; keep per-window evidence JSON on the host.
  tooling "python -c \"import shutil; from pathlib import Path; root=Path('/work');
[shutil.rmtree(c, ignore_errors=True) for c in root.iterdir() if c.is_dir()]\""
  FREE=$(free_bytes)
  [ "$FREE" -lt "$MIN_FREE" ] && MIN_FREE=$FREE
  [ "$FREE" -gt "$PEAK_FREE" ] && PEAK_FREE=$FREE
  # B2 HeadObject 403 showed up after many consecutive windows; brief pause.
  sleep 5
done

echo "archive_windows_ok=$COMPLETED_OK fail=$WINDOW_FAIL reused=$REUSED need=${#HOURS[@]}"
if [ "$COMPLETED_OK" -ne "${#HOURS[@]}" ]; then
  sql "UPDATE market_event_generations SET state='ARCHIVE_FAILED', updated_at=now() WHERE generation_key='${KEY}'"
  echo "windows_incomplete"
  exit 8
fi

# Evidence flags come from per-window independent restore, not assumed True.
python3 - <<PY | tee "$EV/evidence_${KEY}.json"
import hashlib, json
from pathlib import Path
windows_dir = Path("$EV/windows/${KEY}")
window_docs = []
for path in sorted(windows_dir.glob("*.json")):
    doc = json.loads(path.read_text(encoding="utf-8"))
    window_docs.append(doc)
    if doc.get("status") != "verified":
        raise SystemExit(f"unverified window {path.name}")
    if not all(doc.get(k) for k in (
        "checksums_pass", "manifest_pass", "remote_completed",
        "download_verification_pass", "storage_reconciliation_pass",
    )):
        raise SystemExit(f"window gate failed {path.name}")
extra = {
    "partition_name": "${PART}",
    "id_start": int("${ID_START}"),
    "id_end": int("${ID_END}"),
    "windows": int("${COMPLETED_OK}"),
    "reused_completed_windows": int("${REUSED}"),
    "physical_bytes": int("${BYTES}"),
    "dataset_ids": [d["dataset_id"] for d in window_docs],
    "incident_allocated_not_persisted": (
        {"ids": "9071913..9072163", "count": 251}
        if "${KEY}" == "g_9071913_9471913" else None
    ),
}
payload = {
  "generation_key": "${KEY}",
  "min_raw_event_id": int("${RMIN}"),
  "max_raw_event_id": int("${RMAX}"),
  "expected_row_count": int("${RMAX}") - int("${RMIN}") + 1,
  "observed_row_count": int("${RCNT}"),
  "checksums_pass": True,
  "manifest_pass": True,
  "remote_completed": True,
  "download_verification_pass": True,
  "storage_reconciliation_pass": True,
  "id_coverage_contiguous": True,
  "extra": extra,
}
if payload["expected_row_count"] != payload["observed_row_count"]:
    raise SystemExit("evidence row mismatch")
digest = hashlib.sha256(
    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
payload["evidence_sha256"] = digest
print(json.dumps(payload, indent=2, sort_keys=True))
Path("$EV/evidence_${KEY}.sha256").write_text(digest + "\n")
PY
EVHASH=$(tr -d '\n' < "$EV/evidence_${KEY}.sha256")
echo "evidence_sha256=$EVHASH"

sql "UPDATE market_event_generations SET state='VERIFIED', verified_at=now(), updated_at=now(), archive_evidence_sha256='${EVHASH}' WHERE generation_key='${KEY}' AND state='ARCHIVING'"
sql "UPDATE market_event_generations SET state='DROP_ELIGIBLE', drop_eligible_at=now(), updated_at=now() WHERE generation_key='${KEY}' AND state='VERIFIED'"
echo "DROP_ELIGIBLE $KEY"

FREE_BEFORE_DROP=$(free_bytes)
cat >"$STATUS" <<EOF
updated_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
state=DROP_ELIGIBLE
generation_key=$KEY
windows=$COMPLETED_OK
reused_completed_windows=$REUSED
evidence_sha256=$EVHASH
free_bytes=$FREE_BEFORE_DROP
min_free_bytes=$MIN_FREE
drop_requested=$DO_DROP
EOF

if [ "$DO_DROP" != 1 ]; then
  echo "skip_physical_drop"
  exit 0
fi

echo "DROP_BEGIN $KEY free=$FREE_BEFORE_DROP"
RECLAIMED=$(sql "SELECT public.drop_verified_market_event_generation('${KEY}', 'DROP_VERIFIED_GENERATION', true)")
echo "drop_function_returned_bytes=$RECLAIMED"
sleep 2
FREE_AFTER=$(free_bytes)
DELTA=$((FREE_AFTER - FREE_BEFORE_DROP))
PRESENT=$(sql "SELECT CASE WHEN to_regclass('public.${PART}') IS NULL THEN 'absent' ELSE 'present' END")
STATE=$(sql "SELECT state FROM market_event_generations WHERE generation_key='${KEY}'")
echo "drop_result state=$STATE partition=$PRESENT free_before=$FREE_BEFORE_DROP free_after=$FREE_AFTER delta=$DELTA reported=$RECLAIMED"
[ "$STATE" = "DROPPED" ] || { echo "ABORT drop metadata not DROPPED"; exit 9; }
[ "$PRESENT" = "absent" ] || { echo "ABORT partition still present"; exit 9; }

{
  echo "generation=$KEY"
  echo "rows=$RCNT"
  echo "windows=$COMPLETED_OK"
  echo "reused_completed_windows=$REUSED"
  echo "evidence_sha256=$EVHASH"
  echo "physical_bytes_before=$BYTES"
  echo "drop_reported_bytes=$RECLAIMED"
  echo "free_before=$FREE_BEFORE_DROP"
  echo "free_after=$FREE_AFTER"
  echo "reclaimed_observed=$DELTA"
} | tee "$EV/drop_${KEY}.txt"

CLOSED_N=$(sql "SELECT COUNT(*) FROM market_event_generations WHERE state IN ('CLOSED_UNARCHIVED','ARCHIVING','ARCHIVE_FAILED','VERIFY_FAILED')")
DROP_N=$(sql "SELECT COUNT(*) FROM market_event_generations WHERE state='DROP_ELIGIBLE'")
echo "post_drop closed=$CLOSED_N drop_eligible=$DROP_N free=$FREE_AFTER"
echo ARCHIVE_DROP_OK
