import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "collect_quality.py"
POLICY = ROOT / "docs" / "retention_readiness.md"


def load_quality() -> ModuleType:
    spec = importlib.util.spec_from_file_location("collect_quality", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def quality() -> ModuleType:
    return load_quality()


def healthy_raw(**changes: object) -> dict[str, object]:
    now = datetime(2026, 7, 23, 12, tzinfo=UTC)
    values: dict[str, object] = {
        "observed_at_utc": now.isoformat(),
        "market_total_records": 100_000,
        "system_total_records": 10,
        "market_earliest_utc": (now - timedelta(days=10)).isoformat(),
        "market_latest_utc": (now - timedelta(seconds=30)).isoformat(),
        "system_earliest_utc": (now - timedelta(days=10)).isoformat(),
        "system_latest_utc": (now - timedelta(hours=1)).isoformat(),
        "market_recent_records": 3600,
        "system_recent_records": 0,
        "current_half_count": 1800,
        "previous_half_count": 1800,
        "ordering_anomalies": 0,
        "duplicate_records": 0,
        "largest_gap_seconds": 10,
        "malformed_records": 0,
        "database_bytes": 1024**3,
        "market_table_total_bytes": 900 * 1024**2,
        "market_index_bytes": 200 * 1024**2,
        "system_table_total_bytes": 1024**2,
        "collection_span_seconds": 10 * 86400,
    }
    values.update(changes)
    return values


def evaluate(quality: ModuleType, raw: dict[str, object], disk: int = 20 * 1024**3) -> dict:
    return quality.evaluate(raw, window_seconds=3600, disk_free_bytes=disk, full_history=False)


def test_healthy_recent_stream_and_stable_schema(quality: ModuleType) -> None:
    report = evaluate(quality, healthy_raw())
    assert report["schema_version"] == 1
    assert report["status"] == "pass"
    assert report["quality"]["freshness"] == "fresh"
    assert report["quality"]["rate_state"] == "normal"
    assert report["datasets"]["market_events"]["event_rate_per_second"] == 1.0
    assert len(json.dumps(report, separators=(",", ":"))) < 2048


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"market_recent_records": 0}, "critical"),
        ({"market_latest_utc": "2026-07-23T11:00:00+00:00"}, "critical"),
        ({"ordering_anomalies": 1}, "critical"),
        ({"malformed_records": 1}, "critical"),
        ({"duplicate_records": 1}, "warning"),
        ({"largest_gap_seconds": 61}, "warning"),
    ],
)
def test_empty_stale_duplicate_ordering_gap_and_malformed(
    quality: ModuleType, changes: dict[str, object], expected: str
) -> None:
    assert evaluate(quality, healthy_raw(**changes))["status"] == expected


def test_initial_empty_state_is_not_success(quality: ModuleType) -> None:
    report = evaluate(
        quality,
        healthy_raw(
            market_total_records=0,
            market_recent_records=0,
            market_earliest_utc=None,
            market_latest_utc=None,
            collection_span_seconds=None,
        ),
    )
    assert report["status"] == "unknown"
    assert report["quality"]["freshness"] == "unknown"


@pytest.mark.parametrize(
    ("current", "previous", "state"),
    [(30, 60, "normal"), (29, 60, "unknown"), (30, 61, "low"), (61, 30, "high")],
)
def test_rate_shift_boundaries(
    quality: ModuleType, current: int, previous: int, state: str
) -> None:
    report = evaluate(
        quality,
        healthy_raw(current_half_count=current, previous_half_count=previous),
    )
    assert report["quality"]["rate_state"] == state


def test_gap_boundary_is_not_warning(quality: ModuleType) -> None:
    report = evaluate(quality, healthy_raw(largest_gap_seconds=60))
    assert report["quality"]["gap_state"] == "ok"


def test_missing_timestamp_and_metadata_are_unknown(quality: ModuleType) -> None:
    report = evaluate(
        quality,
        healthy_raw(market_latest_utc=None, database_bytes="malformed"),
    )
    assert report["status"] == "unknown"
    assert report["quality"]["freshness"] == "unknown"


def test_insufficient_forecast_sample(quality: ModuleType) -> None:
    report = evaluate(quality, healthy_raw(collection_span_seconds=86399))
    assert report["forecast"]["estimated_daily_growth_bytes"] is None
    assert report["forecast"]["capacity_state"] == "unknown"


def test_warning_and_critical_capacity_forecast(quality: ModuleType) -> None:
    warning = evaluate(quality, healthy_raw(database_bytes=10 * 1024**3), 9 * 1024**3)
    critical = evaluate(quality, healthy_raw(), 3 * 1024**3)
    assert warning["forecast"]["capacity_state"] == "warning"
    assert critical["forecast"]["capacity_state"] == "critical"
    assert critical["status"] == "critical"


@pytest.mark.parametrize("failure", ("unavailable", "timeout"))
def test_database_timeout_or_failure_is_redacted(
    quality: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    sentinel = "postgresql://user:password@private.invalid/research"

    class FailingProbe:
        def __init__(self) -> None:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(sentinel, 5)
            raise ConnectionError(sentinel)

    monkeypatch.setattr(quality, "QualityProbe", FailingProbe)
    assert quality.run([]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert sentinel not in captured.out
    assert "password" not in captured.out
    assert json.loads(captured.out)["status"] == "unknown"


def test_default_sql_is_bounded_read_only_and_expensive_is_opt_in(
    quality: ModuleType,
) -> None:
    bounded = quality.build_sql(3600, full_history=False)
    expensive = quality.build_sql(3600, full_history=True)
    assert "BEGIN READ ONLY" in bounded
    assert "SET LOCAL statement_timeout = '5s'" in bounded
    assert "r.received_at > b.observed_at + interval '5 minutes'" in bounded
    assert "m.received_at <= b.observed_at" not in bounded
    assert "reltuples" in bounded
    assert "count(*) FROM market_events" not in bounded
    assert "count(*) FROM market_events" in expensive
    for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE ", "ALTER ", "VACUUM "):
        assert forbidden not in bounded.upper()
        assert forbidden not in expensive.upper()


def test_summary_is_short_and_contains_no_sensitive_fields(quality: ModuleType) -> None:
    text = quality.summary(evaluate(quality, healthy_raw()))
    assert len(text) < 200
    for forbidden in ("postgresql://", "payload", "path", "container", "host"):
        assert forbidden not in text


def test_invalid_arguments_return_bounded_unknown(
    quality: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert quality.run(["--window-seconds", "299"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    report = json.loads(captured.out)
    assert report["status"] == "unknown"
    assert report["window_seconds"] == quality.DEFAULT_WINDOW_SECONDS
    assert len(captured.out) < 256


def test_policy_documents_retention_and_sample_export_safety() -> None:
    policy = " ".join(POLICY.read_text(encoding="utf-8").lower().split())
    for phrase in (
        "no automatic deletion",
        "3 gib",
        "7 days",
        "manual archive/export",
        "partition-aware",
        "larger volume",
        "analytical replica",
        "mode `0600`",
        "maximum row count",
        "hard maximum is 10000 rows",
        "must not be copied to a public location",
    ):
        assert phrase in policy
