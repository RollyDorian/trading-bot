#!/usr/bin/env python3
"""Atomic sanitized cache writer and fixed-key Zabbix reader."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Final

SCHEMA_VERSION: Final = 1
MAX_CACHE_BYTES: Final = 2048
MAX_CACHE_AGE_SECONDS: Final = 150
COLLECTION_TIMEOUT_SECONDS: Final = 45
SAFE_ERROR: Final = -1

ITEM_KEYS: Final = {
    "postgres": "postgres_health",
    "collector": "collector_health",
    "restart": "collector_restart_loop",
    "restart_count": "collector_restart_count",
    "restart_state": "collector_restart_state",
    "storage": "data_paths_writable",
    "backup": "backup_fresh",
    "disk": "disk_safe",
    "swap": "swap_safe",
    "dashboard": "dashboard_disabled",
    "ports": "ports_safe",
    "readiness": "readiness",
}
BOUNDED_STATES: Final = {
    "healthy_stable",
    "historical_restart",
    "recent_restart",
    "restart_loop",
    "unhealthy",
    "unknown",
}
STORAGE_STATES: Final = {
    "ready",
    "not_applicable",
    "required_path_missing",
    "required_path_unwritable",
    "inconsistent",
    "unknown",
}
RESTART_STATE_CODES: Final = {
    "healthy_stable": 0,
    "historical_restart": 1,
    "recent_restart": 2,
    "restart_loop": 3,
    "unhealthy": 4,
    "unknown": SAFE_ERROR,
}
SOURCE_KEYS: Final = {
    "backup_fresh",
    "collector_health",
    "collector_restart_count",
    "collector_restart_loop",
    "collector_restart_state",
    "dashboard_disabled",
    "data_paths_writable",
    "disk_safe",
    "ports_safe",
    "postgres_health",
    "readiness",
    "runtime_safe",
    "storage_state",
    "swap_safe",
}
CACHE_KEYS: Final = SOURCE_KEYS | {"generated_at", "schema_version"}


def _validate_metrics(value: object) -> dict[str, int | str]:
    if not isinstance(value, dict) or set(value) != SOURCE_KEYS:
        raise ValueError
    metrics = dict(value)
    numeric_rules = {
        "backup_fresh": {-1, 0, 1},
        "collector_health": {-1, 0, 1, 2},
        "collector_restart_loop": {-1, 0, 1},
        "dashboard_disabled": {-1, 0, 1},
        "data_paths_writable": {-1, 0, 1, 2},
        "disk_safe": {-1, 0, 1},
        "ports_safe": {-1, 0, 1},
        "postgres_health": {-1, 0, 1},
        "readiness": {0, 1},
        "runtime_safe": {-1, 0, 1},
        "swap_safe": {-1, 0, 1},
    }
    for key, allowed in numeric_rules.items():
        metric = metrics[key]
        if type(metric) is not int or metric not in allowed:
            raise ValueError
    count = metrics["collector_restart_count"]
    if type(count) is not int or count < -1 or count > 1_000_000:
        raise ValueError
    if metrics["collector_restart_state"] not in BOUNDED_STATES:
        raise ValueError
    if metrics["storage_state"] not in STORAGE_STATES:
        raise ValueError
    if metrics["runtime_safe"] != int(
        metrics["dashboard_disabled"] == 1 and metrics["ports_safe"] == 1
    ):
        raise ValueError
    expected_ready = int(
        metrics["postgres_health"] == 1
        and metrics["collector_health"] == 2
        and metrics["collector_restart_loop"] == 0
        and metrics["data_paths_writable"] in {1, 2}
        and metrics["backup_fresh"] == 1
        and metrics["disk_safe"] == 1
        and metrics["swap_safe"] == 1
        and metrics["dashboard_disabled"] == 1
        and metrics["ports_safe"] == 1
    )
    if metrics["readiness"] != expected_ready:
        raise ValueError
    return metrics


def collect(command: list[str], cache: Path, *, now: int | None = None) -> int:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=COLLECTION_TIMEOUT_SECONDS,
        )
        if len(completed.stdout.encode()) > MAX_CACHE_BYTES:
            raise ValueError
        metrics = _validate_metrics(json.loads(completed.stdout))
        if completed.returncode not in {0, 1}:
            raise ValueError
    except BaseException:
        return 1
    payload: dict[str, int | str] = {
        "generated_at": int(time.time()) if now is None else now,
        "schema_version": SCHEMA_VERSION,
        **metrics,
    }
    cache.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".metrics.", dir=cache.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, cache)
    finally:
        temporary_path.unlink(missing_ok=True)
    return 0


def _load_cache(cache: Path, *, now: int | None = None) -> dict[str, int | str]:
    metadata = cache.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError
    if stat.S_IMODE(metadata.st_mode) != 0o640 or metadata.st_uid != 0:
        raise ValueError
    if metadata.st_size <= 0 or metadata.st_size > MAX_CACHE_BYTES:
        raise ValueError
    text = cache.read_text(encoding="ascii")
    pairs = json.loads(text, object_pairs_hook=list)
    if not isinstance(pairs, list) or any(not isinstance(pair, tuple) for pair in pairs):
        raise ValueError
    keys = [pair[0] for pair in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError
    payload = dict(pairs)
    if set(payload) != CACHE_KEYS or payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError
    generated = payload["generated_at"]
    current = int(time.time()) if now is None else now
    if type(generated) is not int or generated > current + 5:
        raise ValueError
    if current - generated > MAX_CACHE_AGE_SECONDS:
        raise ValueError
    _validate_metrics({key: payload[key] for key in SOURCE_KEYS})
    return payload


def read_item(cache: Path, item: str, *, now: int | None = None) -> int:
    if item not in ITEM_KEYS:
        return SAFE_ERROR
    try:
        value = _load_cache(cache, now=now)[ITEM_KEYS[item]]
        if item == "restart_state":
            return RESTART_STATE_CODES.get(str(value), SAFE_ERROR)
        return value if type(value) is int else SAFE_ERROR
    except BaseException:
        return SAFE_ERROR


def run(argv: list[str] | None = None) -> int:
    try:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("mode", choices=("collect", "read"))
        parser.add_argument("cache", type=Path)
        parser.add_argument("item", nargs="?")
        arguments = parser.parse_args(argv)
        if arguments.mode == "collect":
            if arguments.item is not None:
                raise ValueError
            monitor = os.environ.get("HIBACHI_MONITOR_COMMAND")
            if not monitor or not Path(monitor).is_absolute():
                raise ValueError
            return collect(["python3", monitor], arguments.cache)
        if arguments.item is None:
            raise ValueError
        sys.stdout.write(f"{read_item(arguments.cache, arguments.item)}\n")
        return 0
    except BaseException:
        if argv and argv[0] == "read":
            sys.stdout.write(f"{SAFE_ERROR}\n")
            return 0
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
