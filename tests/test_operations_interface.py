from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "collect_ops.sh"
RUNBOOK = ROOT / "docs" / "operations_runbook.md"


def test_operations_script_is_fail_closed_and_collect_only() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert script.startswith("#!/bin/sh\nset -eu\n")
    for variable in ("HIBACHI_DEPLOY_DIR", "HIBACHI_RUNTIME_ENV", "HIBACHI_BACKUP_DIR"):
        assert f"${{{variable}:?" in script
    for command in ("status", "preflight", "logs", "backup", "validate-backup"):
        assert f"{command})" in script
    assert "docker compose" in script
    assert "config --quiet" in script
    assert "dashboard is present" in script
    assert "publishes a host port" in script
    assert "docker compose down" not in script
    assert "docker system prune" not in script
    assert "docker restart" not in script
    assert "compose restart" not in script
    assert "BOT_MODE=paper" not in script
    assert "BOT_MODE=live" not in script
    assert "trap cleanup EXIT HUP INT TERM" in script
    assert "unexpected service is enabled by default" in script
    assert 'scripts/restart_state.py"' in script
    assert "historical_restart" in script
    assert "recent_restart|restart_loop|unhealthy|unknown" in script
    assert 'scripts/storage_state.py"' in script
    assert "ready|not_applicable" in script
    assert (
        "required_path_unwritable|required_path_missing|inconsistent|unknown"
        in script
    )


def test_backup_and_restore_validation_are_bounded() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert "pg_dump --format=custom" in script
    assert "pg_restore --list" in script
    assert "MAX_BACKUP_RETENTION=20" in script
    assert "hibachi-????????T??????Z-???????.dump" in script
    assert "--network none" in script
    assert "--memory 192m" in script
    assert "--rm" in script
    assert "production_unchanged=true" in script


def test_logs_are_bounded_and_redacted() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert "MAX_LOG_LINES=1000" in script
    assert "--since" in script
    assert "--tail" in script
    assert "[REDACTED]" in script
    assert "[REDACTED_IP]" in script
    assert "[REDACTED_HOST]" in script


def test_runbook_documents_safe_operational_contract() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    for command in ("status", "preflight", "logs", "backup", "validate-backup"):
        assert f"collect_ops.sh {command}" in runbook
    assert "3 GiB" in runbook
    assert "256 MiB" in runbook
    assert "no published port" in runbook
    assert "no automatic Alembic" in runbook
    assert "Never use `compose down -v`" in runbook
