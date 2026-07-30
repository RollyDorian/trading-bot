import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pyarrow.fs as pafs  # type: ignore[import-untyped]
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.test_normalization_parsers import raw
from trading_bot.archive.capacity import GIB, CapacityInputs, plan_capacity
from trading_bot.archive.exporter import (
    ArchiveExporter,
    ArchiveRequest,
    _rows,
    _write_chunk,
)
from trading_bot.archive.manifest import ArchiveManifest, ArchiveObject, raw_id_digest
from trading_bot.archive.retention import plan_retention
from trading_bot.archive.store import LocalArchiveStore, PcArchiveStore, S3ArchiveStore


def test_capacity_plan_preserves_reserve_and_rejects_unsafe_window() -> None:
    safe = plan_capacity(
        CapacityInputs(
            disk_free_bytes=8 * GIB,
            raw_mib_per_day=400,
            normalized_mib_per_day=0,
            parquet_mib_per_day=100,
            wal_mib_per_day=100,
            measured_days=4,
            requested_raw_hot_days=3,
        )
    )
    assert safe.state == "safe"
    assert safe.raw_hot_days == 3
    assert safe.normalized_hot_days == 0
    assert safe.confidence == "measured"
    blocked = plan_capacity(
        replace(
            CapacityInputs(disk_free_bytes=0),
            disk_free_bytes=3 * GIB,
            raw_mib_per_day=400,
            measured_days=1,
        )
    )
    assert blocked.state == "blocked"
    assert blocked.raw_hot_days == 0
    assert blocked.confidence == "extrapolated"
    with pytest.raises(ValueError, match="hot-window"):
        plan_capacity(
            CapacityInputs(
                disk_free_bytes=8 * GIB,
                requested_raw_hot_days=2,
            )
        )
    degraded = plan_capacity(
        CapacityInputs(
            disk_free_bytes=8 * GIB,
            raw_mib_per_day=100,
            measured_days=3,
            requested_raw_hot_days=2,
            allow_degraded_two_day=True,
        )
    )
    assert degraded.state == "warning"
    assert degraded.raw_hot_days == 2


def test_partition_request_is_one_aligned_utc_day(tmp_path: Path) -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    request = ArchiveRequest(
        start=start,
        end=start + timedelta(days=1),
        symbol="ETH/USDT-P",
        work_dir=tmp_path,
        capacity_path=tmp_path,
    )
    assert request.batch_size == 5000
    with pytest.raises(ValueError, match="one UTC day"):
        replace(request, end=start + timedelta(hours=1))
    with pytest.raises(ValueError, match="inter-batch delay"):
        replace(request, inter_batch_delay_seconds=10.1)


def test_rows_preserve_raw_and_separate_normalized_contracts() -> None:
    events = [
        raw("ask_bid_price", raw_id=1),
        raw("mark_price", raw_id=2),
        raw("funding_rate_estimation", raw_id=3),
        raw("orderbook_snapshot", raw_id=4),
    ]
    rows = _rows(events)
    assert len(rows["raw"]) == 4
    assert len(rows["best_quotes"]) == 1
    assert len(rows["reference_prices"]) == 1
    assert len(rows["funding_estimates"]) == 1
    assert len(rows["orderbook_events"]) == 1
    assert rows["raw"][0]["payload_json"] == json.dumps(
        events[0].payload,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert "payload_json" not in rows["best_quotes"][0]


def test_local_and_s3_adapters_publish_without_partial_objects(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"archive")
    local = LocalArchiveStore(tmp_path / "local")
    local.publish_file("dataset/item", source)
    assert local.read_bytes("dataset/item") == b"archive"
    with pytest.raises(ValueError, match="unsafe"):
        local.publish_bytes("../escape", b"x")

    subtree_root = tmp_path / "s3"
    (subtree_root / "bucket").mkdir(parents=True)
    filesystem = pafs.SubTreeFileSystem(str(subtree_root), pafs.LocalFileSystem())
    s3 = S3ArchiveStore(bucket="bucket", filesystem=filesystem)
    s3.publish_bytes("manifest.json", b"verified")
    assert s3.destination_label == "s3"
    assert s3.read_bytes("manifest.json") == b"verified"
    assert not list(subtree_root.rglob("*.partial"))

    pc = PcArchiveStore(tmp_path / "pc")
    pc.publish_bytes("manifest.json", b"verified")
    assert pc.destination_label == "pc_filesystem"
    assert pc.read_bytes("manifest.json") == b"verified"
    assert not list((tmp_path / "pc").rglob("*.partial-*"))


def _manifest(tmp_path: Path, destination: str = "s3") -> ArchiveManifest:
    events = [raw("ask_bid_price", raw_id=1), raw("mark_price", raw_id=2)]
    path, obj = _write_chunk(
        dataset="raw",
        rows=_rows(events)["raw"],
        directory=tmp_path,
        first_id=1,
        last_id=2,
    )
    store = LocalArchiveStore(tmp_path / "store")
    key = "raw/date=2026-07-01/symbol=ETH-USDT-P/part-1-2.parquet"
    store.publish_file(key, path)
    obj = ArchiveObject(**{**asdict(obj), "key": key})
    return ArchiveManifest(
        dataset_group="raw_and_normalized",
        interval_start_utc="2026-07-01T00:00:00+00:00",
        interval_end_utc="2026-07-02T00:00:00+00:00",
        symbol="ETH/USDT-P",
        min_raw_event_id=1,
        max_raw_event_id=2,
        raw_row_count=2,
        raw_id_sha256=__import__("hashlib").sha256(
            obj.raw_id_sha256.encode("ascii")
        ).hexdigest(),
        pipeline_version=1,
        schema_version=1,
        created_at_utc="2026-07-03T00:00:00+00:00",
        destination=destination,
        verification_status="verified",
        objects=(obj,),
    )


def test_verifier_rejects_corrupt_or_mismatched_archive(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    store = LocalArchiveStore(tmp_path / "store")
    factory = cast(async_sessionmaker[AsyncSession], object())
    exporter = ArchiveExporter(factory, store)
    exporter.verify(manifest, tmp_path / "work")
    store.publish_bytes(manifest.objects[0].key, b"truncated")
    with pytest.raises(RuntimeError, match="checksum"):
        exporter.verify(manifest, tmp_path / "work")
    mismatch = replace(manifest, raw_row_count=3)
    with pytest.raises(RuntimeError, match="row count"):
        exporter.verify(mismatch, tmp_path / "work")


def test_retention_plan_requires_external_verified_contiguous_days(tmp_path: Path) -> None:
    first = _manifest(tmp_path)
    second = replace(
        first,
        interval_start_utc="2026-07-02T00:00:00+00:00",
        interval_end_utc="2026-07-03T00:00:00+00:00",
        min_raw_event_id=3,
        max_raw_event_id=4,
    )
    plan = plan_retention(
        [(first, "a" * 64), (second, "b" * 64)],
        now=datetime(2026, 7, 10, tzinfo=UTC),
        hot_raw_days=2,
    )
    assert plan.dry_run is True
    assert plan.eligible_rows == 4
    assert plan.state == "eligible"
    local_only = replace(first, destination="filesystem")
    assert (
        plan_retention(
            [(local_only, "a" * 64)],
            now=datetime(2026, 7, 10, tzinfo=UTC),
            hot_raw_days=2,
        ).state
        == "nothing_eligible"
    )
    pc_external = replace(first, destination="pc_filesystem")
    assert (
        plan_retention(
            [(pc_external, "a" * 64)],
            now=datetime(2026, 7, 10, tzinfo=UTC),
            hot_raw_days=3,
        ).state
        == "eligible"
    )


def test_raw_id_digest_is_order_sensitive() -> None:
    assert raw_id_digest([1, 2]) != raw_id_digest([2, 1])
