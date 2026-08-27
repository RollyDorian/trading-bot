"""Capacity-aware segmented NDJSON writer for live external ingest."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from trading_bot.external_market_data.envelope import ExternalRawEnvelope
from trading_bot.external_market_data.offload.capacity import (
    BacklogAction,
    CapacityPolicy,
    measure_local_external_bytes,
)
from trading_bot.external_market_data.offload.segments import (
    ActiveSegmentWriter,
    SegmentPaths,
)
from trading_bot.external_market_data.spool import ExternalCapacityStop, filesystem_free_bytes


@dataclass(slots=True)
class SegmentedSpoolStats:
    bytes_written: int = 0
    records_written: int = 0
    segments_sealed: int = 0
    local_total_bytes: int = 0
    last_action: str = BacklogAction.NONE.value
    last_sealed_id: str | None = None


class SegmentedExternalSpool:
    """Hot-path writer: append to ACTIVE segment; seal at ~16 MiB; capacity fail-closed."""

    def __init__(
        self,
        root: Path,
        *,
        policy: CapacityPolicy | None = None,
        max_segment_bytes: int = 16 * 1024 * 1024,
        max_segment_seconds: float = 300.0,
        free_bytes_fn: Callable[[Path], int] | None = None,
    ) -> None:
        self.root = root
        self.policy = policy or CapacityPolicy()
        self.max_segment_bytes = max_segment_bytes
        self.free_bytes_fn = free_bytes_fn or filesystem_free_bytes
        self.writer = ActiveSegmentWriter(
            root,
            max_bytes=max_segment_bytes,
            max_seconds=max_segment_seconds,
        )
        self.stats = SegmentedSpoolStats()
        self._opened = False

    def open(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._assert_capacity(upcoming_bytes=0, opening_new_segment=True)
        self._opened = True
        self.stats.local_total_bytes = measure_local_external_bytes(self.root)

    def close(self) -> SegmentPaths | None:
        if not self._opened:
            return None
        sealed = self.writer.close()
        if sealed is not None:
            self.stats.segments_sealed += 1
            self.stats.last_sealed_id = sealed.segment_id
        self._opened = False
        self.stats.local_total_bytes = measure_local_external_bytes(self.root)
        return sealed

    def _assert_capacity(self, *, upcoming_bytes: int, opening_new_segment: bool) -> None:
        free = self.free_bytes_fn(self.root)
        # Full spool walk only when opening a segment (reclaim may have shrunk disk).
        local = (
            measure_local_external_bytes(self.root)
            if opening_new_segment
            else self.stats.local_total_bytes
        )
        projected_local = local + upcoming_bytes
        # If opening a new ACTIVE segment, reserve full segment budget for stop decision.
        reserve = self.max_segment_bytes if opening_new_segment else upcoming_bytes
        action = self.policy.classify(
            local_total_bytes=projected_local,
            filesystem_free_bytes=free,
        )
        self.stats.last_action = action.value
        self.stats.local_total_bytes = local
        if action == BacklogAction.EXTERNAL_STOP_REQUIRED:
            raise ExternalCapacityStop(
                f"EXTERNAL_STOP_REQUIRED local={local} free={free} "
                f"policy_stop={self.policy.stop_bytes}"
            )
        # Stop before writing a segment that would push free under floor+margin.
        if free - reserve <= self.policy.global_floor_bytes + self.policy.floor_margin_bytes:
            raise ExternalCapacityStop(
                f"filesystem reserve would breach floor+margin "
                f"(free={free}, reserve={reserve}, floor={self.policy.global_floor_bytes}, "
                f"margin={self.policy.floor_margin_bytes})"
            )
        if (
            opening_new_segment
            and local + self.max_segment_bytes >= self.policy.stop_bytes
        ):
            # Avoid starting a new ACTIVE if a full segment could fill the stop budget.
            raise ExternalCapacityStop(
                f"next ACTIVE segment would reach stop budget "
                f"(local={local}, segment={self.max_segment_bytes})"
            )

    def append(self, envelope: ExternalRawEnvelope) -> SegmentPaths | None:
        if not self._opened:
            raise RuntimeError("segmented spool not open")
        line = (envelope.to_ndjson_line() + "\n").encode("utf-8")
        opening_new = not self.writer.has_active
        self._assert_capacity(upcoming_bytes=len(line), opening_new_segment=opening_new)
        sealed = self.writer.append_line(line, connection_id=envelope.connection_id)
        self.stats.bytes_written += len(line)
        self.stats.records_written += 1
        # Incremental local total: a full rglob/stat of the spool at bookTicker
        # rate (~250/s) starved the 1-vCPU host during the 18:33:53Z healthcheck.
        self.stats.local_total_bytes += len(line)
        if sealed is not None:
            self.stats.segments_sealed += 1
            self.stats.last_sealed_id = sealed.segment_id
            # Reclaim in the worker can drop files; resync after each seal.
            self.stats.local_total_bytes = measure_local_external_bytes(self.root)
        action = self.policy.classify(
            local_total_bytes=self.stats.local_total_bytes,
            filesystem_free_bytes=self.free_bytes_fn(self.root),
        )
        self.stats.last_action = action.value
        if action == BacklogAction.EXTERNAL_STOP_REQUIRED:
            raise ExternalCapacityStop(
                f"EXTERNAL_STOP_REQUIRED after append local={self.stats.local_total_bytes}"
            )
        return sealed
