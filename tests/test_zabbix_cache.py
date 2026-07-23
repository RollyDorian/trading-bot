import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "zabbix_cache.py"
INSTALLER = ROOT / "scripts" / "install_zabbix_monitoring.sh"
ROLLBACK = ROOT / "scripts" / "rollback_zabbix_monitoring.sh"
VALIDATOR = ROOT / "scripts" / "validate_zabbix_monitoring.sh"
USER_PARAMETERS = ROOT / "deploy" / "zabbix" / "hibachi-collect.conf"
SERVICE = ROOT / "deploy" / "systemd" / "hibachi-collect-monitor.service"
TIMER = ROOT / "deploy" / "systemd" / "hibachi-collect-monitor.timer"


def load_cache() -> ModuleType:
    spec = importlib.util.spec_from_file_location("zabbix_cache", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cache_module() -> ModuleType:
    return load_cache()


def metrics(**changes: int | str) -> dict[str, int | str]:
    values: dict[str, int | str] = {
        "backup_fresh": 1,
        "collector_health": 2,
        "collector_restart_count": 4,
        "collector_restart_loop": 0,
        "collector_restart_state": "historical_restart",
        "dashboard_disabled": 1,
        "data_paths_writable": 2,
        "disk_safe": 1,
        "ports_safe": 1,
        "postgres_health": 1,
        "readiness": 1,
        "runtime_safe": 1,
        "storage_state": "not_applicable",
        "swap_safe": 1,
    }
    values.update(changes)
    return values


def fake_completed(payload: object, returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        stdout=json.dumps(payload, separators=(",", ":")),
        stderr="credential-bearing error",
        returncode=returncode,
    )


def mark_root_owned(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    original = Path.lstat

    def fake_lstat(self: Path) -> os.stat_result:
        result = original(self)
        values = list(result)
        values[0] = stat.S_IFREG | 0o640
        values[4] = 0
        return os.stat_result(values)

    monkeypatch.setattr(Path, "lstat", fake_lstat)


def test_collect_creates_atomic_restrictive_sanitized_cache(
    cache_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "metrics.json"
    cache.write_text("old", encoding="ascii")
    monkeypatch.setattr(
        cache_module.subprocess, "run", lambda *args, **kwargs: fake_completed(metrics())
    )
    assert cache_module.collect(["monitor"], cache, now=1000) == 0
    if os.name != "nt":
        assert stat.S_IMODE(cache.stat().st_mode) == 0o640
    payload = json.loads(cache.read_text(encoding="ascii"))
    assert payload["generated_at"] == 1000
    assert payload["readiness"] == 1
    assert not list(tmp_path.glob(".metrics.*"))


@pytest.mark.parametrize(
    "payload",
    [
        {},
        metrics(extra=1),
        {key: value for key, value in metrics().items() if key != "disk_safe"},
        metrics(disk_safe=2),
        metrics(collector_restart_count=1_000_001),
        metrics(collector_restart_state="secret-path"),
        metrics(readiness=0),
    ],
)
def test_invalid_collection_never_replaces_cache(
    cache_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> None:
    cache = tmp_path / "metrics.json"
    cache.write_text("preserved", encoding="ascii")
    monkeypatch.setattr(
        cache_module.subprocess, "run", lambda *args, **kwargs: fake_completed(payload)
    )
    assert cache_module.collect(["monitor"], cache) == 1
    assert cache.read_text(encoding="ascii") == "preserved"


def test_timeout_and_unavailable_command_fail_closed_without_output(
    cache_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired("postgresql://user:secret@host/db", 45)

    monkeypatch.setattr(cache_module.subprocess, "run", fail)
    assert cache_module.collect(["monitor"], tmp_path / "cache") == 1
    assert capsys.readouterr() == ("", "")


def write_cache(cache_module: ModuleType, path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="ascii")
    path.chmod(0o640)


def valid_cache(cache_module: ModuleType, now: int = 1000) -> dict[str, int | str]:
    return {
        "generated_at": now,
        "schema_version": cache_module.SCHEMA_VERSION,
        **metrics(),
    }


def test_reader_returns_one_bounded_value(
    cache_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    write_cache(cache_module, cache, valid_cache(cache_module))
    mark_root_owned(monkeypatch, cache)
    assert cache_module.read_item(cache, "postgres", now=1000) == 1
    assert cache_module.read_item(cache, "storage", now=1000) == 2
    assert cache_module.read_item(cache, "restart_count", now=1000) == 4
    assert cache_module.read_item(cache, "restart_state", now=1000) == 1
    assert cache_module.read_item(cache, "arbitrary", now=1000) == -1


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(generated_at=849),
        lambda value: value.update(generated_at=1006),
        lambda value: value.update(unexpected=1),
        lambda value: value.pop("disk_safe"),
        lambda value: value.update(schema_version=2),
    ],
)
def test_stale_future_unexpected_missing_and_malformed_cache_fail_closed(
    cache_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: object,
) -> None:
    payload = valid_cache(cache_module)
    mutation(payload)
    cache = tmp_path / "cache"
    write_cache(cache_module, cache, payload)
    mark_root_owned(monkeypatch, cache)
    assert cache_module.read_item(cache, "readiness", now=1000) == -1


def test_duplicate_truncated_oversized_missing_and_symlink_fail_closed(
    cache_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    cases = [
        '{"schema_version":1,"schema_version":1}',
        '{"schema_version":',
        "x" * (cache_module.MAX_CACHE_BYTES + 1),
    ]
    for content in cases:
        cache.write_text(content, encoding="ascii")
        cache.chmod(0o640)
        assert cache_module.read_item(cache, "readiness", now=1000) == -1
    cache.unlink()
    assert cache_module.read_item(cache, "readiness", now=1000) == -1


def test_static_installation_contract_is_least_privilege_and_bounded() -> None:
    service = SERVICE.read_text(encoding="utf-8")
    timer = TIMER.read_text(encoding="utf-8")
    parameters = USER_PARAMETERS.read_text(encoding="utf-8")
    assert "Type=oneshot" in service
    assert "User=root" in service and "Group=zabbix" in service
    assert "TimeoutStartSec=55" in service
    assert "RuntimeDirectoryPreserve=yes" in service
    assert "OnUnitActiveSec=60s" in timer
    assert parameters.count("UserParameter=hibachi.collect.") == 12
    assert "[*]" not in parameters
    for forbidden in ("docker", "sudo", "|", "DATABASE_URL", "bash -c"):
        assert forbidden not in parameters


def test_installer_and_rollback_are_idempotent_by_contract() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    rollback = ROLLBACK.read_text(encoding="utf-8")
    validator = VALIDATOR.read_text(encoding="utf-8")
    assert "grep -Fqx" in installer
    assert "install -d -o root -g root -m 0755 /usr/local/libexec" in installer
    assert "zabbix_agentd -t" in installer
    assert "systemd-analyze verify" in installer
    assert "rm -f --" in rollback
    assert "systemctl disable --now hibachi-collect-monitor.timer" in rollback
    assert "id zabbix | grep -q docker && fail" in validator
    assert "sudo -l -U zabbix" in validator
    assert r"sed -n 's/.*\[t|\([^]]*\)\]$/\1/p'" in validator
    assert "restart_count:[0-9][0-9]*" in validator
    assert "restart_state:4" in validator
