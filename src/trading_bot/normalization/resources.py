import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

GIB = 1024**3
MIB = 1024**2


class ResourceProbe(Protocol):
    def disk_free_bytes(self, path: Path) -> int: ...

    def rss_bytes(self) -> int: ...


class SystemResourceProbe:
    def disk_free_bytes(self, path: Path) -> int:
        return shutil.disk_usage(path).free

    def rss_bytes(self) -> int:
        if sys.platform == "win32":
            return _windows_rss_bytes()
        statm = Path("/proc/self/statm")
        if statm.is_file():
            resident_pages = int(statm.read_text(encoding="ascii").split()[1])
            return resident_pages * os.sysconf("SC_PAGE_SIZE")
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)


def _windows_rss_bytes() -> int:
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    ctypes_dynamic: Any = ctypes
    windll = ctypes_dynamic.windll
    get_current_process = windll.kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    get_process_memory_info = windll.psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    get_process_memory_info.restype = wintypes.BOOL
    if not get_process_memory_info(
        get_current_process(),
        ctypes.byref(counters),
        counters.cb,
    ):
        raise OSError("process RSS is unavailable")
    return int(counters.WorkingSetSize)


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    disk_pause_bytes: int = 4 * GIB
    disk_stop_bytes: int = 3 * GIB
    rss_pause_bytes: int = 128 * MIB
    rss_stop_bytes: int = 160 * MIB
    estimated_output_bytes_per_raw: int = 4096

    def __post_init__(self) -> None:
        if self.disk_pause_bytes < self.disk_stop_bytes:
            raise ValueError("disk pause threshold must be at least the stop threshold")
        if self.rss_pause_bytes >= self.rss_stop_bytes:
            raise ValueError("RSS pause threshold must be below the stop threshold")
        if self.estimated_output_bytes_per_raw < 1:
            raise ValueError("estimated output size must be positive")


@dataclass(frozen=True, slots=True)
class ResourceDecision:
    state: str
    reason: str
    disk_free_bytes: int
    rss_bytes: int
    estimated_batch_bytes: int


def evaluate_resources(
    *,
    probe: ResourceProbe,
    path: Path,
    limits: ResourceLimits,
    batch_size: int,
) -> ResourceDecision:
    try:
        disk_free = probe.disk_free_bytes(path)
        rss = probe.rss_bytes()
    except (OSError, ValueError) as error:
        raise RuntimeError("normalizer resource state is unavailable") from error
    estimate = batch_size * limits.estimated_output_bytes_per_raw
    if disk_free < limits.disk_stop_bytes:
        return ResourceDecision("stop", "disk_hard_stop", disk_free, rss, estimate)
    if rss >= limits.rss_stop_bytes:
        return ResourceDecision("stop", "rss_hard_stop", disk_free, rss, estimate)
    if disk_free - estimate < limits.disk_pause_bytes:
        return ResourceDecision("pause", "disk_pause", disk_free, rss, estimate)
    if rss >= limits.rss_pause_bytes:
        return ResourceDecision("pause", "rss_pause", disk_free, rss, estimate)
    return ResourceDecision("run", "ready", disk_free, rss, estimate)
