import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest

from trading_bot.research.exporter import (
    next_version_slug,
    validate_version,
    write_versioned_export,
)
from trading_bot.storage.models import MarketEvent

START = datetime(2026, 7, 18, tzinfo=UTC)


def _event(event_id: int) -> MarketEvent:
    timestamp = START + timedelta(seconds=event_id)
    return MarketEvent(
        id=event_id,
        received_at=timestamp,
        exchange_at=timestamp,
        source="hibachi_ws",
        event_type="ask_bid_price",
        symbol="ETH/USDT-P",
        sequence=event_id,
        latency_ms=1.0,
        payload={"topic": "ask_bid_price", "bidPrice": 100, "askPrice": 102},
    )


def test_version_slug_auto_increments(tmp_path: Path) -> None:
    (tmp_path / "v1_20260717").mkdir()
    (tmp_path / "v3_custom").mkdir()
    assert next_version_slug(tmp_path, START) == "v4_20260718"


def test_version_rejects_path_traversal() -> None:
    with pytest.raises(ValueError, match="Version"):
        validate_version("../escape")


def test_versioned_export_layout_and_metadata(tmp_path: Path) -> None:
    dataset_dir = write_versioned_export(
        events=[_event(2), _event(1)],
        output_root=tmp_path,
        version="v1_20260718",
        symbol="ETH/USDT-P",
        start=START,
        end=START + timedelta(minutes=1),
    )
    parquet_path = dataset_dir / "ETH-USDT-P.parquet"
    manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    assert parquet_path.is_file()
    assert manifest["version"] == "v1_20260718"
    assert manifest["exchange"] == "hibachi"
    assert manifest["row_count"] == 2
    assert pq.read_table(parquet_path).column("id").to_pylist() == [1, 2]
