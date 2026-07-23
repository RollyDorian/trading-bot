import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "restart_state.py"


def load_restart_state() -> ModuleType:
    spec = importlib.util.spec_from_file_location("restart_state_test", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def restart() -> ModuleType:
    return load_restart_state()


def sample(
    restart: ModuleType,
    *,
    now: datetime,
    count: int = 0,
    age: int = 3600,
    running: bool = True,
    health: str = "healthy",
    opaque_id: str = "opaque",
) -> object:
    return restart.DockerSample(
        opaque_id=opaque_id,
        running=running,
        health=health,
        restart_count=count,
        started_at=now - timedelta(seconds=age),
    )


def classify(restart: ModuleType, first: object, second: object, now: datetime) -> str:
    return restart.classify(first, second, now=now).state


def test_healthy_zero_restarts_is_stable(restart: ModuleType) -> None:
    now = datetime(2026, 7, 23, 12, tzinfo=UTC)
    observed = sample(restart, now=now)
    assert classify(restart, observed, observed, now) == "healthy_stable"


def test_old_static_restart_count_is_history(restart: ModuleType) -> None:
    now = datetime(2026, 7, 23, 12, tzinfo=UTC)
    observed = sample(restart, now=now, count=9, age=1801)
    assessment = restart.classify(observed, observed, now=now)
    assert assessment.state == "historical_restart"
    assert assessment.restart_count == 9


def test_count_increase_or_container_change_is_loop(restart: ModuleType) -> None:
    now = datetime(2026, 7, 23, 12, tzinfo=UTC)
    first = sample(restart, now=now, count=1)
    increased = sample(restart, now=now, count=2)
    replaced = sample(restart, now=now, count=1, opaque_id="different")
    assert classify(restart, first, increased, now) == "restart_loop"
    assert classify(restart, first, replaced, now) == "restart_loop"


@pytest.mark.parametrize(
    ("running", "health"),
    [(False, "healthy"), (True, "unhealthy"), (True, "starting")],
)
def test_unhealthy_or_not_running_blocks(
    restart: ModuleType, running: bool, health: str
) -> None:
    now = datetime(2026, 7, 23, 12, tzinfo=UTC)
    observed = sample(restart, now=now, running=running, health=health)
    assert classify(restart, observed, observed, now) == "unhealthy"


@pytest.mark.parametrize(
    ("count", "age", "state"),
    [
        (1, 300, "recent_restart"),
        (1, 301, "historical_restart"),
        (3, 1800, "restart_loop"),
        (3, 1801, "historical_restart"),
    ],
)
def test_recent_and_repeated_restart_boundaries(
    restart: ModuleType, count: int, age: int, state: str
) -> None:
    now = datetime(2026, 7, 23, 12, tzinfo=UTC)
    observed = sample(restart, now=now, count=count, age=age)
    assert classify(restart, observed, observed, now) == state


def test_malformed_or_inconsistent_state_is_unknown(restart: ModuleType) -> None:
    now = datetime(2026, 7, 23, 12, tzinfo=UTC)
    valid = sample(restart, now=now, count=2)
    decreased = sample(restart, now=now, count=1)
    naive = restart.DockerSample("opaque", True, "healthy", 1, datetime(2026, 7, 23))
    assert classify(restart, valid, decreased, now) == "unknown"
    assert classify(restart, naive, naive, now) == "unknown"


def test_unavailable_docker_is_unknown_and_observation_is_bounded(
    restart: ModuleType,
) -> None:
    sleeps: list[float] = []

    def unavailable(command: object) -> str:
        del command
        raise RuntimeError("credential-bearing internal failure")

    assessment = restart.observe(
        ("docker", "compose"),
        runner=unavailable,
        sleeper=sleeps.append,
    )
    assert assessment.state == "unknown"
    assert sleeps == []
    assert restart.MIN_OBSERVATION_SECONDS <= restart.DEFAULT_OBSERVATION_SECONDS
    assert restart.DEFAULT_OBSERVATION_SECONDS <= restart.MAX_OBSERVATION_SECONDS


@pytest.mark.parametrize(
    "outputs",
    [
        [""],
        ["opaque", "malformed-state"],
        ["opaque", "true|healthy|not-a-count|2026-07-23T12:00:00Z"],
        ["opaque", "true|healthy|0|not-a-timestamp"],
    ],
)
def test_absent_or_unparseable_docker_state_is_unknown(
    restart: ModuleType, outputs: list[str]
) -> None:
    remaining = iter(outputs)

    def runner(command: object) -> str:
        del command
        return next(remaining)

    assessment = restart.observe(
        ("docker", "compose"),
        observation_seconds=2,
        runner=runner,
        sleeper=lambda seconds: None,
    )
    assert assessment.state == "unknown"


def test_public_output_is_bounded_and_redacted(
    restart: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "postgresql://user:password@private.invalid/research"

    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError(sentinel)

    monkeypatch.setattr(restart, "_required_path", fail)
    assert restart.run() == 1
    captured = capsys.readouterr()
    assert captured.out == "unknown -1\n"
    assert captured.err == ""
    assert sentinel not in captured.out
