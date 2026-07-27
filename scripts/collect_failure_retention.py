#!/usr/bin/env python3
"""Retain bounded, redacted collector exit evidence from Docker die events."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Final

STATE_DIR: Final = Path("/var/lib/hibachi-collect-monitor/failures")
LATEST: Final = STATE_DIR / "latest.json"
MAX_RECORDS: Final = 20
MAX_RECORD_BYTES: Final = 2048
MAX_STDERR_LINES: Final = 12
MAX_LINE_CHARS: Final = 160
MAX_DOCKER_LOG_LINES: Final = 40
EVENT_TIMEOUT_SECONDS: Final = 30
SAFE_STAGES: Final = {"startup", "database", "stream", "storage", "unknown"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_SECRET = re.compile(
    r"(?i)(password|passwd|token|secret|api[_-]?key|authorization)\s*([=:])\s*[^\s,;]+"
)
_URL = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://)[^\s]+")
_PATH = re.compile(r"(?:[A-Za-z]:\\|/)[^\s:]+")
_ADDRESS = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[a-z0-9][a-z0-9.-]*\.(?:com|net|org|io|dev|local|internal)\b",
    re.I,
)
_ERROR_CLASS = re.compile(r"\b([A-Z][A-Za-z0-9_]{0,79}(?:Error|Exception))\b")


def _run(*args: str) -> str:
    completed = subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=EVENT_TIMEOUT_SECONDS
    )
    return completed.stdout


def sanitize_line(value: str) -> str | None:
    if value.startswith("  File "):
        return None
    line = _CONTROL.sub(" ", value).strip()
    if not line or line.startswith("Traceback"):
        return None
    line = _SECRET.sub(r"\1\2[REDACTED]", line)
    line = _URL.sub("[REDACTED]", line)
    line = _PATH.sub("[REDACTED]", line)
    line = _ADDRESS.sub("[REDACTED]", line)
    return line[:MAX_LINE_CHARS]


def _stage(lines: list[str]) -> str:
    text = " ".join(lines).lower()
    if any(word in text for word in ("startup", "configuration", "config")):
        return "startup"
    if any(word in text for word in ("postgres", "database", "sqlalchemy")):
        return "database"
    if any(word in text for word in ("websocket", "stream", "subscribe")):
        return "stream"
    if any(word in text for word in ("storage", "write failure", "disk")):
        return "storage"
    return "unknown"


def _error_class(lines: list[str]) -> str:
    for line in lines:
        match = _ERROR_CLASS.search(line)
        if match:
            return match.group(1)
    return "unknown"


def _revision(deploy_dir: Path) -> str:
    try:
        value = _run("git", "-C", str(deploy_dir), "rev-parse", "--verify", "HEAD").strip()
    except BaseException:
        return "unknown"
    return value[:40] if re.fullmatch(r"[0-9a-f]{40}", value) else "unknown"


def _metadata(container_id: str) -> tuple[int, int, int]:
    raw = _run(
        "docker",
        "inspect",
        "--format",
        "{{.State.ExitCode}}|{{.RestartCount}}|{{.State.OOMKilled}}",
        container_id,
    ).strip()
    parts = raw.split("|")
    if (
        len(parts) != 3
        or not parts[0].isdigit()
        or not parts[1].isdigit()
        or parts[2] not in {"true", "false"}
    ):
        raise ValueError("invalid exit metadata")
    exit_code, restart_count = int(parts[0]), int(parts[1])
    if exit_code > 255 or restart_count > 1_000_000:
        raise ValueError("unbounded exit metadata")
    return exit_code, restart_count, int(parts[2] == "true")


def _stderr(container_id: str) -> list[str]:
    try:
        raw = _run("docker", "logs", "--stderr", "--tail", str(MAX_DOCKER_LOG_LINES), container_id)
    except BaseException:
        return []
    return [clean for line in raw.splitlines() if (clean := sanitize_line(line))][:MAX_STDERR_LINES]


def build_record(
    container_id: str, deploy_dir: Path, *, now: dt.datetime | None = None
) -> dict[str, object]:
    exit_code, restart_count, oom = _metadata(container_id)
    lines = _stderr(container_id)
    timestamp = (
        (now or dt.datetime.now(dt.UTC)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    record: dict[str, object] = {
        "timestamp": timestamp,
        "exit_code": exit_code,
        "restart_count": restart_count,
        "failure_stage": _stage(lines),
        "error_class": _error_class(lines),
        "stderr_lines": lines,
        "oom_killed": oom,
        "revision": _revision(deploy_dir),
    }
    encoded = json.dumps(record, separators=(",", ":"), sort_keys=True).encode("ascii")
    if len(encoded) > MAX_RECORD_BYTES:
        raise ValueError("oversized sanitized record")
    return record


def _validate_record(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "timestamp",
        "exit_code",
        "restart_count",
        "failure_stage",
        "error_class",
        "stderr_lines",
        "oom_killed",
        "revision",
    }:
        raise ValueError
    if not isinstance(value["timestamp"], str) or not re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value["timestamp"]
    ):
        raise ValueError
    if type(value["exit_code"]) is not int or not 0 <= value["exit_code"] <= 255:
        raise ValueError
    if type(value["restart_count"]) is not int or not 0 <= value["restart_count"] <= 1_000_000:
        raise ValueError
    if value["failure_stage"] not in SAFE_STAGES or not isinstance(value["error_class"], str):
        raise ValueError
    if not re.fullmatch(
        r"unknown|[A-Z][A-Za-z0-9_]{0,79}(?:Error|Exception)", value["error_class"]
    ):
        raise ValueError
    if type(value["oom_killed"]) is not int or value["oom_killed"] not in {0, 1}:
        raise ValueError
    if (
        not isinstance(value["revision"], str)
        or value["revision"] != "unknown"
        and not re.fullmatch(r"[0-9a-f]{40}", value["revision"])
    ):
        raise ValueError
    lines = value["stderr_lines"]
    if (
        not isinstance(lines, list)
        or len(lines) > MAX_STDERR_LINES
        or any(not isinstance(line, str) or len(line) > MAX_LINE_CHARS for line in lines)
    ):
        raise ValueError
    return value


def retain(record: dict[str, object], directory: Path = STATE_DIR) -> None:
    _validate_record(record)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = directory.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != 0
    ):
        raise ValueError("unsafe retention directory")
    record_bytes = json.dumps(record, separators=(",", ":"), sort_keys=True).encode("ascii")
    stamp = str(record["timestamp"]).replace(":", "").replace("-", "")
    target = directory / f"failure-{stamp}.json"
    _atomic_write(target, record_bytes)
    _atomic_write(directory / "latest.json", record_bytes)
    records = sorted(
        path
        for path in directory.glob("failure-*.json")
        if re.fullmatch(r"failure-\d{8}T\d{6}Z\.json", path.name)
    )
    for stale in records[:-MAX_RECORDS]:
        if stale.is_file() and not stale.is_symlink() and stale.lstat().st_uid == 0:
            stale.unlink()


def _atomic_write(target: Path, payload: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".failure.", dir=target.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)


def listen(deploy_dir: Path) -> int:
    process = subprocess.Popen(
        [
            "docker",
            "events",
            "--filter",
            "label=com.docker.compose.service=collector",
            "--filter",
            "event=die",
            "--format",
            "{{.ID}}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert process.stdout is not None
    for line in process.stdout:
        container_id = line.strip()
        if re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            with suppress(BaseException):
                retain(build_record(container_id, deploy_dir))
    return 1


def run() -> int:
    try:
        value = os.environ.get("HIBACHI_DEPLOY_DIR")
        if not value or not Path(value).is_absolute() or not Path(value).is_dir():
            return 1
        return listen(Path(value))
    except BaseException:
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
