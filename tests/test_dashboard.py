import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from trading_bot.dashboard.app import create_app


class FakeStore:
    async def status(self) -> dict[str, Any]:
        return {"event_count": 42, "last_event_timestamp": "2026-07-18T00:00:00+00:00", "healthy": True}

    async def recent(self, limit: int) -> list[dict[str, Any]]:
        return [{"id": index} for index in range(limit)]

    async def system_events(self, limit: int) -> list[dict[str, Any]]:
        return [
            {
                "id": 7,
                "occurred_at": "2026-07-18T00:00:00+00:00",
                "severity": "WARNING",
                "event_type": "DEGRADED",
                "component": "collector_supervisor",
                "message": "Reconnect failed at redis://:cache-secret@cache:6379/0",
                "details": {
                    "exception_class": "ConnectionError",
                    "access_token": "api-secret",
                },
            }
        ][:limit]

    async def system_event(self, event_id: int) -> dict[str, Any] | None:
        events = await self.system_events(1)
        return events[0] if event_id == 7 else None

    async def close(self) -> None:
        return None


def _dataset(root: Path, version: str, status: str | None) -> None:
    directory = root / version
    directory.mkdir()
    (directory / "manifest.json").write_text(
        json.dumps({"dataset_id": version, "row_counts": {"events": 3}}),
        encoding="utf-8",
    )
    if status is not None:
        (directory / "quality_report.json").write_text(
            json.dumps({"status": status, "findings": [f"{status} reason"]}),
            encoding="utf-8",
        )


def test_dashboard_quality_states_and_system_detail(tmp_path: Path) -> None:
    for index, status in enumerate((None, "valid", "warning", "rejected")):
        _dataset(tmp_path, f"v{index}", status)
    with TestClient(create_app(datasets_dir=tmp_path, store=FakeStore())) as client:
        datasets = client.get("/api/datasets").json()
        assert {item["quality_status"] for item in datasets} == {
            "not_validated",
            "valid",
            "warning",
            "rejected",
        }
        detail = client.get("/api/system/7").json()
        assert detail["details"]["exception_class"] == "ConnectionError"
        assert "cache-secret" not in json.dumps(detail)
        assert "api-secret" not in json.dumps(detail)
        assert client.get("/api/system/8").status_code == 404
        assert "Not validated" in client.get("/").text


def test_dashboard_research_endpoints(tmp_path: Path) -> None:
    _dataset(tmp_path, "v1_20260718", "valid")
    (tmp_path / "v1_20260718" / "eval_momentum.json").write_text(
        json.dumps({"signal_count": 2, "win_rate": 0.5}), encoding="utf-8"
    )
    with TestClient(create_app(datasets_dir=tmp_path, store=FakeStore())) as client:
        assert client.get("/api/status").json()["event_count"] == 42
        assert client.get("/api/datasets/v1_20260718/eval").json()["signal_count"] == 2
        assert len(client.get("/api/market/recent?limit=3").json()) == 3
        assert client.post("/api/status").status_code == 405


def test_dashboard_blocks_dataset_path_traversal(tmp_path: Path) -> None:
    with TestClient(create_app(datasets_dir=tmp_path, store=FakeStore())) as client:
        assert client.get("/api/datasets/%2E%2E/eval").status_code == 404
