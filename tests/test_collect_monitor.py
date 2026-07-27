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
    sys.path.insert(0, str(SCRIPT.parent))
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
        "collector_restart_state": "healthy_stable",
        "storage_state": "ready",
        "backup_age_seconds": monitor.MAX_BACKUP_AGE_SECONDS,
        "disk_free_bytes": monitor.MIN_DISK_BYTES,
        "swap_used_bytes": monitor.MAX_SWAP_USED_BYTES,
        "available_memory_bytes": monitor.MIN_AVAILABLE_MEMORY_BYTES,
        "memory_pressure_state": "inactive",
        "dashboard_disabled": True,
        "ports_safe": True,
    }
    values.update(changes)
    return monitor.Snapshot(**values)


def test_monitor_contract_is_bounded_and_healthy(monitor: ModuleType) -> None:
    metrics = monitor.evaluate(healthy_snapshot(monitor))
    assert tuple(sorted(metrics)) == monitor.METRIC_KEYS
    assert metrics == {
        "backup_fresh": 1,
        "collector_health": 2,
        "collector_restart_count": 0,
        "collector_restart_loop": 0,
        "collector_restart_state": "healthy_stable",
        "data_paths_writable": 1,
        "dashboard_disabled": 1,
        "storage_state": "ready",
        "disk_safe": 1,
        "postgres_health": 1,
        "ports_safe": 1,
        "readiness": 1,
        "runtime_safe": 1,
        "swap_safe": 1,
    }
    assert len(json.dumps(metrics, separators=(",", ":"))) < 320


@pytest.mark.parametrize(
    ("changes", "key", "readiness"),
    [
        ({"postgres_health": "unhealthy"}, "postgres_health", 0),
        ({"collector_health": "unhealthy"}, "collector_health", 0),
        (
            {"collector_restarts": 1, "collector_restart_state": "restart_loop"},
            "collector_restart_loop",
            0,
        ),
        ({"storage_state": "required_path_unwritable"}, "data_paths_writable", 0),
        ({"backup_age_seconds": 93601}, "backup_fresh", 0),
        ({"disk_free_bytes": 3 * 1024**3 - 1}, "disk_safe", 0),
        ({"swap_used_bytes": 256 * 1024**2 + 1}, "swap_safe", 2),
        ({"dashboard_disabled": False}, "dashboard_disabled", 0),
        ({"ports_safe": False}, "ports_safe", 0),
    ],
)
def test_each_failed_gate_rejects_readiness(
    monitor: ModuleType,
    changes: dict[str, object],
    key: str,
    readiness: int,
) -> None:
    metrics = monitor.evaluate(healthy_snapshot(monitor, **changes))
    assert metrics["readiness"] == readiness
    assert metrics[key] != monitor.evaluate(healthy_snapshot(monitor))[key]


def test_missing_and_malformed_values_fail_closed(monitor: ModuleType) -> None:
    metrics = monitor.evaluate(monitor.Snapshot(backup_age_seconds=-1))
    assert metrics["readiness"] == -1
    assert metrics["postgres_health"] == -1
    assert metrics["collector_health"] == -1
    assert metrics["backup_fresh"] == -1


def test_swap_warning_becomes_critical_for_low_ram_or_sustained_pressure(
    monitor: ModuleType,
) -> None:
    warning_swap = {"swap_used_bytes": monitor.MAX_SWAP_USED_BYTES + 1}
    low_ram = monitor.evaluate(
        healthy_snapshot(
            monitor,
            **warning_swap,
            available_memory_bytes=monitor.MIN_AVAILABLE_MEMORY_BYTES - 1,
        )
    )
    pressured = monitor.evaluate(
        healthy_snapshot(monitor, **warning_swap, memory_pressure_state="sustained")
    )
    assert low_ram["readiness"] == 0
    assert pressured["readiness"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"available_memory_bytes": None},
        {"memory_pressure_state": "unknown"},
        {"memory_pressure_state": "unsupported"},
    ],
)
def test_missing_or_unsupported_memory_evidence_is_unknown(
    monitor: ModuleType, changes: dict[str, object]
) -> None:
    assert monitor.evaluate(healthy_snapshot(monitor, **changes))["readiness"] == -1


@pytest.mark.parametrize(
    "changes",
    [
        {"postgres_health": "unsupported"},
        {"collector_health": "unsupported"},
        {"backup_age_seconds": -1},
        {"disk_free_bytes": -1},
        {"swap_used_bytes": -1},
        {"dashboard_disabled": "unsupported"},
    ],
)
def test_unsupported_required_signal_is_unknown(
    monitor: ModuleType, changes: dict[str, object]
) -> None:
    assert monitor.evaluate(healthy_snapshot(monitor, **changes))["readiness"] == -1


def test_database_only_storage_is_neutral_and_ready(monitor: ModuleType) -> None:
    metrics = monitor.evaluate(
        healthy_snapshot(monitor, storage_state="not_applicable")
    )
    assert metrics["data_paths_writable"] == 2
    assert metrics["storage_state"] == "not_applicable"
    assert metrics["readiness"] == 1


@pytest.mark.parametrize(
    ("state", "readiness"),
    [
        ("required_path_unwritable", 0),
        ("required_path_missing", 0),
        ("inconsistent", 0),
        ("unknown", -1),
        ("unsupported", -1),
    ],
)
def test_required_or_uncertain_storage_blocks_monitoring(
    monitor: ModuleType, state: str, readiness: int
) -> None:
    metrics = monitor.evaluate(healthy_snapshot(monitor, storage_state=state))
    assert metrics["readiness"] == readiness


def test_historical_restart_is_observable_without_blocking_readiness(
    monitor: ModuleType,
) -> None:
    metrics = monitor.evaluate(
        healthy_snapshot(
            monitor,
            collector_restarts=9,
            collector_restart_state="historical_restart",
        )
    )
    assert metrics["collector_restart_count"] == 9
    assert metrics["collector_restart_state"] == "historical_restart"
    assert metrics["collector_restart_loop"] == 0
    assert metrics["readiness"] == 1


def test_recent_restart_alerts_without_changing_collector_health(
    monitor: ModuleType,
) -> None:
    metrics = monitor.evaluate(
        healthy_snapshot(
            monitor,
            collector_restarts=1,
            collector_restart_state="recent_restart",
        )
    )
    assert metrics["collector_health"] == 2
    assert metrics["collector_restart_loop"] == 1
    assert metrics["readiness"] == 0


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
    assert json.loads(captured.out)["readiness"] == -1


def test_memory_pressure_classifier_is_bounded_and_conservative(
    monitor: ModuleType,
) -> None:
    baseline = monitor.MemorySample(300 * 1024**2, 300 * 1024**2, 10, 10, 0.0)
    inactive = monitor.MemorySample(
        300 * 1024**2,
        300 * 1024**2,
        10,
        10,
        monitor.MAX_FULL_MEMORY_PRESSURE_AVG10,
    )
    active = monitor.MemorySample(
        300 * 1024**2,
        300 * 1024**2 + monitor.MAX_SWAP_ACTIVITY_BYTES + 1,
        11,
        11,
        0.5,
    )
    pressure = monitor.MemorySample(300 * 1024**2, 300 * 1024**2, 10, 10, 1.1)
    malformed = monitor.MemorySample(300 * 1024**2, 300 * 1024**2, 9, 10, 0.0)
    assert monitor.HostProbe._memory_pressure_state(baseline, inactive) == "inactive"
    assert monitor.HostProbe._memory_pressure_state(baseline, active) == "sustained"
    assert monitor.HostProbe._memory_pressure_state(baseline, pressure) == "sustained"
    assert monitor.HostProbe._memory_pressure_state(baseline, malformed) == "unknown"


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
    assert "hibachi.collect.readiness" in document
    assert sum(line.startswith("| `") for line in document.splitlines()) >= 14
    assert "opens no listener" in document
    assert "no automatic remediation" in document
