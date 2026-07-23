#!/usr/bin/env python3
"""Classify bounded storage readiness for the COLLECT-only stack."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Never

PASS_STATES: Final = frozenset({"ready", "not_applicable"})
BLOCK_STATES: Final = frozenset(
    {
        "required_path_unwritable",
        "required_path_missing",
        "inconsistent",
        "unknown",
    }
)
EXPECTED_TARGETS: Final = frozenset({"/app/datasets", "/app/reports"})
PROCESS_TIMEOUT_SECONDS: Final = 20
CONTAINER_DATA_UID: Final = 10001


@dataclass(frozen=True)
class StorageAssessment:
    state: str


class StorageProbe:
    def __init__(self, compose: Sequence[str]) -> None:
        self.compose = tuple(compose)

    def assess(self) -> StorageAssessment:
        config = self._config()
        services = config.get("services")
        if not isinstance(services, dict):
            return StorageAssessment("unknown")
        collector = services.get("collector")
        dashboard = services.get("dashboard")
        if not isinstance(collector, dict) or not isinstance(dashboard, dict):
            return StorageAssessment("unknown")
        if not self._is_database_collector(collector):
            return StorageAssessment("unknown")

        collector_targets = self._required_targets(collector)
        collector_configured = self._configured_targets(collector)
        if collector_targets is None or collector_configured is None:
            return StorageAssessment("unknown")
        if collector_targets != collector_configured:
            return StorageAssessment("inconsistent")
        if collector_targets:
            return self._validate_required_paths("collector", collector_targets)

        dashboard_id = self._compose("--profile", "dashboard", "ps", "-q", "dashboard")
        if not dashboard_id:
            return StorageAssessment("not_applicable")
        dashboard_targets = self._required_targets(dashboard)
        dashboard_configured = self._configured_targets(dashboard)
        if dashboard_targets is None or dashboard_configured is None:
            return StorageAssessment("unknown")
        if not dashboard_targets >= EXPECTED_TARGETS:
            return StorageAssessment("inconsistent")
        if dashboard_configured != dashboard_targets:
            return StorageAssessment("inconsistent")
        return self._validate_required_paths("dashboard", dashboard_targets)

    def _config(self) -> dict[str, object]:
        raw = self._compose("--profile", "dashboard", "config", "--format", "json")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError
        return parsed

    @staticmethod
    def _is_database_collector(service: dict[str, object]) -> bool:
        environment = service.get("environment")
        if not isinstance(environment, dict):
            return False
        return (
            environment.get("BOT_MODE") == "collect"
            and environment.get("DATABASE_ROLE") == "research"
            and isinstance(environment.get("DATABASE_URL"), str)
            and bool(environment["DATABASE_URL"])
        )

    @staticmethod
    def _configured_targets(
        service: dict[str, object],
    ) -> frozenset[str] | None:
        environment = service.get("environment")
        if not isinstance(environment, dict):
            return None
        targets: set[str] = set()
        datasets = environment.get("DATASETS_DIR")
        if datasets is not None:
            if datasets != "/app/datasets":
                return None
            targets.add("/app/datasets")
        reports = environment.get("REPORTS_DIR")
        if reports is not None:
            if reports != "/app/reports":
                return None
            targets.add("/app/reports")
        admission = environment.get("ADMISSION_REPORT_PATH")
        if admission is not None:
            if not isinstance(admission, str) or not admission.startswith("/app/reports/"):
                return None
            targets.add("/app/reports")
        return frozenset(targets)

    @staticmethod
    def _required_targets(service: dict[str, object]) -> frozenset[str] | None:
        volumes = service.get("volumes", [])
        if not isinstance(volumes, list):
            return None
        targets: set[str] = set()
        for volume in volumes:
            if not isinstance(volume, dict):
                return None
            target = volume.get("target")
            if not isinstance(target, str) or not target.startswith("/"):
                return None
            if target in EXPECTED_TARGETS:
                targets.add(target)
        return frozenset(targets)

    def _validate_required_paths(
        self, service: str, targets: frozenset[str]
    ) -> StorageAssessment:
        container_id = self._compose("ps", "-q", service)
        if not container_id or any(character.isspace() for character in container_id):
            return StorageAssessment("inconsistent")
        mounted = self._mounted_targets(container_id)
        if not targets <= mounted:
            return StorageAssessment("required_path_missing")
        for target in sorted(targets):
            if not self._container_path_ok(container_id, target):
                return StorageAssessment("required_path_unwritable")
        return StorageAssessment("ready")

    def _mounted_targets(self, container_id: str) -> frozenset[str]:
        raw = self._run(
            "docker",
            "inspect",
            "--format",
            "{{range .Mounts}}{{println .Destination}}{{end}}",
            container_id,
        )
        targets = raw.splitlines()
        if any(not target.startswith("/") for target in targets):
            raise ValueError
        return frozenset(targets)

    def _container_path_ok(self, container_id: str, target: str) -> bool:
        result = self._run(
            "docker",
            "exec",
            container_id,
            "python",
            "-c",
            (
                "import os,stat,sys;"
                "s=os.stat(sys.argv[1]);"
                f"ok=stat.S_ISDIR(s.st_mode) and s.st_uid=={CONTAINER_DATA_UID} "
                "and bool(stat.S_IMODE(s.st_mode)&stat.S_IWUSR) "
                "and bool(stat.S_IMODE(s.st_mode)&stat.S_IXUSR);"
                "raise SystemExit(0 if ok else 1)"
            ),
            target,
            check=False,
        )
        return result == "0"

    def _compose(self, *args: str) -> str:
        return self._run(*self.compose, *args)

    @staticmethod
    def _run(*args: str, check: bool = True) -> str:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
        if check and result.returncode != 0:
            raise RuntimeError
        if not check:
            return str(result.returncode)
        return result.stdout.strip()


def observe(compose: Sequence[str]) -> StorageAssessment:
    try:
        return StorageProbe(compose).assess()
    except BaseException:
        return StorageAssessment("unknown")


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
            )
        )
    except BaseException:
        assessment = StorageAssessment("unknown")
    sys.stdout.write(f"{assessment.state}\n")
    return 0 if assessment.state in PASS_STATES else 1


def main() -> Never:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
