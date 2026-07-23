import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "collect_monitor.py"
DOC = ROOT / "docs" / "monitoring.md"


def load_monitor() -> ModuleType:
    spec = importlib.util.spec_from_file_location("collect_monitor", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def monitor() -> ModuleType:
    return load_monitor()


def healthy_snapshot(monitor: ModuleType, **changes: object) -> object:
    values = {
        "postgres_health": "healthy",
        "collector_running": True,
        "collector_health": "healthy",
        "collector_restarts": 0,
        "data_paths_writable": True,
        "backup_age_seconds": monitor.MAX_BACKUP_AGE_SECONDS,
        "disk_free_bytes": monitor.MIN_DISK_BYTES,
        "swap_used_bytes": monitor.MAX_SWAP_USED_BYTES,
        "runtime_safe": True,
    }
    values.update(changes)
    return monitor.Snapshot(**values)


def test_monitor_contract_is_bounded_and_healthy(monitor: ModuleType) -> None:
    metrics = monitor.evaluate(healthy_snapshot(monitor))
    assert tuple(sorted(metrics)) == monitor.METRIC_KEYS
    assert metrics == {
        "backup_fresh": 1,
        "collector_health": 2,
        "collector_restart_loop": 0,
        "data_paths_writable": 1,
        "disk_safe": 1,
        "postgres_health": 1,
        "readiness": 1,
        "runtime_safe": 1,
        "swap_safe": 1,
    }
    assert len(json.dumps(metrics, separators=(",", ":"))) < 256


@pytest.mark.parametrize(
    ("changes", "key"),
    [
        ({"postgres_health": "unhealthy"}, "postgres_health"),
        ({"collector_health": "unhealthy"}, "collector_health"),
        ({"collector_restarts": 1}, "collector_restart_loop"),
        ({"data_paths_writable": False}, "data_paths_writable"),
        ({"backup_age_seconds": 93601}, "backup_fresh"),
        ({"disk_free_bytes": 3 * 1024**3 - 1}, "disk_safe"),
        ({"swap_used_bytes": 256 * 1024**2 + 1}, "swap_safe"),
        ({"runtime_safe": False}, "runtime_safe"),
    ],
)
def test_each_failed_gate_rejects_readiness(
    monitor: ModuleType, changes: dict[str, object], key: str
) -> None:
    metrics = monitor.evaluate(healthy_snapshot(monitor, **changes))
    assert metrics["readiness"] == 0
    assert metrics[key] != monitor.evaluate(healthy_snapshot(monitor))[key]


def test_missing_and_malformed_values_fail_closed(monitor: ModuleType) -> None:
    metrics = monitor.evaluate(monitor.Snapshot(backup_age_seconds=-1))
    assert metrics["readiness"] == 0
    assert metrics["postgres_health"] == -1
    assert metrics["collector_health"] == -1
    assert metrics["backup_fresh"] == 0


def test_run_redacts_unexpected_configuration_failure(
    monitor: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel = "postgresql://user:password@private.invalid/research"

    class FailingProbe:
        def __init__(self) -> None:
            raise RuntimeError(sentinel)

    monkeypatch.setattr(monitor, "HostProbe", FailingProbe)
    assert monitor.run() == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert sentinel not in captured.out
    assert "password" not in captured.out
    assert json.loads(captured.out)["readiness"] == 0


def test_service_state_parses_bounded_mocked_docker_state(
    monitor: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = object.__new__(monitor.HostProbe)
    monkeypatch.setattr(probe, "_compose", lambda *args: "opaque-container-id")
    monkeypatch.setattr(probe, "_run", lambda *args: "true|healthy|0")
    assert probe._service_state("collector") == (True, "healthy", 0)


def test_service_state_rejects_malformed_mocked_docker_state(
    monitor: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = object.__new__(monitor.HostProbe)
    monkeypatch.setattr(probe, "_compose", lambda *args: "opaque-container-id")
    monkeypatch.setattr(probe, "_run", lambda *args: "malformed secret-bearing state")
    with pytest.raises(ValueError, match="invalid service state"):
        probe._service_state("collector")


def test_data_path_permission_contract(monitor: ModuleType) -> None:
    writable = SimpleNamespace(st_mode=0o40700, st_uid=monitor.CONTAINER_DATA_UID)
    read_only = SimpleNamespace(st_mode=0o40500, st_uid=monitor.CONTAINER_DATA_UID)
    wrong_owner = SimpleNamespace(st_mode=0o40700, st_uid=0)
    assert monitor._mode_allows_container_write(writable, True)
    assert not monitor._mode_allows_container_write(read_only, True)
    assert not monitor._mode_allows_container_write(wrong_owner, True)
    assert not monitor._mode_allows_container_write(writable, False)


@pytest.mark.parametrize(
    ("age", "mode", "size", "expected"),
    [
        (26 * 60 * 60, 0o100600, 1, 26 * 60 * 60),
        (26 * 60 * 60 + 1, 0o100600, 1, 26 * 60 * 60 + 1),
        (-1, 0o100600, 1, -1),
        (0, 0o100644, 1, None),
        (0, 0o100600, 0, None),
    ],
)
def test_backup_metadata_normal_stale_malformed_and_boundary(
    monitor: ModuleType,
    age: int,
    mode: int,
    size: int,
    expected: int | None,
) -> None:
    directory = SimpleNamespace(st_mode=0o40700, st_uid=1000)
    backup = SimpleNamespace(st_mode=mode, st_uid=1000, st_size=size, st_mtime=1000 - age)
    assert monitor._validated_backup_age(directory, backup, True, 1000, 1000) == expected


def test_missing_backup_is_unknown(monitor: ModuleType) -> None:
    directory = SimpleNamespace(st_mode=0o40700, st_uid=1000)
    assert monitor._validated_backup_age(directory, None, False, 1000, 1000) is None


def test_script_contains_no_mutating_runtime_commands() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert "capture_output=True" in script
    assert "except BaseException" in script
    for forbidden in (
        "docker compose up",
        "docker compose down",
        "docker restart",
        "docker run",
        "pg_dump",
        "pg_restore",
        "DATABASE_URL",
        "config --format json",
    ):
        assert forbidden not in script


def test_monitoring_document_contract_is_complete() -> None:
    document = DOC.read_text(encoding="utf-8")
    assert "UserParameter=hibachi.collect.monitor" in document
    assert sum(line.startswith("| `") for line in document.splitlines()) == 9
    assert sum(f"{number}." in document for number in range(1, 9)) == 8
    assert "opens no listener" in document
    assert "no automatic remediation" in document
