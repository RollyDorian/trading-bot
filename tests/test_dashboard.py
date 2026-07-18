import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from trading_bot.dashboard.app import create_app


class FakeStore:
    async def status(self) -> dict[str, Any]:
        return {
            "event_count": 42,
            "last_event_timestamp": "2026-07-18T00:00:00+00:00",
            "healthy": True,
        }

    async def recent(self, limit: int) -> list[dict[str, Any]]:
        return [{"id": index} for index in range(limit)]

    async def close(self) -> None:
        return None


def _dataset(root: Path) -> None:
    directory = root / "v1_20260718"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(
        json.dumps({"version": "v1_20260718", "row_count": 10}),
        encoding="utf-8",
    )
    (directory / "eval_momentum.json").write_text(
        json.dumps({"signal_count": 2, "win_rate": 0.5}),
        encoding="utf-8",
    )


def test_dashboard_read_only_endpoints(tmp_path: Path) -> None:
    _dataset(tmp_path)
    with TestClient(create_app(datasets_dir=tmp_path, store=FakeStore())) as client:
        assert client.get("/api/status").json()["event_count"] == 42
        datasets = client.get("/api/datasets").json()
        assert datasets[0]["manifest"]["version"] == "v1_20260718"
        assert client.get("/api/datasets/v1_20260718/eval").json()["signal_count"] == 2
        assert len(client.get("/api/market/recent?limit=3").json()) == 3
        assert client.post("/api/status").status_code == 405


def test_dashboard_blocks_dataset_path_traversal(tmp_path: Path) -> None:
    with TestClient(create_app(datasets_dir=tmp_path, store=FakeStore())) as client:
        response = client.get("/api/datasets/%2E%2E/eval")
        assert response.status_code == 404
