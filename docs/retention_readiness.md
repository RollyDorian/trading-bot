# Data quality and retention readiness

`scripts/collect_quality.py` is a provider-neutral, host-local, read-only inspection of
the append-only `market_events` and `system_events` datasets. It opens no listener and
prints no payload, message, details, database URL, path, host data, or raw error.

```sh
python3 scripts/collect_quality.py
python3 scripts/collect_quality.py --format summary
```

The default one-hour window is configurable from 300 through 86400 seconds. Every query
runs in a read-only transaction with a five-second statement timeout and a 15-second
process timeout. Default analysis uses catalog estimates for total rows, primary-key
endpoints for first/latest inserted timestamps, and bounded recent-window anomaly queries.
`--full-history` explicitly opts into exact counts and min/max scans; it can be expensive,
may time out, and must run only after operator review during a low-risk period.

## Stable schema and signals

Schema version 1 reports both datasets, observation scope, thresholds, quality, storage,
forecast, and aggregate status. `unknown` fails closed. `critical` means empty/stale data,
ordering or malformed-record findings, or disk at the established 3 GiB free-space floor.
`warning` covers duplicates, a receipt gap above the existing 60-second quality threshold,
a recent rate ratio below 0.5 or above 2.0, or a capacity forecast at or below 7 days.
Rate comparison requires at least 30 events in both half-windows.

Duplicate detection is intentionally limited to the logical identity
`(source, event_type, symbol, exchange_at, sequence)` where both timestamp and sequence
exist. It is a review signal, not permission to remove either record. Ordering and largest
gap use `received_at` in append order within the selected recent window. Malformed signals
cover empty normalized identity fields, materially future timestamps, and negative latency.

Storage sizes come from PostgreSQL relation/catalog functions. Daily growth is conservatively
estimated as current database bytes divided by observed collection days. Forecasting requires
at least 24 hours of history, assumes linear growth, includes existing fixed overhead, and
does not predict volatility or index bloat. `unknown` is expected until the sample matures.

## Retention decision framework

**No automatic deletion** is implemented or authorized. The critical boundary remains the
deployment threshold of 3 GiB free. Warning begins when the conservative forecast reaches
7 days to that same boundary.

- **Manual archive/export followed by an approved retention action:** requires a verified
  backup, immutable export checksums, restore evidence, a reviewed cutoff, and explicit
  approval. Removal is irreversible without a validated backup and is disabled now.
- **Partition-aware retention:** requires a future reviewed schema/migration, partition
  integrity tests, backup evidence, and separate approval. The current schema is not
  partitioned, so this option is unavailable and has no automatic rollback.
- **Larger volume/capacity:** preserves append-only history but requires infrastructure,
  filesystem, backup, and rollback planning plus separate approval. Reverting a storage
  expansion may be impossible after growth, so it is not automated or enabled here.
- **Offline analytical replica/export:** preserves production capacity and research access
  only after integrity and restore checks plus separate approval. The copy has its own access
  and retention risks, cannot restore production unless independently validated, and is not
  enabled now.

At forecast warning, verify the trend on multiple daily observations, validate a fresh
backup, and choose an approved capacity/archival plan. At critical, stop optional exports
and require immediate human capacity review; do not delete data. For stale data, gaps,
duplicates, ordering, or malformed findings, preserve evidence, inspect bounded redacted
logs, and stop research admission for the affected interval.

## Explicit bounded sample export

For an approved local investigation, choose UTC `start`, exclusive UTC `end`, and a positive
`maximum row count`. Use a read-only transaction and export only:
`received_at, exchange_at, source, event_type, symbol, sequence, latency_ms`. Exclude
`id`, payload, system-event message/details, and all operational configuration.

Set `umask 077`, write outside Git to an operator-owned directory, and verify mode `0600`.
The following template requires explicit values and uses psql literal quoting for timestamps:

```sh
umask 077
START_UTC='<ISO8601 UTC start>'
END_UTC='<ISO8601 UTC exclusive end>'
MAX_ROWS='<positive integer>'
OUTPUT='<absolute path outside Git>/market-sample.csv'
case "$MAX_ROWS" in ''|*[!0-9]*|0) exit 2 ;; esac
[ "$MAX_ROWS" -le 10000 ] || exit 2
[ ! -e "$OUTPUT" ] || exit 2
TEMP_OUTPUT="${OUTPUT}.partial.$$"
trap 'rm -f -- "$TEMP_OUTPUT"' EXIT HUP INT TERM
docker compose --env-file "$HIBACHI_RUNTIME_ENV" \
  -f "$HIBACHI_DEPLOY_DIR/compose.production.yaml" \
  exec -T postgres sh -c \
  'exec psql -qXAt -v ON_ERROR_STOP=1 --username="$POSTGRES_USER" \
  --dbname="$POSTGRES_DB" "$@"' psql \
  --set=start_utc="$START_UTC" --set=end_utc="$END_UTC" --set=max_rows="$MAX_ROWS" \
  >"$TEMP_OUTPUT" <<'SQL'
BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';
COPY (
  SELECT received_at, exchange_at, source, event_type, symbol, sequence, latency_ms
  FROM market_events
  WHERE received_at >= :'start_utc'::timestamptz
    AND received_at < :'end_utc'::timestamptz
  ORDER BY received_at
  LIMIT :max_rows
) TO STDOUT WITH CSV HEADER;
COMMIT;
SQL
chmod 600 "$TEMP_OUTPUT"
mv "$TEMP_OUTPUT" "$OUTPUT"
trap - EXIT HUP INT TERM
```

Confirm `START_UTC < END_UTC`; the hard maximum is 10000 rows. Inspect only the row count
and file metadata, and delete no database row. The resulting file is never scheduled
automatically and must not be copied to a public location.
