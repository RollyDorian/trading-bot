import datetime as dt
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "collect_failure_retention.py"
SERVICE = ROOT / "deploy" / "systemd" / "hibachi-collect-failure-retention.service"


def load_retention() -> ModuleType:
    spec = importlib.util.spec_from_file_location("collect_failure_retention", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_sanitization_removes_secrets_addresses_paths_and_tracebacks() -> None:
    module = load_retention()
    assert (
        module.sanitize_line("password=abc /private/file 192.0.2.1")
        == "password=[REDACTED] [REDACTED] [REDACTED]"
    )
    assert module.sanitize_line("Traceback (most recent call last):") is None
    assert module.sanitize_line("  File /private/file, line 4") is None


def test_record_is_bounded_and_contains_only_contract_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = load_retention()
    monkeypatch.setattr(module, "_metadata", lambda value: (17, 3, 1))
    monkeypatch.setattr(module, "_stderr", lambda value: ["DatabaseError database unavailable"])
    monkeypatch.setattr(module, "_revision", lambda value: "a" * 40)
    record = module.build_record(
        "a" * 12, tmp_path, now=dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    )
    assert record == {
        "timestamp": "2026-01-02T03:04:05Z",
        "exit_code": 17,
        "restart_count": 3,
        "failure_stage": "database",
        "error_class": "DatabaseError",
        "stderr_lines": ["DatabaseError database unavailable"],
        "oom_killed": 1,
        "revision": "a" * 40,
    }
    assert len(json.dumps(record, separators=(",", ":")).encode("ascii")) <= module.MAX_RECORD_BYTES


def test_retention_keeps_latest_and_bounded_number_of_exact_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_retention()
    directory = tmp_path / "failures"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda self: (
            os.stat_result((stat.S_IFDIR | 0o700, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            if self == directory
            else Path.stat(self)
        ),
    )
    for second in range(module.MAX_RECORDS + 2):
        record = {
            "timestamp": f"2026-01-01T00:00:{second:02d}Z",
            "exit_code": 1,
            "restart_count": 0,
            "failure_stage": "unknown",
            "error_class": "unknown",
            "stderr_lines": [],
            "oom_killed": 0,
            "revision": "unknown",
        }
        module.retain(record, directory)
    records = [path for path in directory.glob("failure-*.json") if path.name != "latest.json"]
    assert len(records) == module.MAX_RECORDS
    assert (
        json.loads((directory / "latest.json").read_text(encoding="ascii"))["timestamp"]
        == "2026-01-01T00:00:21Z"
    )


def test_listener_service_is_root_only_and_has_no_network_listener() -> None:
    service = SERVICE.read_text(encoding="utf-8")
    assert "User=root" in service and "StateDirectoryMode=0700" in service
    assert "RestrictAddressFamilies=AF_UNIX" in service
    assert "Restart=on-failure" in service
