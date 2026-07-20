from pathlib import Path

ROOT = Path(__file__).parents[1]


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
