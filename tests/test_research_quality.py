import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest

from trading_bot.research.dataset import sha256_file, write_dataset
from trading_bot.research.quality import require_acceptable_quality, validate_dataset
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


def test_forged_schema_4_quality_report_is_refused(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [100, 101, 102])
    validate_dataset(dataset)
    quality_path = dataset / "quality_report.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality["quality_report_version"] = 4
    quality_path.write_text(json.dumps(quality), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported"):
        require_acceptable_quality(dataset, allow_warnings=False)


def test_clean_dataset_is_valid(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [100, 101, 102])
    report = validate_dataset(dataset)
    assert report["status"] == "pass"
    assert report["row_count"] == 3
    assert report["quality_report_version"] == 5
    assert report["sequence_availability"]["fixture:trades"] == "present"
    assert replay_dataset(dataset)["dataset_quality_status"] == "pass"


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
    assert report["receipt_timestamp_ordering_violations"] == 1
    assert report["exchange_timestamp_ordering_violations"] == 1
    with pytest.raises(ValueError, match="rejected"):
        replay_dataset(dataset)


def test_timestamp_outside_manifest_range_is_rejected(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [100, 101, 102])
    rows = pq.read_table(dataset / "events.parquet").to_pylist()
    rows[0]["exchange_at"] = START - timedelta(seconds=10)
    _replace_rows(dataset, rows)
    report = validate_dataset(dataset)
    assert report["status"] == "rejected"
    assert report["timestamp_manifest_range_violations"] == 1


def test_exchange_timestamp_boundary_excursion_within_tolerance_passes(
    tmp_path: Path,
) -> None:
    dataset = _dataset(tmp_path, [100, 101, 102])
    rows = pq.read_table(dataset / "events.parquet").to_pylist()
    rows[0]["exchange_at"] = START - timedelta(seconds=1)
    _replace_rows(dataset, rows)
    report = validate_dataset(dataset)
    assert report["status"] == "pass"
    assert report["exchange_timestamp_boundary_excursions"] == 1
    assert report["exchange_timestamp_manifest_range_violations"] == 0
    assert report["exchange_boundary_tolerance_seconds"] == 5.0


def test_exchange_timestamp_boundary_excursion_rejects_when_tolerance_zero(
    tmp_path: Path,
) -> None:
    dataset = _dataset(tmp_path, [100, 101, 102])
    rows = pq.read_table(dataset / "events.parquet").to_pylist()
    rows[0]["exchange_at"] = START - timedelta(seconds=1)
    _replace_rows(dataset, rows)
    report = validate_dataset(dataset, exchange_boundary_tolerance_seconds=0)
    assert report["status"] == "rejected"
    assert report["exchange_timestamp_manifest_range_violations"] == 1
    assert report["exchange_timestamp_boundary_excursions"] == 0


def test_partial_sequence_metadata_is_warning(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [100, 101, 102])
    rows = pq.read_table(dataset / "events.parquet").to_pylist()
    rows[1]["sequence"] = None
    _replace_rows(dataset, rows)
    report = validate_dataset(dataset)
    assert report["status"] == "warning"
    assert report["sequence_availability"]["fixture:trades"] == "partial"
    assert any("partial" in finding for finding in report["findings"])


def test_sequence_availability_present_and_absent(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [100, 101, 102, 103])
    rows = pq.read_table(dataset / "events.parquet").to_pylist()
    for index, row in enumerate(rows):
        if index % 2 == 0:
            row["topic"] = "orderbook"
            row["sequence"] = index // 2 + 1
        else:
            row["topic"] = "mark_price"
            row["sequence"] = None
            row["exchange_at"] = None
    _replace_rows(dataset, rows)
    report = validate_dataset(dataset)
    assert report["status"] == "pass"
    assert report["sequence_availability"]["fixture:orderbook"] == "present"
    assert report["sequence_availability"]["fixture:mark_price"] == "absent"


def test_exchange_ordering_is_checked_within_source_topic_streams(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [100, 101, 102, 103])
    rows = pq.read_table(dataset / "events.parquet").to_pylist()
    for index, row in enumerate(rows):
        if index % 2 == 0:
            row["topic"] = "orderbook"
            row["sequence"] = index // 2 + 1
            row["exchange_at"] = (
                row["received_at"]
                if index == 0
                else row["received_at"] - timedelta(milliseconds=1_500)
            )
        else:
            row["topic"] = "mark_price"
            row["sequence"] = None
            row["exchange_at"] = None
    _replace_rows(dataset, rows)
    report = validate_dataset(dataset)
    assert report["status"] == "pass"
    assert report["receipt_timestamp_ordering_violations"] == 0
    assert report["exchange_timestamp_ordering_violations"] == 0


def test_orderbook_snapshot_resets_sequence_baseline_after_reconnect(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [100, 101, 102, 103])
    rows = pq.read_table(dataset / "events.parquet").to_pylist()
    sequences = (100, 101, 50, 51)
    for index, row in enumerate(rows):
        row["topic"] = "orderbook"
        row["sequence"] = sequences[index]
        row["payload_json"] = json.dumps(
            {
                "topic": "orderbook",
                "messageType": "snapshot" if index in {0, 2} else "update",
            }
        )
    _replace_rows(dataset, rows)
    report = validate_dataset(dataset)
    assert report["status"] == "pass"
    assert report["sequence_anomalies"] == 0


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


def test_nested_captured_trade_price_passes(tmp_path: Path) -> None:
    events = [
        MarketEvent(
            id=1,
            received_at=START,
            exchange_at=START,
            source="fixture",
            event_type="trades",
            symbol="ETH/USDT-P",
            sequence=1,
            latency_ms=0.0,
            payload={
                "topic": "trades",
                "symbol": "ETH/USDT-P",
                "data": {
                    "trade": {
                        "price": "2000.25",
                        "quantity": "0.50",
                        "takerSide": "Buy",
                        "timestamp": 1785283201000,
                    }
                },
            },
        )
    ]
    dataset = write_dataset(
        events=events,
        symbol="ETH/USDT-P",
        start=START,
        end=START + timedelta(minutes=1),
        output_root=tmp_path,
    )
    report = validate_dataset(dataset)
    assert report["status"] == "pass"
    assert report["invalid_or_missing_price_count"] == 0


def test_nested_invalid_trade_price_is_warning(tmp_path: Path) -> None:
    events = [
        MarketEvent(
            id=1,
            received_at=START,
            exchange_at=START,
            source="fixture",
            event_type="trades",
            symbol="ETH/USDT-P",
            sequence=1,
            latency_ms=0.0,
            payload={
                "topic": "trades",
                "symbol": "ETH/USDT-P",
                "data": {"trade": {"price": "0", "quantity": "1"}},
            },
        )
    ]
    dataset = write_dataset(
        events=events,
        symbol="ETH/USDT-P",
        start=START,
        end=START + timedelta(minutes=1),
        output_root=tmp_path,
    )
    report = validate_dataset(dataset)
    assert report["status"] == "warning"
    assert report["invalid_or_missing_price_count"] == 1
