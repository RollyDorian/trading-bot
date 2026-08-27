"""Backlog / capacity policy for external feed local footprint."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class BacklogAction(StrEnum):
    NONE = "NONE"
    OFFLOAD_PRESSURE = "OFFLOAD_PRESSURE"
    EXTERNAL_STOP_REQUIRED = "EXTERNAL_STOP_REQUIRED"


# Measured canary ~358 MiB/h RAW ≈ 6 MiB/min.
DEFAULT_INGEST_MIB_PER_HOUR = 358.0
# Conservative external local budget (active + sealed + temp), not whole free margin.
DEFAULT_EXTERNAL_BUDGET_BYTES = 192 * 1024 * 1024
DEFAULT_PRESSURE_BYTES = 128 * 1024 * 1024
DEFAULT_STOP_BYTES = 192 * 1024 * 1024
GLOBAL_FLOOR_BYTES = 5 * 1024 * 1024 * 1024
# Leave headroom below global floor for Hibachi/WAL/system.
EXTERNAL_FLOOR_MARGIN_BYTES = 512 * 1024 * 1024  # stop before floor+margin; floor stays 5 GiB


@dataclass(frozen=True, slots=True)
class CapacityPolicy:
    external_budget_bytes: int = DEFAULT_EXTERNAL_BUDGET_BYTES
    pressure_bytes: int = DEFAULT_PRESSURE_BYTES
    stop_bytes: int = DEFAULT_STOP_BYTES
    global_floor_bytes: int = GLOBAL_FLOOR_BYTES
    floor_margin_bytes: int = EXTERNAL_FLOOR_MARGIN_BYTES

    def classify(
        self,
        *,
        local_total_bytes: int,
        filesystem_free_bytes: int,
    ) -> BacklogAction:
        # Stop before threatening the global 5 GiB floor.
        if filesystem_free_bytes <= self.global_floor_bytes + self.floor_margin_bytes:
            return BacklogAction.EXTERNAL_STOP_REQUIRED
        if local_total_bytes >= self.stop_bytes:
            return BacklogAction.EXTERNAL_STOP_REQUIRED
        if local_total_bytes >= self.pressure_bytes:
            return BacklogAction.OFFLOAD_PRESSURE
        return BacklogAction.NONE


def measure_local_external_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    return total
