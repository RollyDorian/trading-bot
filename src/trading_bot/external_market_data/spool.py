"""Bounded append-only NDJSON spool for external RAW (no circular overwrite)."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trading_bot.external_market_data.envelope import ExternalRawEnvelope


class ExternalCapacityStop(RuntimeError):
    """Fail-closed stop: hard cap or filesystem floor breached."""


@dataclass(frozen=True, slots=True)
class SpoolLimits:
    hard_cap_bytes: int
    filesystem_floor_bytes: int  # must keep free >= floor (default 5 GiB)


@dataclass(slots=True)
class SpoolStats:
    bytes_written: int = 0
    records_written: int = 0
    path: str = ""


def filesystem_free_bytes(path: Path) -> int:
    """Best-effort free bytes for path's filesystem."""

    target = path if path.exists() else path.parent
    if hasattr(os, "statvfs"):
        usage = os.statvfs(target)
        return int(usage.f_bavail * usage.f_frsize)
    if sys.platform == "win32":
        import ctypes

        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        total_free = ctypes.c_ulonglong(0)
        root = os.path.splitdrive(str(target.resolve()))[0] + "\\"
        ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(root),
            ctypes.byref(free),
            ctypes.byref(total),
            ctypes.byref(total_free),
        )
        if not ok:
            raise OSError(f"GetDiskFreeSpaceExW failed for {root}")
        return int(free.value)
    raise OSError(f"filesystem free bytes unavailable on platform {sys.platform}")


class BoundedNdjsonSpool:
    """Single append file. Cap hit => EXTERNAL_CAPACITY_STOP (no rotation delete)."""

    def __init__(
        self,
        directory: Path,
        *,
        limits: SpoolLimits,
        filename: str = "external_raw_canary.ndjson",
        free_bytes_fn: Callable[[Path], int] | None = None,
    ) -> None:
        self.directory = directory
        self.limits = limits
        self.path = directory / filename
        self.stats = SpoolStats(path=str(self.path))
        self._free_bytes_fn = free_bytes_fn or filesystem_free_bytes
        self._fh: Any | None = None

    def open(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            mode = self.directory.stat().st_mode & 0o777
            if mode & 0o002:
                raise PermissionError(
                    f"spool directory must not be world-writable: {self.directory}"
                )
        self._preflight_capacity(upcoming_bytes=0)
        self._fh = self.path.open("a", encoding="utf-8", newline="\n")
        self.stats.bytes_written = self.path.stat().st_size if self.path.exists() else 0

    def close(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None

    def _preflight_capacity(self, *, upcoming_bytes: int) -> None:
        free = self._free_bytes_fn(self.directory)
        if free - upcoming_bytes < self.limits.filesystem_floor_bytes:
            raise ExternalCapacityStop(
                f"filesystem free {free} would breach floor "
                f"{self.limits.filesystem_floor_bytes}"
            )
        current = self.path.stat().st_size if self.path.exists() else 0
        projected = max(self.stats.bytes_written, current) + upcoming_bytes
        if projected > self.limits.hard_cap_bytes:
            raise ExternalCapacityStop(
                f"spool hard cap {self.limits.hard_cap_bytes} would be exceeded "
                f"(projected={projected})"
            )

    def append(self, envelope: ExternalRawEnvelope) -> None:
        if self._fh is None:
            raise RuntimeError("spool not open")
        line = envelope.to_ndjson_line() + "\n"
        data = line.encode("utf-8")
        self._preflight_capacity(upcoming_bytes=len(data))
        self._fh.write(line)
        self._fh.flush()
        self.stats.bytes_written += len(data)
        self.stats.records_written += 1

    def read_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return self.path.read_text(encoding="utf-8").splitlines()
