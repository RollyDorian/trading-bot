import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "storage_state.py"


def load_storage() -> ModuleType:
    spec = importlib.util.spec_from_file_location("storage_state_test", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def storage() -> ModuleType:
    return load_storage()


def service(
    *,
    volumes: object = None,
    filesystem_environment: bool = False,
) -> dict[str, object]:
    environment: dict[str, str] = {
        "BOT_MODE": "collect",
        "DATABASE_ROLE": "research",
        "DATABASE_URL": "configured",
    }
    if filesystem_environment:
        environment["DATASETS_DIR"] = "/app/datasets"
        environment["ADMISSION_REPORT_PATH"] = (
            "/app/reports/paper-admission-report.json"
        )
    return {
        "environment": environment,
        "volumes": [] if volumes is None else volumes,
    }


def config(
    *,
    collector: dict[str, object] | None = None,
    dashboard: dict[str, object] | None = None,
) -> str:
    return json.dumps(
        {
            "services": {
                "collector": collector or service(),
                "dashboard": dashboard or service(
                    volumes=[
                        {"type": "bind", "target": "/app/datasets"},
                        {"type": "bind", "target": "/app/reports"},
                    ],
                    filesystem_environment=True,
                ),
            }
        }
    )


def probe(
    storage: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    rendered: str,
    *,
    dashboard_id: str = "",
) -> object:
    instance = storage.StorageProbe(("docker", "compose"))

    def compose(*args: str) -> str:
        if args[-3:] == ("config", "--format", "json"):
            return rendered
        if args == ("--profile", "dashboard", "ps", "-q", "dashboard"):
            return dashboard_id
        if args == ("ps", "-q", "dashboard"):
            return dashboard_id
        raise AssertionError(args)

    monkeypatch.setattr(instance, "_compose", compose)
    return instance


def test_database_only_without_optional_mounts_is_not_applicable(
    storage: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = probe(storage, monkeypatch, config())
    assert instance.assess().state == "not_applicable"


def test_optional_dashboard_paths_are_not_required_while_disabled(
    storage: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = probe(storage, monkeypatch, config())
    monkeypatch.setattr(
        instance,
        "_validate_required_paths",
        lambda *args: pytest.fail("disabled dashboard paths must not be probed"),
    )
    assert instance.assess().state == "not_applicable"


def test_required_configured_paths_present_and_writable(
    storage: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = probe(storage, monkeypatch, config(), dashboard_id="opaque")
    monkeypatch.setattr(
        instance, "_mounted_targets", lambda container: storage.EXPECTED_TARGETS
    )
    monkeypatch.setattr(instance, "_container_path_ok", lambda container, target: True)
    assert instance.assess().state == "ready"


def test_required_path_missing_blocks(
    storage: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = probe(storage, monkeypatch, config(), dashboard_id="opaque")
    monkeypatch.setattr(instance, "_mounted_targets", lambda container: frozenset())
    assert instance.assess().state == "required_path_missing"


def test_required_path_unwritable_blocks(
    storage: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = probe(storage, monkeypatch, config(), dashboard_id="opaque")
    monkeypatch.setattr(
        instance, "_mounted_targets", lambda container: storage.EXPECTED_TARGETS
    )
    monkeypatch.setattr(instance, "_container_path_ok", lambda container, target: False)
    assert instance.assess().state == "required_path_unwritable"


def test_configured_path_without_mount_is_inconsistent(
    storage: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = service(filesystem_environment=True)
    instance = probe(storage, monkeypatch, config(collector=collector))
    assert instance.assess().state == "inconsistent"


def test_mount_without_required_dashboard_configuration_is_inconsistent(
    storage: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    dashboard = service(
        volumes=[
            {"type": "bind", "target": "/app/datasets"},
            {"type": "bind", "target": "/app/reports"},
        ]
    )
    instance = probe(
        storage,
        monkeypatch,
        config(dashboard=dashboard),
        dashboard_id="opaque",
    )
    assert instance.assess().state == "inconsistent"


@pytest.mark.parametrize(
    "collector",
    [
        {"environment": {}, "volumes": []},
        service(volumes="malformed"),
        service(volumes=["raw-mount"]),
    ],
)
def test_unsupported_or_malformed_state_is_unknown(
    storage: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    collector: dict[str, object],
) -> None:
    instance = probe(storage, monkeypatch, config(collector=collector))
    assert instance.assess().state == "unknown"


def test_unavailable_compose_is_unknown_and_fail_closed(
    storage: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("credential-bearing internal failure")

    monkeypatch.setattr(storage.subprocess, "run", unavailable)
    assessment = storage.observe(("docker", "compose"))
    assert assessment.state == "unknown"
    assert assessment.state in storage.BLOCK_STATES


def test_public_output_is_bounded_and_redacted(
    storage: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "postgresql://user:password@private.invalid/research"

    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError(sentinel)

    monkeypatch.setattr(storage, "_required_path", fail)
    assert storage.run() == 1
    captured = capsys.readouterr()
    assert captured.out == "unknown\n"
    assert captured.err == ""
    assert sentinel not in captured.out
    assert len(captured.out) < 32


def test_pass_and_block_policies_are_disjoint(storage: ModuleType) -> None:
    assert {"ready", "not_applicable"} == storage.PASS_STATES
    assert not storage.PASS_STATES & storage.BLOCK_STATES


def test_classifier_contains_no_mutating_commands() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden in (
        "docker compose up",
        "docker compose down",
        "docker restart",
        "mkdir",
        "chmod",
        "chown",
        "DATABASE_URL=",
    ):
        assert forbidden not in source
