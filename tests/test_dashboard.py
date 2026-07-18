import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from trading_bot.dashboard.app import collector_status, create_app

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


class FakeStore:
    def __init__(self, status: dict[str, Any] | None = None) -> None:
        self._status = status or {
            "state": "healthy",
            "event_count": 42,
            "last_event_timestamp": "2026-07-18T12:00:00+00:00",
            "last_event_age_seconds": 0,
            "events_per_minute": 20,
            "coverage_start_timestamp": "2026-07-18T10:00:00+00:00",
            "coverage_end_timestamp": "2026-07-18T12:00:00+00:00",
            "average_latency_ms_1m": 12.5,
            "healthy": True,
        }

    async def status(self) -> dict[str, Any]:
        return self._status

    async def recent(self, limit: int) -> list[dict[str, Any]]:
        return [{"id": index} for index in range(limit)]

    async def system_events(self, limit: int) -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "occurred_at": "2026-07-18T11:59:00+00:00",
                "severity": "WARNING",
                "event_type": "DEGRADED",
                "component": "collector_supervisor",
                "message": "Reconnect scheduled",
                "details": {},
            }
        ][:limit]

    async def close(self) -> None:
        return None


def _dataset(
    root: Path,
    *,
    row_count: int = 10,
    evaluated: bool = True,
) -> None:
    directory = root / "v1_20260718"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "version": "v1_20260718",
                "row_count": row_count,
                "start_utc": "2026-07-18T00:00:00+00:00",
                "end_utc": "2026-07-18T08:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    if evaluated:
        (directory / "eval_momentum.json").write_text(
            json.dumps({"signal_count": 2, "win_rate": 0.5}),
            encoding="utf-8",
        )


def test_healthy_status_includes_operational_metrics() -> None:
    status = collector_status(
        event_count=100,
        last_event=NOW - timedelta(seconds=5),
        coverage_start=NOW - timedelta(hours=2),
        events_last_minute=25,
        average_latency_ms_1m=8.5,
        now=NOW,
    )
    assert status["state"] == "healthy"
    assert status["healthy"] is True
    assert status["last_event_age_seconds"] == 5
    assert status["events_per_minute"] == 25


def test_stale_status_drives_stale_alert() -> None:
    status = collector_status(
        event_count=100,
        last_event=NOW - timedelta(minutes=13, seconds=50),
        coverage_start=NOW - timedelta(hours=2),
        events_last_minute=0,
        average_latency_ms_1m=None,
        now=NOW,
    )
    assert status["state"] == "stale"
    assert status["healthy"] is False
    with TestClient(create_app(store=FakeStore(status))) as client:
        html = client.get("/").text
    assert "STALE DATA" in html
    assert "criticalAlert" in html


def test_dashboard_read_only_endpoints(tmp_path: Path) -> None:
    _dataset(tmp_path)
    with TestClient(create_app(datasets_dir=tmp_path, store=FakeStore())) as client:
        assert client.get("/api/status").json()["event_count"] == 42
        datasets = client.get("/api/datasets").json()
        assert datasets[0]["manifest"]["version"] == "v1_20260718"
        assert client.get("/api/datasets/v1_20260718/eval").json()["signal_count"] == 2
        assert len(client.get("/api/market/recent?limit=3").json()) == 3
        assert client.get("/api/system/recent").json()[0]["event_type"] == "DEGRADED"
        assert client.post("/api/status").status_code == 405


def test_no_datasets_and_empty_dataset_states(tmp_path: Path) -> None:
    with TestClient(create_app(datasets_dir=tmp_path, store=FakeStore())) as client:
        assert client.get("/api/datasets").json() == []
    _dataset(tmp_path, row_count=0, evaluated=False)
    with TestClient(create_app(datasets_dir=tmp_path, store=FakeStore())) as client:
        dataset = client.get("/api/datasets").json()[0]
        assert dataset["manifest"]["row_count"] == 0
        assert dataset["evaluation"] is None


def test_unavailable_evaluation_returns_404(tmp_path: Path) -> None:
    _dataset(tmp_path, evaluated=False)
    with TestClient(create_app(datasets_dir=tmp_path, store=FakeStore())) as client:
        assert client.get("/api/datasets/v1_20260718/eval").status_code == 404


def test_database_failure_status_response(tmp_path: Path) -> None:
    fault = {
        "state": "fault",
        "healthy": False,
        "event_count": 0,
        "last_event_timestamp": None,
        "last_event_age_seconds": None,
        "events_per_minute": None,
        "coverage_start_timestamp": None,
        "coverage_end_timestamp": None,
        "average_latency_ms_1m": None,
        "error": "database_unavailable",
    }
    with TestClient(create_app(datasets_dir=tmp_path, store=FakeStore(fault))) as client:
        response = client.get("/api/status")
    assert response.status_code == 200
    assert response.json()["state"] == "fault"
    assert response.json()["error"] == "database_unavailable"


def test_dashboard_blocks_dataset_path_traversal(tmp_path: Path) -> None:
    with TestClient(create_app(datasets_dir=tmp_path, store=FakeStore())) as client:
        response = client.get("/api/datasets/%2E%2E/eval")
        assert response.status_code == 404
