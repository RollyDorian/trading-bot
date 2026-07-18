import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest

from trading_bot.research.dataset import sha256_file, write_dataset
from trading_bot.research.quality import validate_dataset
from trading_bot.research.replay import replay_dataset
from trading_bot.storage.models import MarketEvent

START = datetime(2026, 7, 18, tzinfo=UTC)


def _dataset(tmp_path: Path, prices: list[float]) -> Path:
    events = [
        MarketEvent(
            id=index + 1,
            received_at=START + timedelta(seconds=index),
            exchange_at=START + timedelta(seconds=index),
            source="fixture",
            event_type="trades",
            symbol="ETH/USDT-P",
            sequence=index + 1,
            latency_ms=0.0,
            payload={"topic": "trades", "price": price, "quantity": 1},
        )
        for index, price in enumerate(prices)
    ]
    return write_dataset(
        events=events,
        symbol="ETH/USDT-P",
        start=START,
        end=START + timedelta(minutes=1),
        output_root=tmp_path,
    )


def _replace_rows(dataset: Path, rows: list[dict[str, object]]) -> None:
    path = dataset / "events.parquet"
    schema = pq.read_table(path).schema
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)
    manifest_path = dataset / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["row_counts"]["events"] = len(rows)
    manifest["checksums"]["events.parquet"] = sha256_file(path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_clean_dataset_is_valid(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [100, 101, 102])
    report = validate_dataset(dataset)
    assert report["status"] == "valid"
    assert report["row_count"] == 3
    assert replay_dataset(dataset)["dataset_quality_status"] == "valid"


def test_duplicates_are_warning(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [100, 101])
    rows = pq.read_table(dataset / "events.parquet").to_pylist()
    _replace_rows(dataset, [*rows, rows[-1]])
    report = validate_dataset(dataset)
    assert report["status"] == "warning"
    assert report["duplicate_event_count"] == 1


def test_timestamp_disorder_is_rejected(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [100, 101, 102])
    rows = pq.read_table(dataset / "events.parquet").to_pylist()
    _replace_rows(dataset, [rows[1], rows[0], rows[2]])
    report = validate_dataset(dataset)
    assert report["status"] == "rejected"
    assert report["timestamp_ordering_violations"] == 1
    with pytest.raises(ValueError, match="rejected"):
        replay_dataset(dataset)


def test_empty_dataset_is_rejected(tmp_path: Path) -> None:
    report = validate_dataset(_dataset(tmp_path, []))
    assert report["status"] == "rejected"


def test_price_discontinuity_requires_warning_override(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [100, 101, 150])
    report = validate_dataset(dataset, price_discontinuity_percent=20)
    assert report["status"] == "warning"
    assert report["price_discontinuity_count"] == 1
    with pytest.raises(ValueError, match="--allow-warnings"):
        replay_dataset(dataset)
    evaluation = replay_dataset(dataset, allow_warnings=True)
    assert evaluation["quality_warnings_allowed"] is True


def test_changed_manifest_refuses_replay(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [100, 101, 102])
    validate_dataset(dataset)
    manifest_path = dataset / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest changed"):
        replay_dataset(dataset)


def test_changed_parquet_refuses_replay(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [100, 101, 102])
    validate_dataset(dataset)
    path = dataset / "events.parquet"
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="Parquet input changed"):
        replay_dataset(dataset)


def test_missing_parquet_refuses_replay(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [100, 101, 102])
    validate_dataset(dataset)
    (dataset / "events.parquet").unlink()
    with pytest.raises(ValueError, match="Parquet inputs changed"):
        replay_dataset(dataset, allow_warnings=True)
