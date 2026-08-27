"""Guard, drain, atomic state, and 000006 reclaim-adoption regressions."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from tests.test_external_live_offload import _env
from trading_bot.archive.store import LocalArchiveStore
from trading_bot.external_market_data.offload.capacity import CapacityPolicy
from trading_bot.external_market_data.offload.hibachi_guard import (
    DOCKER_UNHEALTHY_STOP_STREAK,
    GuardVerdict,
    HibachiGuardSnapshot,
    StopReason,
    classify_hibachi_guard,
)
from trading_bot.external_market_data.offload.lifecycle import (
    recover_root,
    remote_data_key,
    remote_evidence_key,
    remote_manifest_key,
)
from trading_bot.external_market_data.offload.segments import (
    SegmentPaths,
    SegmentState,
    read_state,
    write_state,
)
from trading_bot.external_market_data.offload.worker import AsyncOffloadWorker
from trading_bot.external_market_data.segmented_spool import SegmentedExternalSpool


def _live(**overrides: object) -> HibachiGuardSnapshot:
    payload = dict(
        process_live=True,
        postgres_live=True,
        partition_covered=True,
        capacity_safe=True,
        docker_health="healthy",
        docker_unhealthy_streak=0,
        data_progress=True,
        stale_progress_samples=0,
    )
    payload.update(overrides)
    return HibachiGuardSnapshot(**payload)  # type: ignore[arg-type]


def test_one_docker_timeout_with_id_progress_is_transient() -> None:
    verdict, reason = classify_hibachi_guard(
        _live(docker_health="unhealthy", docker_unhealthy_streak=1, data_progress=True)
    )
    assert verdict is GuardVerdict.HIBACHI_HEALTH_TRANSIENT
    assert reason is None
    timeout_verdict, timeout_reason = classify_hibachi_guard(
        _live(docker_health="timeout", docker_unhealthy_streak=1, data_progress=True)
    )
    assert timeout_verdict is GuardVerdict.HIBACHI_HEALTH_TRANSIENT
    assert timeout_reason is None


def test_sustained_docker_unhealthy_stops_external() -> None:
    verdict, reason = classify_hibachi_guard(
        _live(
            docker_health="unhealthy",
            docker_unhealthy_streak=DOCKER_UNHEALTHY_STOP_STREAK,
            data_progress=True,
        )
    )
    assert verdict is GuardVerdict.STOP
    assert reason == StopReason.DOCKER_UNHEALTHY_SUSTAINED.value


def test_stale_data_progress_stops_even_if_docker_healthy() -> None:
    verdict, reason = classify_hibachi_guard(_live(stale_progress_samples=2, data_progress=False))
    assert verdict is GuardVerdict.STOP
    assert reason == StopReason.DATA_STALE.value


def test_postgres_failure_stops_immediately() -> None:
    verdict, reason = classify_hibachi_guard(_live(postgres_live=False))
    assert verdict is GuardVerdict.STOP
    assert reason == StopReason.POSTGRES_DEAD.value


def test_partition_miss_stops_immediately() -> None:
    verdict, reason = classify_hibachi_guard(_live(partition_covered=False))
    assert verdict is GuardVerdict.STOP
    assert reason == StopReason.PARTITION_UNCOVERED.value


def test_capacity_stop_stops_immediately() -> None:
    verdict, reason = classify_hibachi_guard(_live(capacity_safe=False))
    assert verdict is GuardVerdict.STOP
    assert reason == StopReason.CAPACITY_STOP.value


def test_process_dead_stops_immediately() -> None:
    verdict, reason = classify_hibachi_guard(_live(process_live=False))
    assert verdict is GuardVerdict.STOP
    assert reason == StopReason.PROCESS_DEAD.value


def _seal_one(tmp_path: Path) -> tuple[Path, SegmentPaths]:
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
        max_segment_seconds=10**9,
        free_bytes_fn=lambda _p: 10**12,
    )
    spool.open()
    spool.append(_env(1))
    sealed = spool.close()
    assert sealed is not None
    return root, sealed


def test_verified_reclaimed_segment_is_not_failed(tmp_path: Path) -> None:
    """Previous canary 000006: remote VERIFY_OK + local reclaim must not be FAILED."""

    root, sealed = _seal_one(tmp_path)
    store = LocalArchiveStore(tmp_path / "store")
    worker = AsyncOffloadWorker(root, store, auto_reclaim=True)
    worker.process_one(sealed)
    record = read_state(sealed)
    assert record is not None
    assert record.state == SegmentState.RECLAIMABLE
    assert not sealed.sealed_ndjson.exists()
    assert store.exists(remote_data_key(sealed.segment_id))
    assert store.exists(remote_manifest_key(sealed.segment_id))
    assert store.exists(remote_evidence_key(sealed.segment_id))

    # Simulate the drain race: mark FAILED after reclaim deleted bulky files.
    record.state = SegmentState.FAILED
    record.error = "No such file or directory: events.ndjson.gz"
    write_state(sealed, record)
    report = recover_root(root)
    assert any(a.get("action") == "adopt_reclaimed" for a in report["actions"])
    adopted = read_state(sealed)
    assert adopted is not None
    assert adopted.state == SegmentState.RECLAIMABLE
    assert adopted.error is None
    again = recover_root(root)
    assert not any(a.get("action") == "adopt_reclaimed" for a in again["actions"])
    worker.process_one(sealed)
    assert worker.stats.segments_failed == 0
    assert sealed.sealed_ndjson.exists() is False


def test_drain_and_worker_do_not_double_fail_same_segment(tmp_path: Path) -> None:
    root, sealed = _seal_one(tmp_path)
    store = LocalArchiveStore(tmp_path / "store")
    worker = AsyncOffloadWorker(root, store, auto_reclaim=True)
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            worker.process_one(sealed)
        except BaseException as exc:  # noqa: BLE001 — capture for assertion
            errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    record = read_state(sealed)
    assert record is not None
    assert record.state == SegmentState.RECLAIMABLE
    assert worker.stats.segments_failed == 0


def test_state_write_survives_concurrent_reconnect_writers(tmp_path: Path) -> None:
    root, sealed = _seal_one(tmp_path)
    record = read_state(sealed)
    assert record is not None
    errors: list[BaseException] = []

    def _writer(idx: int) -> None:
        try:
            record.event_count = idx
            write_state(sealed, record)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    loaded = json.loads(sealed.state_path.read_text(encoding="utf-8"))
    SegmentState(loaded["state"])  # valid enum
    assert "segment_id" in loaded
    leftovers = list(sealed.dir.glob(".state.*.tmp"))
    assert leftovers == []


def test_recover_root_is_idempotent_after_clean_reclaim(tmp_path: Path) -> None:
    root, sealed = _seal_one(tmp_path)
    worker = AsyncOffloadWorker(root, LocalArchiveStore(tmp_path / "store"))
    worker.process_one(sealed)
    first = recover_root(root)
    second = recover_root(root)
    record = read_state(sealed)
    assert record is not None
    assert record.state == SegmentState.RECLAIMABLE
    assert first["actions"] == second["actions"] or second["actions"] == []


def test_existing_remote_object_is_not_reuploaded(tmp_path: Path) -> None:
    root, sealed = _seal_one(tmp_path)
    publishes: list[str] = []

    class CountingStore(LocalArchiveStore):
        def publish_file(self, key: str, source: Path) -> None:
            publishes.append(key)
            super().publish_file(key, source)

        def publish_bytes(self, key: str, value: bytes) -> None:
            publishes.append(key)
            super().publish_bytes(key, value)

    store = CountingStore(tmp_path / "store")
    from trading_bot.external_market_data.offload.lifecycle import SegmentOffloader

    offloader = SegmentOffloader(store)
    first = offloader.process_sealed(sealed)
    assert first.state == SegmentState.RECLAIMABLE
    first_count = len(publishes)
    assert first_count >= 2
    # Rewind to SEALED_UNVERIFIED with local gzip still present; exists() must skip publish.
    record = read_state(sealed)
    assert record is not None
    record.state = SegmentState.SEALED_UNVERIFIED
    write_state(sealed, record)
    second = offloader.process_sealed(sealed)
    assert second.state == SegmentState.RECLAIMABLE
    assert len(publishes) == first_count


def test_stale_state_tmp_is_dropped_and_ignored(tmp_path: Path) -> None:
    _root, sealed = _seal_one(tmp_path)
    stale = sealed.dir / ".state.999.1.tmp"
    stale.write_text("{partial", encoding="utf-8")
    report = recover_root(sealed.dir.parent)
    assert any(a.get("action") == "drop_stale_state_tmp" for a in report["actions"])
    assert not stale.exists()
    record = read_state(sealed)
    assert record is not None
    assert record.state == SegmentState.SEALED_UNVERIFIED


def test_failed_without_remote_or_audit_stays_failed(tmp_path: Path) -> None:
    _root, sealed = _seal_one(tmp_path)
    sealed.sealed_ndjson.unlink()
    record = read_state(sealed)
    assert record is not None
    record.state = SegmentState.FAILED
    record.error = "No such file or directory: events.ndjson"
    write_state(sealed, record)
    report = recover_root(sealed.dir.parent)
    assert not any(a.get("action") == "adopt_reclaimed" for a in report["actions"])
    again = read_state(sealed)
    assert again is not None
    assert again.state == SegmentState.FAILED
