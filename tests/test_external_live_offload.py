"""Live wiring tests: segmented spool + async offload worker."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading_bot.archive.store import LocalArchiveStore
from trading_bot.external_market_data.envelope import ExternalRawEnvelope
from trading_bot.external_market_data.offload.capacity import (
    BacklogAction,
    CapacityPolicy,
    measure_local_external_bytes,
)
from trading_bot.external_market_data.offload.lifecycle import reclaim_local_segment
from trading_bot.external_market_data.offload.segments import SegmentState, read_state
from trading_bot.external_market_data.offload.worker import AsyncOffloadWorker
from trading_bot.external_market_data.segmented_spool import SegmentedExternalSpool
from trading_bot.external_market_data.spool import ExternalCapacityStop


def _env(seq: int) -> ExternalRawEnvelope:
    return ExternalRawEnvelope(
        venue="binance_usdm",
        instrument="ETHUSDT",
        event_type="book_ticker",
        received_at=datetime.now(UTC),
        connection_id="c1",
        local_sequence=seq,
        schema_version=1,
        payload={"u": seq, "b": "1", "B": "1", "a": "1", "A": "1"},
        stream="bookTicker",
        book_update_id=seq,
    )


def test_segmented_writer_seals_and_opens_next(tmp_path: Path) -> None:
    free = {"v": 10 * 1024**3}

    spool = SegmentedExternalSpool(
        tmp_path / "segments",
        policy=CapacityPolicy(
            pressure_bytes=10**9,
            stop_bytes=10**9,
            global_floor_bytes=5 * 1024**3,
            floor_margin_bytes=100,
        ),
        max_segment_bytes=800,
        max_segment_seconds=10**9,
        free_bytes_fn=lambda _p: free["v"],
    )
    spool.open()
    sealed_ids: list[str] = []
    for i in range(30):
        maybe = spool.append(_env(i))
        if maybe is not None:
            sealed_ids.append(maybe.segment_id)
    final = spool.close()
    if final:
        sealed_ids.append(final.segment_id)
    assert len(sealed_ids) >= 2
    assert spool.stats.segments_sealed == len(sealed_ids)
    # Exactly one ACTIVE at most after close (none).
    actives = [
        p
        for p in (tmp_path / "segments").iterdir()
        if p.is_dir() and (p / "events.active.ndjson").exists()
    ]
    assert actives == []


def test_worker_handoff_reclaim_after_verify(tmp_path: Path) -> None:
    root = tmp_path / "segments"
    store = LocalArchiveStore(tmp_path / "store")
    spool = SegmentedExternalSpool(
        root,
        policy=CapacityPolicy(
            pressure_bytes=10**9,
            stop_bytes=10**9,
            global_floor_bytes=1024,
            floor_margin_bytes=0,
        ),
        max_segment_bytes=16 * 1024 * 1024,
        max_segment_seconds=10**9,
        free_bytes_fn=lambda _p: 10**12,
    )
    spool.open()
    for i in range(5):
        spool.append(_env(i))
    sealed = spool.close()
    assert sealed is not None
    worker = AsyncOffloadWorker(root, store, auto_reclaim=True)
    worker.process_one(sealed)
    record = read_state(sealed)
    assert record is not None
    assert record.state == SegmentState.RECLAIMABLE
    assert not sealed.sealed_ndjson.exists()
    assert store.exists(
        f"external/binance_usdm/ETHUSDT/{sealed.segment_id}/events.ndjson.gz"
    )


@pytest.mark.asyncio
async def test_concurrent_producer_offloader(tmp_path: Path) -> None:
    root = tmp_path / "segments"
    store = LocalArchiveStore(tmp_path / "store")
    stop = asyncio.Event()
    worker = AsyncOffloadWorker(root, store, poll_seconds=0.05, stop_event=stop)
    task = asyncio.create_task(worker.run())
    spool = SegmentedExternalSpool(
        root,
        policy=CapacityPolicy(
            pressure_bytes=10**9,
            stop_bytes=10**9,
            global_floor_bytes=1024,
            floor_margin_bytes=0,
        ),
        max_segment_bytes=1200,
        max_segment_seconds=10**9,
        free_bytes_fn=lambda _p: 10**12,
    )
    spool.open()
    for i in range(40):
        spool.append(_env(i))
        await asyncio.sleep(0)
    spool.close()
    # Allow worker to drain.
    for _ in range(50):
        if not worker.discover_pending():
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert worker.stats.segments_verified >= 1
    assert worker.stats.segments_failed == 0
    assert worker.stats.segments_verified >= worker.stats.segments_failed


def test_backlog_includes_temp_and_failed(tmp_path: Path) -> None:
    root = tmp_path / "segments"
    (root / "segA").mkdir(parents=True)
    (root / "segA" / "events.ndjson").write_bytes(b"x" * 1000)
    (root / "segA" / "tmp_verify.bin").write_bytes(b"y" * 500)
    total = measure_local_external_bytes(root)
    assert total == 1500


def test_upload_failure_keeps_failed_no_reclaim(tmp_path: Path) -> None:
    class Broken:
        def exists(self, key: str) -> bool:
            return False

        def publish_file(self, key: str, source: Path) -> None:
            raise RuntimeError("B2 down")

        def publish_bytes(self, key: str, value: bytes) -> None:
            raise RuntimeError("B2 down")

        def download_file(self, key: str, destination: Path) -> None:
            raise RuntimeError("B2 down")

        def read_bytes(self, key: str) -> bytes:
            raise RuntimeError("B2 down")

    root = tmp_path / "segments"
    spool = SegmentedExternalSpool(
        root,
        policy=CapacityPolicy(
            pressure_bytes=10**9,
            stop_bytes=10**9,
            global_floor_bytes=1024,
            floor_margin_bytes=0,
        ),
        max_segment_bytes=16 * 1024 * 1024,
        free_bytes_fn=lambda _p: 10**12,
    )
    spool.open()
    spool.append(_env(1))
    sealed = spool.close()
    assert sealed is not None
    worker = AsyncOffloadWorker(
        root,
        Broken(),  # type: ignore[arg-type]
        auto_reclaim=True,
    )
    worker.offloader.max_upload_attempts = 2
    worker.offloader.backoff_base_seconds = 0.01
    worker.process_one(sealed)
    record = read_state(sealed)
    assert record is not None
    assert record.state == SegmentState.FAILED
    assert sealed.sealed_ndjson.exists()
    with pytest.raises(RuntimeError):
        reclaim_local_segment(sealed)


def test_capacity_stop_on_local_budget(tmp_path: Path) -> None:
    free = {"v": 10 * 1024**3}
    spool = SegmentedExternalSpool(
        tmp_path / "segments",
        policy=CapacityPolicy(
            pressure_bytes=2_000,
            stop_bytes=4_000,
            global_floor_bytes=5 * 1024**3,
            floor_margin_bytes=100,
        ),
        max_segment_bytes=1_500,
        free_bytes_fn=lambda _p: free["v"],
    )
    spool.open()
    with pytest.raises(ExternalCapacityStop):
        for i in range(200):
            spool.append(_env(i))
    assert spool.stats.last_action in {
        BacklogAction.EXTERNAL_STOP_REQUIRED.value,
        BacklogAction.OFFLOAD_PRESSURE.value,
        BacklogAction.NONE.value,
    }


def test_restart_recovery_before_offload(tmp_path: Path) -> None:
    root = tmp_path / "segments"
    spool = SegmentedExternalSpool(
        root,
        policy=CapacityPolicy(
            pressure_bytes=10**9,
            stop_bytes=10**9,
            global_floor_bytes=1024,
            floor_margin_bytes=0,
        ),
        max_segment_bytes=16 * 1024 * 1024,
        free_bytes_fn=lambda _p: 10**12,
    )
    spool.open()
    spool.append(_env(1))
    assert spool.writer._paths is not None
    active = spool.writer._paths.active_ndjson
    with active.open("ab") as handle:
        handle.write(b'{"partial":')
    spool.writer._fh.close()
    spool.writer._fh = None
    worker = AsyncOffloadWorker(root, LocalArchiveStore(tmp_path / "store"))
    report = worker.recover()
    assert any(a.get("action") == "trim_partial" for a in report["actions"])
    text = active.read_text(encoding="utf-8")
    assert '{"partial":' not in text
