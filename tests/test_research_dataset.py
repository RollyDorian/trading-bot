import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_bot.research.dataset import (
    DatasetValidationError,
    aggregate_candles,
    generate_dataset_id,
    sha256_file,
    validate_manifest,
    write_dataset,
)
from trading_bot.storage.models import MarketEvent

START = datetime(2026, 7, 18, tzinfo=UTC)


def _event(
    event_id: int,
    seconds: int,
    price: str,
    quantity: str | None,
) -> MarketEvent:
    payload: dict[str, object] = {"topic": "trades", "price": price}
    if quantity is not None:
        payload["quantity"] = quantity
    return MarketEvent(
        id=event_id,
        received_at=START + timedelta(seconds=seconds, milliseconds=100 * event_id),
        exchange_at=START + timedelta(seconds=seconds, milliseconds=50 * event_id),
        source="hibachi_ws",
        event_type="trades",
        symbol="ETH/USDT-P",
        sequence=event_id,
        latency_ms=50.0,
        payload=payload,
    )


def test_dataset_id_is_deterministic_and_bounded() -> None:
    end = START + timedelta(hours=1)
    expected = "eth-usdt-p_20260718T000000000000Z_20260718T010000000000Z_v1"
    assert generate_dataset_id("ETH/USDT-P", START, end) == expected
    assert generate_dataset_id("ETH/USDT-P", START, end) == expected


def test_candle_aggregation_is_chronological_and_does_not_invent_volume() -> None:
    candles = aggregate_candles(
        [_event(3, 1, "102", "2"), _event(1, 0, "100", "1"), _event(2, 0, "101", None)]
    )
    assert [(item.open, item.high, item.low, item.close) for item in candles] == [
        (100.0, 101.0, 100.0, 101.0),
        (102.0, 102.0, 102.0, 102.0),
    ]
    assert candles[0].volume is None
    assert candles[1].volume == 2.0


def test_checksums_and_manifest_validation_fail_closed(tmp_path: Path) -> None:
    dataset_dir = write_dataset(
        events=[_event(1, 0, "100", "1")],
        symbol="ETH/USDT-P",
        start=START,
        end=START + timedelta(minutes=1),
        output_root=tmp_path,
    )
    manifest = validate_manifest(dataset_dir)
    assert manifest["checksums"]["events.parquet"] == sha256_file(
        dataset_dir / "events.parquet"
    )
    (dataset_dir / "events.parquet").write_bytes(b"corrupt")
    with pytest.raises(DatasetValidationError, match="checksum mismatch"):
        validate_manifest(dataset_dir)


def test_manifest_rejects_unknown_schema_version(tmp_path: Path) -> None:
    dataset_dir = write_dataset(
        events=[],
        symbol="ETH/USDT-P",
        start=START,
        end=START + timedelta(minutes=1),
        output_root=tmp_path,
    )
    path = dataset_dir / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 999
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DatasetValidationError, match="Unsupported"):
        validate_manifest(dataset_dir)


def test_dataset_artifact_checksums_are_reproducible(tmp_path: Path) -> None:
    arguments = {
        "events": [_event(1, 0, "100", "1"), _event(2, 1, "101", "2")],
        "symbol": "ETH/USDT-P",
        "start": START,
        "end": START + timedelta(minutes=1),
    }
    first = write_dataset(**arguments, output_root=tmp_path / "first")
    second = write_dataset(**arguments, output_root=tmp_path / "second")
    assert validate_manifest(first)["checksums"] == validate_manifest(second)["checksums"]
