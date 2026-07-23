#!/usr/bin/env python3
"""Bounded host-local monitoring for the private COLLECT-only stack."""

from __future__ import annotations

import json
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
MAX_SWAP_USED_BYTES: Final = 256 * 1024**2
MAX_BACKUP_AGE_SECONDS: Final = 26 * 60 * 60
UNKNOWN: Final = -1

METRIC_KEYS: Final = (
    "backup_fresh",
    "collector_health",
    "collector_restart_count",
    "collector_restart_loop",
    "collector_restart_state",
    "data_paths_writable",
    "disk_safe",
    "postgres_health",
    "readiness",
    "runtime_safe",
    "storage_state",
    "swap_safe",
)


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
    runtime_safe: bool | None = None


def evaluate(snapshot: Snapshot) -> dict[str, int | str]:
    postgres = UNKNOWN if snapshot.postgres_health is None else int(
        snapshot.postgres_health == "healthy"
    )
    if snapshot.collector_running is None:
        collector = UNKNOWN
    elif not snapshot.collector_running:
        collector = 0
    elif snapshot.collector_health == "healthy":
        collector = 2
    else:
        collector = 1
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
    backup = (
        UNKNOWN
        if snapshot.backup_age_seconds is None
        else int(0 <= snapshot.backup_age_seconds <= MAX_BACKUP_AGE_SECONDS)
    )
    disk = (
        UNKNOWN
        if snapshot.disk_free_bytes is None
        else int(snapshot.disk_free_bytes >= MIN_DISK_BYTES)
    )
    swap = (
        UNKNOWN
        if snapshot.swap_used_bytes is None
        else int(snapshot.swap_used_bytes <= MAX_SWAP_USED_BYTES)
    )
    runtime = _boolean_metric(snapshot.runtime_safe)
    ready = int(
        postgres == 1
        and collector == 2
        and restart == 0
        and data in {1, 2}
        and backup == 1
        and disk == 1
        and swap == 1
        and runtime == 1
    )
    return {
        "backup_fresh": backup,
        "collector_health": collector,
        "collector_restart_count": restart_count,
        "collector_restart_loop": restart,
        "collector_restart_state": snapshot.collector_restart_state,
        "data_paths_writable": data,
        "storage_state": snapshot.storage_state,
        "disk_safe": disk,
        "postgres_health": postgres,
        "readiness": ready,
        "runtime_safe": runtime,
        "swap_safe": swap,
    }


def _boolean_metric(value: bool | None) -> int:
    return UNKNOWN if value is None else int(value)


def unknown_metrics() -> dict[str, int | str]:
    metrics: dict[str, int | str] = {key: UNKNOWN for key in METRIC_KEYS}
    metrics["collector_restart_state"] = "unknown"
    metrics["storage_state"] = "unknown"
    metrics["readiness"] = 0
    return metrics


class HostProbe:
    def __init__(self) -> None:
        self.deploy_dir = _required_path("HIBACHI_DEPLOY_DIR", directory=True)
        self.runtime_env = _required_path("HIBACHI_RUNTIME_ENV", directory=False)
        self.backup_dir = _required_path("HIBACHI_BACKUP_DIR", directory=True)
        runtime_stat = self.runtime_env.stat()
        if stat.S_IMODE(runtime_stat.st_mode) != 0o600 or runtime_stat.st_uid != os.getuid():
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
        postgres = self._service_state("postgres")
        collector = self._service_state("collector")
        restart = observe(self.compose)
        storage = observe_storage(self.compose)
        return Snapshot(
            postgres_health=postgres[1],
            collector_running=collector[0],
            collector_health=collector[1],
            collector_restarts=restart.restart_count,
            collector_restart_state=restart.state,
            storage_state=storage.state,
            backup_age_seconds=self._backup_age_seconds(),
            disk_free_bytes=shutil.disk_usage(self.deploy_dir).free,
            swap_used_bytes=self._swap_used_bytes(),
            runtime_safe=self._runtime_safe(),
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

    def _runtime_safe(self) -> bool:
        self._compose("config", "--quiet")
        services = set(self._compose("config", "--services").splitlines())
        if services != {"postgres", "collector"}:
            return False
        profiles = set(self._compose("config", "--profiles").splitlines())
        if not {"dashboard", "tools"} <= profiles:
            return False
        if self._compose("--profile", "dashboard", "ps", "-q", "dashboard"):
            return False
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
                directory_stat, None, False, time.time(), os.getuid()
            )
        latest = max(backups, key=lambda path: path.stat().st_mtime)
        backup_stat = latest.stat()
        return _validated_backup_age(
            directory_stat, backup_stat, latest.is_file(), time.time(), os.getuid()
        )

    @staticmethod
    def _swap_used_bytes() -> int:
        values: dict[str, int] = {}
        with Path("/proc/meminfo").open(encoding="ascii") as handle:
            for line in handle:
                key, separator, remainder = line.partition(":")
                if separator and key in {"SwapTotal", "SwapFree"}:
                    parts = remainder.split()
                    if len(parts) != 2 or parts[1] != "kB":
                        raise ValueError("invalid memory state")
                    values[key] = int(parts[0]) * 1024
        if set(values) != {"SwapTotal", "SwapFree"}:
            raise ValueError("missing memory state")
        used = values["SwapTotal"] - values["SwapFree"]
        if used < 0:
            raise ValueError("invalid memory state")
        return used


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
