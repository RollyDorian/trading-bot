"""Phase 0 admission-readiness audit for quality schema 5 and admission gates.

Scope: quality validation, admission evaluation, receipt/exchange ordering, and
sequence-availability classification only. Does not exercise collection,
deployment, REST clients, migrations, or backfill.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest

from trading_bot.research.admission import (
    AdmissionInputError,
    AdmissionThresholds,
    evaluate_admission,
)
from trading_bot.research.dataset import write_dataset
from trading_bot.research.quality import require_acceptable_quality, validate_dataset
from trading_bot.research.replay import BaselineConfig, CostConfig, configuration_hash
from trading_bot.storage.models import MarketEvent

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "hibachi"
FIXTURE_ANCHOR = datetime(2026, 7, 29, tzinfo=UTC)
ADMISSION_START = datetime(2026, 7, 1, tzinfo=UTC)
PASSING_THRESHOLDS = AdmissionThresholds(
    minimum_quality_passing_datasets=4,
    minimum_oos_dataset_count=2,
    minimum_oos_trade_count=2,
    maximum_oos_drawdown=20,
    minimum_oos_utc_days=2,
)

TOPIC_FIXTURES: tuple[tuple[str, str], ...] = (
    ("ask_bid_price.json", "ask_bid_price"),
    ("funding_rate_estimation.json", "funding_rate_estimation"),
    ("mark_price.json", "mark_price"),
    ("spot_price.json", "spot_price"),
    ("trades.json", "trades"),
    ("orderbook_snapshot.json", "orderbook"),
)


def _load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _exchange_at_from_payload(payload: dict[str, object]) -> datetime | None:
    timestamp_ms = payload.get("timestamp_ms")
    if isinstance(timestamp_ms, int):
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    return None


def _fixture_event(
    event_id: int,
    topic: str,
    payload: dict[str, object],
    *,
    seconds: int = 0,
    sequence: int | None = None,
    local_sequence: int | None = None,
    exchange_sequence: int | None = None,
    connection_id: str | None = None,
    schema_version: int = 2,
) -> MarketEvent:
    received_at = FIXTURE_ANCHOR + timedelta(seconds=seconds)
    return MarketEvent(
        id=event_id,
        received_at=received_at,
        exchange_at=_exchange_at_from_payload(payload),
        source="fixture",
        event_type=topic,
        symbol="ETH/USDT-P",
        sequence=sequence,
        connection_id=connection_id,
        local_sequence=local_sequence,
        exchange_sequence=exchange_sequence,
        schema_version=schema_version,
        latency_ms=0.0,
        payload=payload,
    )


def _write_fixture_dataset(
    tmp_path: Path,
    events: list[MarketEvent],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Path:
    window_start = start or FIXTURE_ANCHOR
    window_end = end or (FIXTURE_ANCHOR + timedelta(minutes=1))
    return write_dataset(
        events=events,
        symbol="ETH/USDT-P",
        start=window_start,
        end=window_end,
        output_root=tmp_path,
    )


def _attach_offline_replay(
    dataset_dir: Path,
    *,
    net_pnl: float = 10.0,
    trade_count: int = 1,
) -> None:
    manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    start = datetime.fromisoformat(str(manifest["start_utc"])).astimezone(UTC)
    end = datetime.fromisoformat(str(manifest["end_utc"])).astimezone(UTC)
    trades = [
        {
            "direction": 1,
            "entry_time": (start + timedelta(minutes=30 + 60 * index)).isoformat(),
            "exit_time": (start + timedelta(minutes=45 + 60 * index)).isoformat(),
            "entry_price": 100,
            "exit_price": 101,
            "gross_pnl": net_pnl + 3,
            "fees": 1,
            "funding": 1,
            "slippage": 1,
            "net_pnl": net_pnl,
        }
        for index in range(trade_count)
    ]
    signal = BaselineConfig()
    costs = CostConfig()
    report = {
        "result_type": "offline_research_simulation",
        "dataset_id": dataset_dir.name,
        "configuration_hash": configuration_hash(signal, costs),
        "configuration": {"signal": asdict(signal), "costs": asdict(costs)},
        "simulated_exits": trade_count,
        "gross_pnl": (net_pnl + 3) * trade_count,
        "fees": trade_count,
        "funding": trade_count,
        "slippage_and_latency": trade_count,
        "net_pnl": net_pnl * trade_count,
        "trades": trades,
        "dataset_quality_status": "pass",
        "quality_warnings_allowed": False,
    }
    (dataset_dir / "offline_replay.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    assert end > start


def _admission_dataset(
    root: Path,
    day: float,
    *,
    net_pnl: float = 10.0,
    trade_count: int = 1,
) -> Path:
    start = ADMISSION_START + timedelta(days=day)
    events = [
        MarketEvent(
            id=index + 1,
            received_at=start + timedelta(seconds=index),
            exchange_at=start + timedelta(seconds=index),
            source="fixture",
            event_type="trades",
            symbol="ETH/USDT-P",
            sequence=index + 1,
            latency_ms=0.0,
            payload={"price": 100 + index, "quantity": 1},
        )
        for index in range(2)
    ]
    directory = write_dataset(
        events=events,
        symbol="ETH/USDT-P",
        start=start,
        end=start + timedelta(days=1),
        output_root=root,
    )
    validate_dataset(directory, now=start + timedelta(days=1))
    _attach_offline_replay(directory, net_pnl=net_pnl, trade_count=trade_count)
    return directory


@pytest.mark.parametrize(("fixture_name", "topic"), TOPIC_FIXTURES)
def test_fixture_topic_classifies_sequence_absent(
    tmp_path: Path, fixture_name: str, topic: str
) -> None:
    payload = _load_fixture(fixture_name)
    events = [_fixture_event(1, topic, payload)]
    dataset = _write_fixture_dataset(tmp_path, events)
    report = validate_dataset(dataset)

    assert report["quality_report_version"] == 5
    # `absent` records an evidence limitation; continuity is not proven.
    assert report["sequence_availability"][f"fixture:{topic}"] == "absent"
    assert report["status"] == "pass"
    assert report["sequence_anomalies"] is None


def test_continuous_sequences_classify_present(tmp_path: Path) -> None:
    events = [
        MarketEvent(
            id=index + 1,
            received_at=FIXTURE_ANCHOR + timedelta(seconds=index),
            exchange_at=FIXTURE_ANCHOR + timedelta(seconds=index),
            source="fixture",
            event_type="trades",
            symbol="ETH/USDT-P",
            sequence=index + 1,
            latency_ms=0.0,
            payload={"topic": "trades", "price": 100 + index, "quantity": 1},
        )
        for index in range(3)
    ]
    dataset = _write_fixture_dataset(tmp_path, events)
    report = validate_dataset(dataset)

    assert report["sequence_availability"]["fixture:trades"] == "present"
    assert report["status"] == "pass"
    assert report["sequence_anomalies"] == 0


def test_mixed_sequence_metadata_classifies_partial_and_warns(tmp_path: Path) -> None:
    events = [
        MarketEvent(
            id=index + 1,
            received_at=FIXTURE_ANCHOR + timedelta(seconds=index),
            exchange_at=FIXTURE_ANCHOR + timedelta(seconds=index),
            source="fixture",
            event_type="trades",
            symbol="ETH/USDT-P",
            sequence=index + 1 if index < 2 else None,
            latency_ms=0.0,
            payload={"topic": "trades", "price": 100 + index, "quantity": 1},
        )
        for index in range(3)
    ]
    dataset = _write_fixture_dataset(tmp_path, events)
    report = validate_dataset(dataset)

    assert report["sequence_availability"]["fixture:trades"] == "partial"
    assert report["status"] == "warning"
    assert any("partial" in finding for finding in report["findings"])


def test_absent_pass_does_not_invent_sequences(tmp_path: Path) -> None:
    payload = _load_fixture("mark_price.json")
    dataset = _write_fixture_dataset(
        tmp_path, [_fixture_event(1, "mark_price", payload)]
    )
    report = validate_dataset(dataset)

    assert report["status"] == "pass"
    assert report["sequence_availability"]["fixture:mark_price"] == "absent"
    assert report["sequence_anomalies"] is None
    rows = pq.read_table(dataset / "events.parquet").to_pylist()
    assert all(row["sequence"] is None for row in rows)
    # Quality report must not fabricate per-row sequence values; only metadata keys.
    assert set(report["sequence_availability"].values()) == {"absent"}


def test_orderbook_fixtures_remain_absent_without_sequence_fields(
    tmp_path: Path,
) -> None:
    fixtures = (
        "orderbook_snapshot.json",
        "orderbook_update.json",
        "orderbook_empty_update.json",
    )
    events = [
        _fixture_event(index + 1, "orderbook", _load_fixture(name), seconds=index)
        for index, name in enumerate(fixtures)
    ]
    dataset = _write_fixture_dataset(tmp_path, events)
    report = validate_dataset(dataset)

    assert report["sequence_availability"]["fixture:orderbook"] == "absent"
    assert report["sequence_anomalies"] is None
    assert report["status"] == "pass"


def test_gapped_sequences_warn_and_count_anomalies(tmp_path: Path) -> None:
    events = [
        MarketEvent(
            id=index + 1,
            received_at=FIXTURE_ANCHOR + timedelta(seconds=index),
            exchange_at=FIXTURE_ANCHOR + timedelta(seconds=index),
            source="fixture",
            event_type="trades",
            symbol="ETH/USDT-P",
            sequence=sequence,
            latency_ms=0.0,
            payload={"topic": "trades", "price": 100 + index, "quantity": 1},
        )
        for index, sequence in enumerate((1, 2, 5))
    ]
    dataset = _write_fixture_dataset(tmp_path, events)
    report = validate_dataset(dataset)

    assert report["sequence_availability"]["fixture:trades"] == "present"
    assert report["sequence_anomalies"] == 1
    assert report["status"] == "warning"


def test_equal_exchange_timestamps_do_not_violate_stream_order(tmp_path: Path) -> None:
    shared_exchange_at = FIXTURE_ANCHOR + timedelta(seconds=5)
    events = [
        MarketEvent(
            id=index + 1,
            received_at=FIXTURE_ANCHOR + timedelta(seconds=index),
            exchange_at=shared_exchange_at,
            source="fixture",
            event_type="orderbook",
            symbol="ETH/USDT-P",
            sequence=None,
            latency_ms=0.0,
            payload=_load_fixture("orderbook_snapshot.json"),
        )
        for index in range(2)
    ]
    dataset = _write_fixture_dataset(tmp_path, events)
    report = validate_dataset(dataset)

    assert report["status"] == "pass"
    assert report["exchange_timestamp_ordering_violations"] == 0


def test_equal_received_at_uses_id_tie_breaker_in_export_order(tmp_path: Path) -> None:
    shared_received_at = FIXTURE_ANCHOR + timedelta(seconds=10)
    events = [
        MarketEvent(
            id=event_id,
            received_at=shared_received_at,
            exchange_at=None,
            source="fixture",
            event_type="mark_price",
            symbol="ETH/USDT-P",
            sequence=None,
            latency_ms=0.0,
            payload=_load_fixture("mark_price.json"),
        )
        for event_id in (3, 1, 2)
    ]
    dataset = write_dataset(
        events=events,
        symbol="ETH/USDT-P",
        start=FIXTURE_ANCHOR,
        end=FIXTURE_ANCHOR + timedelta(minutes=1),
        output_root=tmp_path,
    )
    rows = pq.read_table(dataset / "events.parquet").to_pylist()
    assert [row["raw_event_id"] for row in rows] == [1, 2, 3]
    report = validate_dataset(dataset)
    assert report["receipt_timestamp_ordering_violations"] == 0
    assert report["status"] == "pass"


def test_adjacent_utc_day_windows_do_not_overlap(tmp_path: Path) -> None:
    day_end = datetime(2026, 7, 2, 0, 0, tzinfo=UTC)
    day_start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    datasets = [
        _admission_dataset(tmp_path / "a", 0),
        _admission_dataset(tmp_path / "b", 1),
        _admission_dataset(tmp_path / "c", 2),
        _admission_dataset(tmp_path / "d", 3),
    ]
    manifest_first = json.loads((datasets[0] / "manifest.json").read_text(encoding="utf-8"))
    manifest_second = json.loads((datasets[1] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_first["end_utc"] == day_end.isoformat()
    assert manifest_second["start_utc"] == day_end.isoformat()
    assert day_end == day_start + timedelta(days=1)

    report = evaluate_admission(
        datasets,
        validation_count=1,
        oos_count=2,
        thresholds=PASSING_THRESHOLDS,
    )
    assert report["admitted"] is True
    assert all(item["admissible"] for item in report["datasets"])


def test_overlapping_utc_windows_raise_admission_input_error(tmp_path: Path) -> None:
    overlap_root = tmp_path / "overlap"
    datasets = [
        _admission_dataset(overlap_root, 0),
        _admission_dataset(overlap_root, 0.5),
        _admission_dataset(overlap_root, 2),
    ]
    with pytest.raises(AdmissionInputError, match="overlap"):
        evaluate_admission(
            datasets, validation_count=1, oos_count=1, thresholds=PASSING_THRESHOLDS
        )


def test_adjacent_half_open_windows_allow_end_equals_next_start(tmp_path: Path) -> None:
    root = tmp_path / "adjacent"
    datasets = [
        _admission_dataset(root, day, trade_count=2)
        for day in range(4)
    ]
    manifests = [
        json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        for path in datasets[:3]
    ]
    assert manifests[0]["end_utc"] == manifests[1]["start_utc"]
    assert manifests[1]["end_utc"] == manifests[2]["start_utc"]

    report = evaluate_admission(
        datasets, validation_count=1, oos_count=2, thresholds=PASSING_THRESHOLDS
    )
    assert report["admitted"] is True


def test_receipt_gap_above_threshold_warns(tmp_path: Path) -> None:
    events = [
        MarketEvent(
            id=1,
            received_at=FIXTURE_ANCHOR,
            exchange_at=FIXTURE_ANCHOR,
            source="fixture",
            event_type="trades",
            symbol="ETH/USDT-P",
            sequence=1,
            latency_ms=0.0,
            payload={"topic": "trades", "price": 100, "quantity": 1},
        ),
        MarketEvent(
            id=2,
            received_at=FIXTURE_ANCHOR + timedelta(seconds=120),
            exchange_at=FIXTURE_ANCHOR + timedelta(seconds=120),
            source="fixture",
            event_type="trades",
            symbol="ETH/USDT-P",
            sequence=2,
            latency_ms=0.0,
            payload={"topic": "trades", "price": 101, "quantity": 1},
        ),
    ]
    dataset = _write_fixture_dataset(
        tmp_path,
        events,
        end=FIXTURE_ANCHOR + timedelta(minutes=5),
    )
    report = validate_dataset(dataset)

    assert report["status"] == "warning"
    assert report["largest_timestamp_gap_seconds"] > 60.0


def test_admission_refuses_warning_quality_datasets(tmp_path: Path) -> None:
    datasets = [
        _admission_dataset(tmp_path, day, trade_count=2)
        for day in range(4)
    ]
    gap_events = [
        MarketEvent(
            id=1,
            received_at=ADMISSION_START + timedelta(days=4),
            exchange_at=ADMISSION_START + timedelta(days=4),
            source="fixture",
            event_type="trades",
            symbol="ETH/USDT-P",
            sequence=1,
            latency_ms=0.0,
            payload={"price": 100, "quantity": 1},
        ),
        MarketEvent(
            id=2,
            received_at=ADMISSION_START + timedelta(days=4, seconds=120),
            exchange_at=ADMISSION_START + timedelta(days=4, seconds=120),
            source="fixture",
            event_type="trades",
            symbol="ETH/USDT-P",
            sequence=2,
            latency_ms=0.0,
            payload={"price": 101, "quantity": 1},
        ),
    ]
    warning_dataset = write_dataset(
        events=gap_events,
        symbol="ETH/USDT-P",
        start=ADMISSION_START + timedelta(days=4),
        end=ADMISSION_START + timedelta(days=5),
        output_root=tmp_path,
    )
    validate_dataset(warning_dataset, now=ADMISSION_START + timedelta(days=5))
    assert json.loads((warning_dataset / "quality_report.json").read_text())["status"] == "warning"
    _attach_offline_replay(warning_dataset, trade_count=2)

    report = evaluate_admission(
        [*datasets, warning_dataset],
        validation_count=1,
        oos_count=2,
        thresholds=PASSING_THRESHOLDS,
    )
    warning_item = next(
        item for item in report["datasets"] if item["version"] == warning_dataset.name
    )
    assert warning_item["admissible"] is False
    assert warning_item["rejection_reason"] is not None
    assert "warning" in warning_item["rejection_reason"].lower()


def test_legacy_rows_classify_absent_without_using_local_sequence(
    tmp_path: Path,
) -> None:
    payload = _load_fixture("mark_price.json")
    events = [
        _fixture_event(
            1,
            "mark_price",
            payload,
            sequence=None,
            local_sequence=99,
            exchange_sequence=None,
            connection_id=None,
            schema_version=1,
        )
    ]
    dataset = _write_fixture_dataset(tmp_path, events)
    report = validate_dataset(dataset)

    assert report["sequence_availability"]["fixture:mark_price"] == "absent"
    assert report["status"] == "pass"
    assert report["sequence_anomalies"] is None
    rows = pq.read_table(dataset / "events.parquet").to_pylist()
    assert rows[0]["local_sequence"] == 99
    assert rows[0]["sequence"] is None


def test_passing_quality_dataset_clears_admission_quality_gate(tmp_path: Path) -> None:
    datasets = [
        _admission_dataset(tmp_path, day, trade_count=2) for day in range(4)
    ]
    for dataset in datasets:
        quality = require_acceptable_quality(dataset, allow_warnings=False)
        assert quality["status"] == "pass"

    report = evaluate_admission(
        datasets,
        validation_count=1,
        oos_count=2,
        thresholds=PASSING_THRESHOLDS,
    )
    assert report["admitted"] is True
    assert all(item["quality_status"] == "pass" for item in report["datasets"])
    assert isinstance(report["evidence_limitations"], list)


def test_admission_pass_includes_absent_stream_evidence_limitations(tmp_path: Path) -> None:
    mark_payload = {
        "topic": "mark_price",
        "symbol": "ETH/USDT-P",
        "data": {"markPrice": "100.5"},
    }
    datasets = [_admission_dataset(tmp_path, day, trade_count=2) for day in range(3)]
    start = ADMISSION_START + timedelta(days=3)
    events = [
        MarketEvent(
            id=index + 1,
            received_at=start + timedelta(seconds=index),
            exchange_at=start + timedelta(seconds=index),
            source="fixture",
            event_type="trades",
            symbol="ETH/USDT-P",
            sequence=index + 1,
            latency_ms=0.0,
            payload={"price": 100 + index, "quantity": 1},
        )
        for index in range(2)
    ]
    events.append(
        MarketEvent(
            id=10,
            received_at=start + timedelta(seconds=5),
            exchange_at=start + timedelta(seconds=5),
            source="fixture",
            event_type="mark_price",
            symbol="ETH/USDT-P",
            sequence=None,
            latency_ms=0.0,
            payload=mark_payload,
        )
    )
    mixed_dir = write_dataset(
        events=events,
        symbol="ETH/USDT-P",
        start=start,
        end=start + timedelta(days=1),
        output_root=tmp_path,
    )
    validate_dataset(mixed_dir, now=start + timedelta(days=1))
    _attach_offline_replay(mixed_dir, trade_count=2)
    datasets.append(mixed_dir)

    report = evaluate_admission(
        datasets,
        validation_count=1,
        oos_count=2,
        thresholds=PASSING_THRESHOLDS,
    )
    assert report["admitted"] is True
    mixed_item = next(item for item in report["datasets"] if item["version"] == mixed_dir.name)
    assert mixed_item["sequence_availability"]["fixture:mark_price"] == "absent"
    assert any(
        limitation.startswith(
            f"sequence_availability_absent:{mixed_dir.name}:fixture:mark_price"
        )
        for limitation in report["evidence_limitations"]
    )
