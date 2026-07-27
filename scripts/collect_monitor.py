#!/usr/bin/env python3
"""Bounded host-local monitoring for the private COLLECT-only stack."""

from __future__ import annotations

import json
import math
import mmap
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from restart_state import BLOCK_STATES, PASS_STATES, observe
from storage_state import BLOCK_STATES as STORAGE_BLOCK_STATES
from storage_state import observe as observe_storage

MIN_DISK_BYTES: Final = 3 * 1024**3
MIN_AVAILABLE_MEMORY_BYTES: Final = 256 * 1024**2
MAX_SWAP_USED_BYTES: Final = 256 * 1024**2
MAX_BACKUP_AGE_SECONDS: Final = 26 * 60 * 60
MAX_SWAP_ACTIVITY_BYTES: Final = 1024**2
MAX_FULL_MEMORY_PRESSURE_AVG10: Final = 1.0
UNKNOWN: Final = -1
CRITICAL: Final = 0
HEALTHY: Final = 1
WARNING: Final = 2
COLLECTOR_HEALTHY: Final = 2
STORAGE_NOT_APPLICABLE: Final = 2

MEMORY_PRESSURE_STATES: Final = {"inactive", "sustained", "unknown"}

METRIC_KEYS: Final = tuple(sorted((
    "backup_fresh",
    "collector_health",
    "collector_restart_count",
    "collector_restart_loop",
    "collector_restart_state",
    "data_paths_writable",
    "dashboard_disabled",
    "disk_safe",
    "ports_safe",
    "postgres_health",
    "readiness",
    "runtime_safe",
    "storage_state",
    "swap_safe",
)))


@dataclass(frozen=True)
class Snapshot:
    postgres_health: str | None = None
    collector_running: bool | None = None
    collector_health: str | None = None
    collector_restarts: int | None = None
    collector_restart_state: str = "unknown"
    storage_state: str = "unknown"
    backup_age_seconds: int | None = None
    disk_free_bytes: int | None = None
    swap_used_bytes: int | None = None
    available_memory_bytes: int | None = None
    memory_pressure_state: str = "unknown"
    dashboard_disabled: bool | None = None
    ports_safe: bool | None = None


@dataclass(frozen=True)
class MemorySample:
    available_bytes: int
    swap_used_bytes: int
    swap_in_pages: int
    swap_out_pages: int
    full_pressure_avg10: float


def evaluate(snapshot: Snapshot) -> dict[str, int | str]:
    postgres = _health_metric(snapshot.postgres_health)
    collector = _collector_metric(snapshot.collector_running, snapshot.collector_health)
    if snapshot.collector_restart_state in PASS_STATES:
        restart = 0
    elif snapshot.collector_restart_state in BLOCK_STATES - {"unknown"}:
        restart = 1
    else:
        restart = UNKNOWN
    restart_count = (
        UNKNOWN if snapshot.collector_restarts is None else snapshot.collector_restarts
    )
    if snapshot.storage_state == "ready":
        data = 1
    elif snapshot.storage_state == "not_applicable":
        data = 2
    elif snapshot.storage_state in STORAGE_BLOCK_STATES - {"unknown"}:
        data = 0
    else:
        data = UNKNOWN
    backup = _threshold_metric(snapshot.backup_age_seconds, MAX_BACKUP_AGE_SECONDS)
    disk = _minimum_metric(snapshot.disk_free_bytes, MIN_DISK_BYTES)
    swap = _threshold_metric(snapshot.swap_used_bytes, MAX_SWAP_USED_BYTES)
    dashboard = _boolean_metric(snapshot.dashboard_disabled)
    ports = _boolean_metric(snapshot.ports_safe)
    runtime = (
        UNKNOWN if UNKNOWN in {dashboard, ports} else int(dashboard == 1 and ports == 1)
    )
    ready = classify_readiness(
        postgres_health=postgres,
        collector_health=collector,
        collector_restart_loop=restart,
        data_paths_writable=data,
        backup_fresh=backup,
        disk_safe=disk,
        swap_safe=swap,
        dashboard_disabled=dashboard,
        ports_safe=ports,
        available_memory_bytes=snapshot.available_memory_bytes,
        memory_pressure_state=snapshot.memory_pressure_state,
    )
    return {
        "backup_fresh": backup,
        "collector_health": collector,
        "collector_restart_count": restart_count,
        "collector_restart_loop": restart,
        "collector_restart_state": snapshot.collector_restart_state,
        "data_paths_writable": data,
        "dashboard_disabled": dashboard,
        "storage_state": snapshot.storage_state,
        "disk_safe": disk,
        "postgres_health": postgres,
        "ports_safe": ports,
        "readiness": ready,
        "runtime_safe": runtime,
        "swap_safe": swap,
    }


def classify_readiness(
    *,
    postgres_health: int,
    collector_health: int,
    collector_restart_loop: int,
    data_paths_writable: int,
    backup_fresh: int,
    disk_safe: int,
    swap_safe: int,
    dashboard_disabled: int,
    ports_safe: int,
    available_memory_bytes: int | None,
    memory_pressure_state: str,
) -> int:
    """Return the bounded aggregate state without mutating the host."""
    values = {
        postgres_health,
        collector_health,
        collector_restart_loop,
        data_paths_writable,
        backup_fresh,
        disk_safe,
        swap_safe,
        dashboard_disabled,
        ports_safe,
    }
    if (
        UNKNOWN in values
        or available_memory_bytes is None
        or available_memory_bytes < 0
        or memory_pressure_state not in MEMORY_PRESSURE_STATES - {"unknown"}
    ):
        return UNKNOWN

    hard_gates_pass = (
        postgres_health == HEALTHY
        and collector_health == COLLECTOR_HEALTHY
        and collector_restart_loop == CRITICAL
        and data_paths_writable in {HEALTHY, STORAGE_NOT_APPLICABLE}
        and backup_fresh == HEALTHY
        and disk_safe == HEALTHY
        and dashboard_disabled == HEALTHY
        and ports_safe == HEALTHY
    )
    if (
        not hard_gates_pass
        or available_memory_bytes < MIN_AVAILABLE_MEMORY_BYTES
        or memory_pressure_state == "sustained"
    ):
        return CRITICAL
    if swap_safe == HEALTHY:
        return HEALTHY
    if swap_safe == CRITICAL:
        return WARNING
    return UNKNOWN


def _boolean_metric(value: bool | None) -> int:
    return int(value) if type(value) is bool else UNKNOWN


def _health_metric(value: str | None) -> int:
    if value == "healthy":
        return HEALTHY
    if value in {"starting", "unhealthy"}:
        return CRITICAL
    return UNKNOWN


def _collector_metric(running: bool | None, health: str | None) -> int:
    if type(running) is not bool:
        return UNKNOWN
    if not running:
        return CRITICAL
    if health == "healthy":
        return COLLECTOR_HEALTHY
    if health in {"starting", "unhealthy"}:
        return HEALTHY
    return UNKNOWN


def _threshold_metric(value: int | None, threshold: int) -> int:
    if type(value) is not int or value < 0:
        return UNKNOWN
    return int(value <= threshold)


def _minimum_metric(value: int | None, minimum: int) -> int:
    if type(value) is not int or value < 0:
        return UNKNOWN
    return int(value >= minimum)


def unknown_metrics() -> dict[str, int | str]:
    metrics: dict[str, int | str] = {key: UNKNOWN for key in METRIC_KEYS}
    metrics["collector_restart_state"] = "unknown"
    metrics["storage_state"] = "unknown"
    metrics["readiness"] = UNKNOWN
    return metrics


class HostProbe:
    def __init__(self) -> None:
        self.expected_uid = _expected_owner_uid()
        self.deploy_dir = _required_path("HIBACHI_DEPLOY_DIR", directory=True)
        self.runtime_env = _required_path("HIBACHI_RUNTIME_ENV", directory=False)
        self.backup_dir = _required_path("HIBACHI_BACKUP_DIR", directory=True)
        runtime_stat = self.runtime_env.stat()
        if (
            stat.S_IMODE(runtime_stat.st_mode) != 0o600
            or runtime_stat.st_uid != self.expected_uid
        ):
            raise ValueError("invalid runtime configuration")
        self.compose = (
            "docker",
            "compose",
            "--env-file",
            str(self.runtime_env),
            "-f",
            str(self.deploy_dir / "compose.production.yaml"),
        )

    def snapshot(self) -> Snapshot:
        first_memory = self._memory_sample()
        postgres = self._service_state("postgres")
        collector = self._service_state("collector")
        restart = observe(self.compose)
        storage = observe_storage(self.compose)
        second_memory = self._memory_sample()
        return Snapshot(
            postgres_health=postgres[1],
            collector_running=collector[0],
            collector_health=collector[1],
            collector_restarts=restart.restart_count,
            collector_restart_state=restart.state,
            storage_state=storage.state,
            backup_age_seconds=self._backup_age_seconds(),
            disk_free_bytes=shutil.disk_usage(self.deploy_dir).free,
            swap_used_bytes=second_memory.swap_used_bytes,
            available_memory_bytes=second_memory.available_bytes,
            memory_pressure_state=self._memory_pressure_state(first_memory, second_memory),
            dashboard_disabled=self._dashboard_disabled(),
            ports_safe=self._ports_safe(),
        )

    def _run(self, *args: str) -> str:
        result = subprocess.run(
            args,
            cwd=self.deploy_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return result.stdout.strip()

    def _compose(self, *args: str) -> str:
        return self._run(*self.compose, *args)

    def _service_state(self, service: str) -> tuple[bool, str | None, int | None]:
        container_id = self._compose("ps", "-q", service)
        if not container_id:
            return False, None, None
        raw = self._run(
            "docker",
            "inspect",
            "--format",
            "{{.State.Running}}|"
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|"
            "{{.RestartCount}}",
            container_id,
        )
        parts = raw.split("|")
        if len(parts) != 3 or parts[0] not in {"true", "false"}:
            raise ValueError("invalid service state")
        running = parts[0] == "true"
        health_status = None if parts[1] == "none" else parts[1]
        if health_status not in {None, "starting", "healthy", "unhealthy"}:
            raise ValueError("invalid health state")
        if not parts[2].isdigit():
            raise ValueError("invalid restart state")
        restart_count = int(parts[2])
        return running, health_status, restart_count

    def _dashboard_disabled(self) -> bool:
        self._compose("config", "--quiet")
        services = set(self._compose("config", "--services").splitlines())
        if services != {"postgres", "collector"}:
            return False
        profiles = set(self._compose("config", "--profiles").splitlines())
        if not {"dashboard", "tools"} <= profiles:
            return False
        return not self._compose("--profile", "dashboard", "ps", "-q", "dashboard")

    def _ports_safe(self) -> bool:
        for name in ("postgres", "collector"):
            container_id = self._compose("ps", "-q", name)
            if not container_id:
                return False
            network_mode = self._run(
                "docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", container_id
            )
            if network_mode == "host" or self._run("docker", "port", container_id):
                return False
        return True

    def _backup_age_seconds(self) -> int | None:
        directory_stat = self.backup_dir.stat()
        backups = list(self.backup_dir.glob("hibachi-????????T??????Z-???????.dump"))
        if not backups:
            return _validated_backup_age(
                directory_stat, None, False, time.time(), self.expected_uid
            )
        latest = max(backups, key=lambda path: path.stat().st_mtime)
        backup_stat = latest.stat()
        return _validated_backup_age(
            directory_stat, backup_stat, latest.is_file(), time.time(), self.expected_uid
        )

    @staticmethod
    def _memory_sample() -> MemorySample:
        values: dict[str, int] = {}
        with Path("/proc/meminfo").open(encoding="ascii") as handle:
            for line in handle:
                key, separator, remainder = line.partition(":")
                if separator and key in {"MemAvailable", "SwapTotal", "SwapFree"}:
                    parts = remainder.split()
                    if len(parts) != 2 or parts[1] != "kB":
                        raise ValueError("invalid memory state")
                    values[key] = int(parts[0]) * 1024
        if set(values) != {"MemAvailable", "SwapTotal", "SwapFree"}:
            raise ValueError("missing memory state")
        swap_used = values["SwapTotal"] - values["SwapFree"]
        if values["MemAvailable"] < 0 or swap_used < 0:
            raise ValueError("invalid memory state")
        vmstat: dict[str, int] = {}
        with Path("/proc/vmstat").open(encoding="ascii") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) == 2 and parts[0] in {"pswpin", "pswpout"}:
                    vmstat[parts[0]] = int(parts[1])
        if set(vmstat) != {"pswpin", "pswpout"} or min(vmstat.values()) < 0:
            raise ValueError("missing memory activity")
        full_pressure = None
        with Path("/proc/pressure/memory").open(encoding="ascii") as handle:
            for line in handle:
                parts = line.split()
                if not parts or parts[0] != "full":
                    continue
                averages = dict(part.split("=", 1) for part in parts[1:] if "=" in part)
                full_pressure = float(averages["avg10"])
                break
        if full_pressure is None or not math.isfinite(full_pressure) or full_pressure < 0:
            raise ValueError("missing memory pressure")
        return MemorySample(
            available_bytes=values["MemAvailable"],
            swap_used_bytes=swap_used,
            swap_in_pages=vmstat["pswpin"],
            swap_out_pages=vmstat["pswpout"],
            full_pressure_avg10=full_pressure,
        )

    @staticmethod
    def _memory_pressure_state(first: MemorySample, second: MemorySample) -> str:
        if (
            first.available_bytes < 0
            or second.available_bytes < 0
            or first.swap_used_bytes < 0
            or second.swap_used_bytes < 0
            or second.swap_in_pages < first.swap_in_pages
            or second.swap_out_pages < first.swap_out_pages
            or first.full_pressure_avg10 < 0
            or second.full_pressure_avg10 < 0
            or not math.isfinite(first.full_pressure_avg10)
            or not math.isfinite(second.full_pressure_avg10)
        ):
            return "unknown"
        page_size = mmap.PAGESIZE
        activity_bytes = page_size * (
            second.swap_in_pages
            - first.swap_in_pages
            + second.swap_out_pages
            - first.swap_out_pages
        )
        if page_size <= 0 or activity_bytes < 0:
            return "unknown"
        if (
            second.available_bytes < MIN_AVAILABLE_MEMORY_BYTES
            or second.swap_used_bytes - first.swap_used_bytes > MAX_SWAP_ACTIVITY_BYTES
            or activity_bytes > MAX_SWAP_ACTIVITY_BYTES
            or max(first.full_pressure_avg10, second.full_pressure_avg10)
            > MAX_FULL_MEMORY_PRESSURE_AVG10
        ):
            return "sustained"
        return "inactive"


def _required_path(name: str, *, directory: bool) -> Path:
    value = os.environ.get(name)
    if not value:
        raise ValueError("missing monitoring configuration")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("invalid monitoring configuration")
    if directory and not path.is_dir():
        raise ValueError("invalid monitoring configuration")
    if not directory and not path.is_file():
        raise ValueError("invalid monitoring configuration")
    return path


def _expected_owner_uid() -> int:
    value = os.environ.get("HIBACHI_OWNER_UID")
    if value is None:
        return os.getuid()
    if os.getuid() != 0 or not value.isdigit():
        raise ValueError("invalid monitoring configuration")
    return int(value)


def _validated_backup_age(
    directory_stat: os.stat_result,
    backup_stat: os.stat_result | None,
    is_file: bool,
    now: float,
    expected_uid: int,
) -> int | None:
    if stat.S_IMODE(directory_stat.st_mode) != 0o700:
        return None
    if directory_stat.st_uid != expected_uid or backup_stat is None:
        return None
    if (
        not is_file
        or backup_stat.st_size <= 0
        or stat.S_IMODE(backup_stat.st_mode) != 0o600
        or backup_stat.st_uid != expected_uid
    ):
        return None
    return int(now - backup_stat.st_mtime)


def run() -> int:
    try:
        metrics = evaluate(HostProbe().snapshot())
    except BaseException:
        metrics = unknown_metrics()
    sys.stdout.write(json.dumps(metrics, separators=(",", ":"), sort_keys=True) + "\n")
    return 0 if metrics["readiness"] == 1 else 1


if __name__ == "__main__":
    raise SystemExit(run())
