#!/usr/bin/env python3
"""Bounded read-only quality and capacity analysis for COLLECT-only storage."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Never

SCHEMA_VERSION: Final = 1
DEFAULT_WINDOW_SECONDS: Final = 3600
MIN_WINDOW_SECONDS: Final = 300
MAX_WINDOW_SECONDS: Final = 86400
FRESHNESS_SECONDS: Final = 120
GAP_WARNING_SECONDS: Final = 60
MIN_RATE_SAMPLE_EVENTS: Final = 30
LOW_RATE_RATIO: Final = 0.5
HIGH_RATE_RATIO: Final = 2.0
MIN_FORECAST_SAMPLE_SECONDS: Final = 86400
FORECAST_WARNING_DAYS: Final = 7
MIN_DISK_BYTES: Final = 3 * 1024**3
QUERY_TIMEOUT_SECONDS: Final = 5
PROCESS_TIMEOUT_SECONDS: Final = 15
UNKNOWN: Final = "unknown"


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        raise ValueError


def build_sql(window_seconds: int, *, full_history: bool) -> str:
    total_market = (
        "(SELECT count(*) FROM market_events)"
        if full_history
        else (
            "(SELECT greatest(reltuples, 0)::bigint FROM pg_class "
            "WHERE oid = 'market_events'::regclass)"
        )
    )
    total_system = (
        "(SELECT count(*) FROM system_events)"
        if full_history
        else (
            "(SELECT greatest(reltuples, 0)::bigint FROM pg_class "
            "WHERE oid = 'system_events'::regclass)"
        )
    )
    earliest_market = (
        "(SELECT min(received_at) FROM market_events)"
        if full_history
        else "(SELECT received_at FROM market_events ORDER BY id ASC LIMIT 1)"
    )
    latest_market = (
        "(SELECT max(received_at) FROM market_events)"
        if full_history
        else "(SELECT received_at FROM market_events ORDER BY id DESC LIMIT 1)"
    )
    earliest_system = (
        "(SELECT min(occurred_at) FROM system_events)"
        if full_history
        else "(SELECT occurred_at FROM system_events ORDER BY id ASC LIMIT 1)"
    )
    latest_system = (
        "(SELECT max(occurred_at) FROM system_events)"
        if full_history
        else "(SELECT occurred_at FROM system_events ORDER BY id DESC LIMIT 1)"
    )
    return f"""
BEGIN READ ONLY;
SET LOCAL statement_timeout = '{QUERY_TIMEOUT_SECONDS}s';
WITH bounds AS (
    SELECT
        clock_timestamp() AS observed_at,
        clock_timestamp() - interval '{window_seconds} seconds' AS window_start,
        clock_timestamp() - interval '{window_seconds // 2} seconds' AS midpoint
),
recent AS (
    SELECT
        m.id, m.received_at, m.exchange_at, m.source, m.event_type,
        m.symbol, m.sequence, m.latency_ms
    FROM market_events AS m, bounds AS b
    WHERE m.received_at >= b.window_start
),
ordered AS (
    SELECT received_at, lag(received_at) OVER (ORDER BY id) AS previous_received_at
    FROM recent
),
continuity AS (
    SELECT
        count(*) FILTER (WHERE received_at < previous_received_at) AS ordering_anomalies,
        coalesce(max(extract(epoch FROM received_at - previous_received_at))
            FILTER (WHERE received_at >= previous_received_at), 0) AS largest_gap_seconds
    FROM ordered
),
duplicate_groups AS (
    SELECT count(*) - 1 AS extra
    FROM recent
    WHERE exchange_at IS NOT NULL AND sequence IS NOT NULL
    GROUP BY source, event_type, symbol, exchange_at, sequence
    HAVING count(*) > 1
),
rates AS (
    SELECT
        count(*) FILTER (WHERE r.received_at <= b.observed_at) AS recent_count,
        count(*) FILTER (
            WHERE r.received_at >= b.midpoint AND r.received_at <= b.observed_at
        ) AS current_half_count,
        count(*) FILTER (
            WHERE r.received_at < b.midpoint AND r.received_at >= b.window_start
        ) AS previous_half_count,
        count(*) FILTER (
            WHERE r.received_at > b.observed_at + interval '5 minutes'
               OR r.source = '' OR r.event_type = '' OR r.symbol = ''
               OR (r.exchange_at IS NOT NULL
                   AND r.exchange_at > b.observed_at + interval '5 minutes')
               OR (r.latency_ms IS NOT NULL AND r.latency_ms < 0)
        ) AS malformed_count
    FROM recent AS r CROSS JOIN bounds AS b
),
storage AS (
    SELECT
        pg_database_size(current_database()) AS database_bytes,
        pg_total_relation_size('market_events') AS market_table_total_bytes,
        pg_indexes_size('market_events') AS market_index_bytes,
        pg_total_relation_size('system_events') AS system_table_total_bytes
)
SELECT json_build_object(
    'observed_at_utc', (SELECT observed_at FROM bounds),
    'market_total_records', {total_market},
    'system_total_records', {total_system},
    'market_earliest_utc', {earliest_market},
    'market_latest_utc', {latest_market},
    'system_earliest_utc', {earliest_system},
    'system_latest_utc', {latest_system},
    'market_recent_records', rates.recent_count,
    'system_recent_records', (
        SELECT count(*) FROM system_events, bounds
        WHERE occurred_at >= window_start AND occurred_at <= observed_at
    ),
    'current_half_count', rates.current_half_count,
    'previous_half_count', rates.previous_half_count,
    'ordering_anomalies', continuity.ordering_anomalies,
    'duplicate_records', coalesce((SELECT sum(extra) FROM duplicate_groups), 0),
    'largest_gap_seconds', continuity.largest_gap_seconds,
    'malformed_records', rates.malformed_count,
    'database_bytes', storage.database_bytes,
    'market_table_total_bytes', storage.market_table_total_bytes,
    'market_index_bytes', storage.market_index_bytes,
    'system_table_total_bytes', storage.system_table_total_bytes,
    'collection_span_seconds', extract(epoch FROM ({latest_market} - {earliest_market}))
)
FROM rates CROSS JOIN continuity CROSS JOIN storage;
COMMIT;
""".strip()


def evaluate(
    raw: dict[str, Any], *, window_seconds: int, disk_free_bytes: int, full_history: bool
) -> dict[str, Any]:
    observed = _text_or_none(raw, "observed_at_utc")
    market_latest = _text_or_none(raw, "market_latest_utc")
    market_earliest = _text_or_none(raw, "market_earliest_utc")
    system_latest = _text_or_none(raw, "system_latest_utc")
    system_earliest = _text_or_none(raw, "system_earliest_utc")
    numeric_keys = (
        "market_total_records",
        "system_total_records",
        "market_recent_records",
        "system_recent_records",
        "current_half_count",
        "previous_half_count",
        "ordering_anomalies",
        "duplicate_records",
        "largest_gap_seconds",
        "malformed_records",
        "database_bytes",
        "market_table_total_bytes",
        "market_index_bytes",
        "system_table_total_bytes",
        "collection_span_seconds",
    )
    values = {key: _number_or_none(raw, key) for key in numeric_keys}
    recent = values["market_recent_records"]
    freshness_age = _timestamp_age_seconds(observed, market_latest)
    freshness = (
        UNKNOWN
        if freshness_age is None
        else ("fresh" if 0 <= freshness_age <= FRESHNESS_SECONDS else "stale")
    )
    rate = None if recent is None else recent / window_seconds
    rate_state = _rate_state(values["current_half_count"], values["previous_half_count"])
    gap = values["largest_gap_seconds"]
    gap_state = UNKNOWN if gap is None else ("ok" if gap <= GAP_WARNING_SECONDS else "warning")
    database_bytes = values["database_bytes"]
    span = values["collection_span_seconds"]
    daily_growth = None
    if database_bytes is not None and span is not None and span >= MIN_FORECAST_SAMPLE_SECONDS:
        daily_growth = database_bytes / (span / 86400)
    days_to_threshold = None
    if daily_growth is not None and daily_growth > 0:
        days_to_threshold = max(disk_free_bytes - MIN_DISK_BYTES, 0) / daily_growth
    if disk_free_bytes <= MIN_DISK_BYTES:
        capacity_state = "critical"
    elif days_to_threshold is None:
        capacity_state = UNKNOWN
    elif days_to_threshold <= FORECAST_WARNING_DAYS:
        capacity_state = "warning"
    else:
        capacity_state = "ok"
    critical = (
        recent == 0
        or freshness == "stale"
        or _positive(values["ordering_anomalies"])
        or _positive(values["malformed_records"])
        or capacity_state == "critical"
    )
    warning = (
        _positive(values["duplicate_records"])
        or gap_state == "warning"
        or rate_state in {"low", "high"}
        or capacity_state in {"warning", UNKNOWN}
    )
    required_known = (
        observed,
        market_latest,
        market_earliest,
        recent,
        rate,
        gap,
        database_bytes,
        values["market_table_total_bytes"],
    )
    if any(value is None for value in required_known):
        status = UNKNOWN
    elif critical:
        status = "critical"
    elif warning:
        status = "warning"
    else:
        status = "pass"
    return {
        "schema_version": SCHEMA_VERSION,
        "analysis_scope": "explicit_full_history" if full_history else "bounded_recent",
        "status": status,
        "window_seconds": window_seconds,
        "thresholds": {
            "freshness_seconds": FRESHNESS_SECONDS,
            "gap_warning_seconds": GAP_WARNING_SECONDS,
            "disk_critical_free_bytes": MIN_DISK_BYTES,
            "forecast_warning_days": FORECAST_WARNING_DAYS,
            "minimum_forecast_sample_seconds": MIN_FORECAST_SAMPLE_SECONDS,
        },
        "datasets": {
            "market_events": {
                "total_records": values["market_total_records"],
                "total_kind": "exact" if full_history else "catalog_estimate",
                "earliest_utc": market_earliest,
                "latest_utc": market_latest,
                "recent_records": recent,
                "event_rate_per_second": _rounded(rate),
            },
            "system_events": {
                "total_records": values["system_total_records"],
                "total_kind": "exact" if full_history else "catalog_estimate",
                "earliest_utc": system_earliest,
                "latest_utc": system_latest,
                "recent_records": values["system_recent_records"],
            },
        },
        "quality": {
            "freshness": freshness,
            "freshness_age_seconds": _rounded(freshness_age),
            "rate_state": rate_state,
            "largest_gap_seconds": _rounded(gap),
            "gap_state": gap_state,
            "duplicate_records": values["duplicate_records"],
            "ordering_anomalies": values["ordering_anomalies"],
            "malformed_records": values["malformed_records"],
        },
        "storage": {
            "database_bytes": database_bytes,
            "market_table_total_bytes": values["market_table_total_bytes"],
            "market_index_bytes": values["market_index_bytes"],
            "system_table_total_bytes": values["system_table_total_bytes"],
            "disk_free_bytes": disk_free_bytes,
        },
        "forecast": {
            "sample_seconds": span,
            "estimated_daily_growth_bytes": _rounded(daily_growth),
            "days_to_disk_threshold": _rounded(days_to_threshold),
            "capacity_state": capacity_state,
            "method": "linear_database_size_over_observed_span",
        },
    }


def unknown_report(*, window_seconds: int, full_history: bool) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "analysis_scope": "explicit_full_history" if full_history else "bounded_recent",
        "status": UNKNOWN,
        "window_seconds": window_seconds,
        "error": "quality inspection unavailable",
    }


class QualityProbe:
    def __init__(self) -> None:
        self.deploy_dir = _required_path("HIBACHI_DEPLOY_DIR", directory=True)
        runtime_env = _required_path("HIBACHI_RUNTIME_ENV", directory=False)
        runtime_stat = runtime_env.stat()
        if stat.S_IMODE(runtime_stat.st_mode) != 0o600 or runtime_stat.st_uid != os.getuid():
            raise ValueError("invalid inspection configuration")
        self.compose = (
            "docker",
            "compose",
            "--env-file",
            str(runtime_env),
            "-f",
            str(self.deploy_dir / "compose.production.yaml"),
        )

    def inspect(self, *, window_seconds: int, full_history: bool) -> dict[str, Any]:
        result = subprocess.run(
            (
                *self.compose,
                "exec",
                "-T",
                "postgres",
                "sh",
                "-c",
                'exec psql -qXAt -v ON_ERROR_STOP=1 --username="$POSTGRES_USER" '
                '--dbname="$POSTGRES_DB"',
            ),
            cwd=self.deploy_dir,
            input=build_sql(window_seconds, full_history=full_history),
            check=True,
            capture_output=True,
            text=True,
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
        lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
        if len(lines) != 1:
            raise ValueError("invalid inspection result")
        value = json.loads(lines[0])
        if not isinstance(value, dict):
            raise ValueError("invalid inspection result")
        return evaluate(
            value,
            window_seconds=window_seconds,
            disk_free_bytes=shutil.disk_usage(self.deploy_dir).free,
            full_history=full_history,
        )


def _required_path(name: str, *, directory: bool) -> Path:
    value = os.environ.get(name)
    if not value:
        raise ValueError("missing inspection configuration")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("invalid inspection configuration")
    if directory and not path.is_dir():
        raise ValueError("invalid inspection configuration")
    if not directory and not path.is_file():
        raise ValueError("invalid inspection configuration")
    return path


def _number_or_none(raw: dict[str, Any], key: str) -> float | None:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _text_or_none(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) and value else None


def _timestamp_age_seconds(observed: str | None, latest: str | None) -> float | None:
    if observed is None or latest is None:
        return None
    try:
        return (datetime.fromisoformat(observed) - datetime.fromisoformat(latest)).total_seconds()
    except (TypeError, ValueError):
        return None


def _rate_state(current: float | None, previous: float | None) -> str:
    if (
        current is None
        or previous is None
        or current < MIN_RATE_SAMPLE_EVENTS
        or previous < MIN_RATE_SAMPLE_EVENTS
    ):
        return UNKNOWN
    ratio = current / previous
    if ratio < LOW_RATE_RATIO:
        return "low"
    if ratio > HIGH_RATE_RATIO:
        return "high"
    return "normal"


def _positive(value: float | None) -> bool:
    return value is not None and value > 0


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def summary(report: dict[str, Any]) -> str:
    if report.get("status") == UNKNOWN or "datasets" not in report:
        return "quality=unknown"
    market = report["datasets"]["market_events"]
    quality = report["quality"]
    forecast = report["forecast"]
    return (
        f"quality={report['status']} freshness={quality['freshness']} "
        f"rate={market['event_rate_per_second']} gap={quality['largest_gap_seconds']} "
        f"duplicates={quality['duplicate_records']} ordering={quality['ordering_anomalies']} "
        f"capacity={forecast['capacity_state']}"
    )


def run(argv: Sequence[str] | None = None) -> int:
    window_seconds = DEFAULT_WINDOW_SECONDS
    full_history = False
    output_format = "json"
    try:
        parser = SafeArgumentParser(add_help=False, exit_on_error=False)
        parser.add_argument("--window-seconds", type=int, default=DEFAULT_WINDOW_SECONDS)
        parser.add_argument("--full-history", action="store_true")
        parser.add_argument("--format", choices=("json", "summary"), default="json")
        args = parser.parse_args(argv)
        full_history = args.full_history
        output_format = args.format
        if not MIN_WINDOW_SECONDS <= args.window_seconds <= MAX_WINDOW_SECONDS:
            raise ValueError
        window_seconds = args.window_seconds
        report = QualityProbe().inspect(
            window_seconds=window_seconds,
            full_history=full_history,
        )
    except BaseException:
        report = unknown_report(window_seconds=window_seconds, full_history=full_history)
    if output_format == "summary":
        sys.stdout.write(summary(report) + "\n")
    else:
        sys.stdout.write(json.dumps(report, separators=(",", ":"), sort_keys=True) + "\n")
    return 0 if report.get("status") in {"pass", "warning"} else 1


if __name__ == "__main__":
    raise SystemExit(run())
