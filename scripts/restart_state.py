#!/usr/bin/env python3
"""Bounded, secret-safe collector restart-state classification."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Never

DEFAULT_OBSERVATION_SECONDS: Final = 5
MIN_OBSERVATION_SECONDS: Final = 2
MAX_OBSERVATION_SECONDS: Final = 30
RECENT_RESTART_SECONDS: Final = 300
REPEATED_RESTART_SECONDS: Final = 1800
REPEATED_RESTART_THRESHOLD: Final = 3
PROCESS_TIMEOUT_SECONDS: Final = 10

PASS_STATES: Final = {"healthy_stable", "historical_restart"}
BLOCK_STATES: Final = {"recent_restart", "restart_loop", "unhealthy", "unknown"}


@dataclass(frozen=True)
class DockerSample:
    opaque_id: str
    running: bool
    health: str
    restart_count: int
    started_at: datetime


@dataclass(frozen=True)
class RestartAssessment:
    state: str
    restart_count: int | None


def classify(
    first: DockerSample,
    second: DockerSample,
    *,
    now: datetime,
) -> RestartAssessment:
    if now.tzinfo is None:
        return RestartAssessment("unknown", None)
    if not _valid_sample(first) or not _valid_sample(second):
        return RestartAssessment("unknown", None)
    if not first.running or first.health != "healthy":
        return RestartAssessment("unhealthy", first.restart_count)
    if not second.running or second.health != "healthy":
        return RestartAssessment("unhealthy", second.restart_count)
    if second.restart_count < first.restart_count:
        return RestartAssessment("unknown", second.restart_count)
    if first.opaque_id != second.opaque_id or second.restart_count > first.restart_count:
        return RestartAssessment("restart_loop", second.restart_count)
    if second.started_at != first.started_at:
        return RestartAssessment("unknown", second.restart_count)
    age_seconds = (now.astimezone(UTC) - second.started_at.astimezone(UTC)).total_seconds()
    if age_seconds < 0:
        return RestartAssessment("unknown", second.restart_count)
    if (
        second.restart_count >= REPEATED_RESTART_THRESHOLD
        and age_seconds <= REPEATED_RESTART_SECONDS
    ):
        return RestartAssessment("restart_loop", second.restart_count)
    if second.restart_count > 0 and age_seconds <= RECENT_RESTART_SECONDS:
        return RestartAssessment("recent_restart", second.restart_count)
    if second.restart_count > 0:
        return RestartAssessment("historical_restart", second.restart_count)
    return RestartAssessment("healthy_stable", 0)


def observe(
    compose: Sequence[str],
    *,
    observation_seconds: int = DEFAULT_OBSERVATION_SECONDS,
    runner: Callable[[Sequence[str]], str] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RestartAssessment:
    if not MIN_OBSERVATION_SECONDS <= observation_seconds <= MAX_OBSERVATION_SECONDS:
        return RestartAssessment("unknown", None)
    command_runner = runner or _run
    try:
        first = _sample(compose, command_runner)
        sleeper(observation_seconds)
        second = _sample(compose, command_runner)
        return classify(first, second, now=now())
    except BaseException:
        return RestartAssessment("unknown", None)


def _sample(
    compose: Sequence[str],
    runner: Callable[[Sequence[str]], str],
) -> DockerSample:
    container_id = runner((*compose, "ps", "-q", "collector"))
    if not container_id or any(character.isspace() for character in container_id):
        raise ValueError
    raw = runner(
        (
            "docker",
            "inspect",
            "--format",
            "{{.State.Running}}|"
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|"
            "{{.RestartCount}}|{{.State.StartedAt}}",
            container_id,
        )
    )
    parts = raw.split("|")
    if len(parts) != 4 or parts[0] not in {"true", "false"} or not parts[2].isdigit():
        raise ValueError
    started_at = datetime.fromisoformat(parts[3].replace("Z", "+00:00"))
    return DockerSample(
        opaque_id=container_id,
        running=parts[0] == "true",
        health=parts[1],
        restart_count=int(parts[2]),
        started_at=started_at,
    )


def _valid_sample(sample: DockerSample) -> bool:
    return (
        bool(sample.opaque_id)
        and sample.health in {"healthy", "unhealthy", "starting", "none"}
        and sample.restart_count >= 0
        and sample.started_at.tzinfo is not None
    )


def _run(command: Sequence[str]) -> str:
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=PROCESS_TIMEOUT_SECONDS,
    )
    return result.stdout.strip()


def _required_path(name: str, *, directory: bool) -> Path:
    value = os.environ.get(name)
    if not value:
        raise ValueError
    path = Path(value)
    if not path.is_absolute():
        raise ValueError
    if directory and not path.is_dir():
        raise ValueError
    if not directory and not path.is_file():
        raise ValueError
    return path


def _observation_seconds() -> int:
    value = os.environ.get(
        "HIBACHI_RESTART_OBSERVATION_SECONDS",
        str(DEFAULT_OBSERVATION_SECONDS),
    )
    if not value.isdigit():
        raise ValueError
    seconds = int(value)
    if not MIN_OBSERVATION_SECONDS <= seconds <= MAX_OBSERVATION_SECONDS:
        raise ValueError
    return seconds


def run() -> int:
    try:
        deploy_dir = _required_path("HIBACHI_DEPLOY_DIR", directory=True)
        runtime_env = _required_path("HIBACHI_RUNTIME_ENV", directory=False)
        runtime_stat = runtime_env.stat()
        if stat.S_IMODE(runtime_stat.st_mode) != 0o600 or runtime_stat.st_uid != os.getuid():
            raise ValueError
        assessment = observe(
            (
                "docker",
                "compose",
                "--env-file",
                str(runtime_env),
                "-f",
                str(deploy_dir / "compose.production.yaml"),
            ),
            observation_seconds=_observation_seconds(),
        )
    except BaseException:
        assessment = RestartAssessment("unknown", None)
    count = assessment.restart_count if assessment.restart_count is not None else -1
    sys.stdout.write(f"{assessment.state} {count}\n")
    return 0 if assessment.state in PASS_STATES else 1


def main() -> Never:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
