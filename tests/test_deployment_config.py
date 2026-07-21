import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
COMPOSE_FILES = (ROOT / "compose.yaml", ROOT / "compose.production.yaml")


def _service_blocks(compose: str) -> dict[str, str]:
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    in_services = False
    for line in compose.splitlines():
        if line == "services:":
            in_services = True
            continue
        if in_services and line and not line.startswith(" "):
            break
        if in_services and line.startswith("  ") and not line.startswith("    "):
            current = line.strip().removesuffix(":")
            blocks[current] = [line]
        elif current is not None:
            blocks[current].append(line)
    return {name: "\n".join(lines) for name, lines in blocks.items()}


@pytest.mark.parametrize("compose_path", COMPOSE_FILES)
def test_runtime_services_are_internal_and_memory_bounded(compose_path: Path) -> None:
    compose = compose_path.read_text(encoding="utf-8")
    services = _service_blocks(compose)
    assert {"postgres", "collector", "dashboard"} <= services.keys()
    assert "ports:" not in compose
    assert "network_mode: host" not in compose
    assert "mem_limit: 256m" in services["postgres"]
    assert "mem_limit: 160m" in services["collector"]
    assert "mem_limit: 80m" in services["dashboard"]
    assert 'profiles: ["dashboard"]' in services["dashboard"]


@pytest.mark.parametrize("compose_path", COMPOSE_FILES)
def test_postgres_is_persistent_tuned_and_gates_collector(compose_path: Path) -> None:
    services = _service_blocks(compose_path.read_text(encoding="utf-8"))
    postgres = services["postgres"]
    collector = services["collector"]
    assert "postgres:16-alpine" in postgres
    assert "postgres-data:/var/lib/postgresql/data" in postgres
    assert "pg_isready" in postgres
    for setting in (
        "shared_buffers=64MB",
        "work_mem=4MB",
        "maintenance_work_mem=32MB",
        "max_connections=5",
        "log_min_duration_statement=1000",
    ):
        assert setting in postgres
    assert 'command: ["hibachi-bot", "--stream"]' in collector
    assert "postgres:" in collector
    assert "condition: service_healthy" in collector


@pytest.mark.parametrize("compose_path", COMPOSE_FILES)
def test_runtime_secrets_remain_required(compose_path: Path) -> None:
    compose = compose_path.read_text(encoding="utf-8")
    assert "${POSTGRES_PASSWORD:?" in compose
    assert "${DATABASE_URL:?" in compose
    assert "${MIGRATION_DATABASE_URL:?" in compose


@pytest.mark.parametrize("compose_path", COMPOSE_FILES)
def test_compose_config_renders_with_non_secret_substitutions(
    compose_path: Path, tmp_path: Path
) -> None:
    standalone = shutil.which("docker-compose")
    docker = shutil.which("docker")
    if standalone is None and docker is None:
        pytest.skip("Docker CLI is unavailable")
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP"}
    }
    environment.update(
        {
            "DOCKER_CONFIG": str(docker_config),
            "POSTGRES_PASSWORD": "compose-validation-placeholder",
            "DATABASE_URL": "postgresql+asyncpg://postgres/research",
            "MIGRATION_DATABASE_URL": "postgresql+asyncpg://postgres/research",
            "IMAGE_REPOSITORY": "example.invalid/hibachi-collector",
            "IMAGE_DIGEST": f"sha256:{'0' * 64}",
            "DASHBOARD_TOKEN": "compose-validation-placeholder",
            "DATASETS_PATH": "/tmp/hibachi-datasets",
            "REPORTS_PATH": "/tmp/hibachi-reports",
        }
    )
    command = [standalone] if standalone is not None else [docker, "compose"]
    result = subprocess.run(
        [*command, "-f", str(compose_path), "config", "--quiet"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "Compose config validation failed"


def test_production_compose_is_collect_only_and_uses_immutable_image() -> None:
    compose = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    assert "BOT_MODE: collect" in compose
    assert "DATABASE_ROLE: research" in compose
    assert "DATABASE_ROLE: test" not in compose
    assert "TEST_DATABASE_URL" not in compose
    assert "${DATABASE_URL:?" in compose
    assert "${MIGRATION_DATABASE_URL:?" in compose
    assert "${DATASETS_PATH:?" in compose
    assert "${REPORTS_PATH:?" in compose
    assert "@${IMAGE_DIGEST:?" in compose
    assert '["hibachi-bot", "--stream"]' in compose
    assert "trading_bot.healthcheck" in compose


def test_docker_context_excludes_secrets_and_generated_data() -> None:
    patterns = set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())
    required = {".env", ".env.*", ".venv", "datasets", "data", "*.parquet", "*.dump"}
    assert required <= patterns


def test_ci_verifies_before_building_and_never_deploys() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "needs: verify" in workflow
    assert "alembic -x database-role=test upgrade head" in workflow
    assert "TEST_DATABASE_ROLE: test" in workflow
    assert "docker/build-push-action@v6" in workflow
    assert "secrets.GITHUB_TOKEN" in workflow
    assert "ssh" not in workflow.casefold()
    assert "deploy" not in workflow.casefold()
