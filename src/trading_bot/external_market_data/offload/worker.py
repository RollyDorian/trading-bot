"""Async filesystem-backed offload worker for sealed external segments."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trading_bot.external_market_data.offload.lifecycle import (
    ObjectStore,
    SegmentOffloader,
    adopt_verified_reclaimed,
    reclaim_local_segment,
    recover_root,
)
from trading_bot.external_market_data.offload.segments import (
    SegmentPaths,
    SegmentState,
    read_state,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OffloadWorkerStats:
    segments_processed: int = 0
    segments_verified: int = 0
    segments_reclaimed: int = 0
    segments_failed: int = 0
    bytes_uploaded: int = 0
    bytes_reclaimed: int = 0
    last_error: str | None = None
    gzip_bytes_total: int = 0
    offload_elapsed_seconds: float = 0.0
    verify_latencies_seconds: list[float] = field(default_factory=list)

    def offload_mib_per_hour(self, *, wall_seconds: float) -> float:
        if wall_seconds <= 0:
            return 0.0
        return (self.gzip_bytes_total / wall_seconds) * 3600.0 / (1024.0 * 1024.0)


class AsyncOffloadWorker:
    """Discover sealed segments on disk and offload them one at a time.

    Durable job state is segment ``state.json``; no unbounded in-memory queue.
    """

    def __init__(
        self,
        root: Path,
        store: ObjectStore,
        *,
        poll_seconds: float = 2.0,
        auto_reclaim: bool = True,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self.root = root
        self.store = store
        self.poll_seconds = poll_seconds
        self.auto_reclaim = auto_reclaim
        self.stop_event = stop_event or asyncio.Event()
        self.offloader = SegmentOffloader(store)
        self.stats = OffloadWorkerStats()
        self._started_monotonic = time.monotonic()
        self._busy = False
        self._shutdown = False
        self._process_lock = threading.Lock()
        self._in_flight: str | None = None

    def recover(self) -> dict[str, Any]:
        return recover_root(self.root)

    def discover_pending(self) -> list[SegmentPaths]:
        if not self.root.exists():
            return []
        pending: list[SegmentPaths] = []
        for child in sorted(p for p in self.root.iterdir() if p.is_dir()):
            paths = SegmentPaths(self.root, child.name)
            record = read_state(paths)
            if record is None:
                continue
            if record.state in {
                SegmentState.SEALED_UNVERIFIED,
                SegmentState.UPLOADING,
                SegmentState.FAILED,
                SegmentState.VERIFIED_REMOTE,
            } or (
                record.state == SegmentState.RECLAIMABLE
                and self.auto_reclaim
                and any(
                    p.exists()
                    for p in (paths.sealed_ndjson, paths.gzip_path, paths.parquet_path)
                )
            ):
                pending.append(paths)
        return pending

    def process_one(self, paths: SegmentPaths) -> None:
        with self._process_lock:
            self._in_flight = paths.segment_id
            try:
                self._process_one_locked(paths)
            finally:
                self._in_flight = None

    def _process_one_locked(self, paths: SegmentPaths) -> None:
        if adopt_verified_reclaimed(paths, self.store):
            return
        record = read_state(paths)
        if record is None:
            return
        if record.state == SegmentState.RECLAIMABLE:
            before = _dir_bytes(paths.dir)
            reclaim_local_segment(paths)
            after = _dir_bytes(paths.dir)
            self.stats.bytes_reclaimed += max(0, before - after)
            self.stats.segments_reclaimed += 1
            return
        started = time.perf_counter()
        result = self.offloader.process_sealed(paths)
        elapsed = time.perf_counter() - started
        self.stats.segments_processed += 1
        self.stats.offload_elapsed_seconds += elapsed
        self.stats.verify_latencies_seconds.append(elapsed)
        if len(self.stats.verify_latencies_seconds) > 256:
            self.stats.verify_latencies_seconds = self.stats.verify_latencies_seconds[-128:]
        if result.state == SegmentState.FAILED:
            self.stats.segments_failed += 1
            self.stats.last_error = result.error
            return
        if result.state == SegmentState.RECLAIMABLE:
            self.stats.segments_verified += 1
            gzip_path = paths.gzip_path
            # After verify, gzip may still exist until reclaim.
            if gzip_path.exists():
                self.stats.gzip_bytes_total += gzip_path.stat().st_size
                self.stats.bytes_uploaded += gzip_path.stat().st_size
            if self.auto_reclaim:
                before = _dir_bytes(paths.dir)
                reclaim_local_segment(paths)
                after = _dir_bytes(paths.dir)
                self.stats.bytes_reclaimed += max(0, before - after)
                self.stats.segments_reclaimed += 1

    async def run(self) -> None:
        self.recover()
        while not self.stop_event.is_set() and not self._shutdown:
            pending = self.discover_pending()
            if not pending:
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    continue
                break
            self._busy = True
            try:
                # Offload is sync/blocking I/O — run in a worker thread.
                await asyncio.to_thread(self.process_one, pending[0])
            except Exception as exc:  # noqa: BLE001 — keep worker alive; fail segment
                logger.error("offload_worker_error: %s", exc)
                self.stats.last_error = str(exc)[:500]
                self.stats.segments_failed += 1
            finally:
                self._busy = False
            await asyncio.sleep(0)  # yield to receive path

    async def shutdown_drain(
        self, *, deadline_seconds: float = 35.0, max_segments: int = 32
    ) -> int:
        """Single-owner drain after ingest stop. Do not run concurrently with run().

        Waits for the in-flight segment, then processes remaining pending work
        until empty or deadline. Deadline expiry preserves local artifacts and
        does not fabricate FAILED.
        """

        self._shutdown = True
        self.stop_event.set()
        deadline = time.monotonic() + deadline_seconds
        while self._busy and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self.recover()
        done = 0
        while done < max_segments and time.monotonic() < deadline:
            pending = self.discover_pending()
            if not pending:
                break
            await asyncio.to_thread(self.process_one, pending[0])
            done += 1
        return done

    def drain_remaining(self, *, max_segments: int = 64) -> int:
        """Synchronous drain for tests. Production shutdown uses shutdown_drain."""

        self._shutdown = True
        self.recover()
        done = 0
        for _ in range(max_segments):
            pending = self.discover_pending()
            if not pending:
                break
            self.process_one(pending[0])
            done += 1
        return done

    def snapshot(self) -> dict[str, Any]:
        wall = max(time.monotonic() - self._started_monotonic, 1e-9)
        lat = sorted(self.stats.verify_latencies_seconds)
        return {
            "busy": self._busy,
            "segments_processed": self.stats.segments_processed,
            "segments_verified": self.stats.segments_verified,
            "segments_reclaimed": self.stats.segments_reclaimed,
            "segments_failed": self.stats.segments_failed,
            "bytes_uploaded": self.stats.bytes_uploaded,
            "bytes_reclaimed": self.stats.bytes_reclaimed,
            "gzip_bytes_total": self.stats.gzip_bytes_total,
            "offload_mib_per_hour": self.stats.offload_mib_per_hour(wall_seconds=wall),
            "verify_latency_seconds_p50": _p50(lat),
            "last_error": self.stats.last_error,
        }


def _dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _p50(sorted_vals: list[float]) -> float | None:
    if not sorted_vals:
        return None
    return sorted_vals[len(sorted_vals) // 2]
