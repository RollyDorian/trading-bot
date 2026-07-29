from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "raw_v2_preflight.sh"


def test_raw_v2_preflight_is_bounded_read_only_and_secret_safe() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert "BEGIN READ ONLY" in script
    assert "statement_timeout = '\\''5s'\\''" in script
    assert "timeout 15 docker compose" in script
    assert "count(*)::bigint" in script
    assert "pg_relation_size" in script
    assert "pg_indexes_size" in script
    assert "pg_stat_activity" in script
    assert "pg_locks" in script
    assert "payload" not in script
    assert "printenv" not in script
    assert "cat \"$HIBACHI_RUNTIME_ENV\"" not in script
    assert "env |" not in script
    for mutation in ("INSERT ", "UPDATE ", "DELETE ", "ALTER ", "CREATE ", "DROP "):
        assert mutation not in script


def test_raw_v2_preflight_emits_only_bounded_aggregate_fields() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    output = script.split('printf \'%s\\n\' \\\n    "', maxsplit=1)[1]
    assert "DATABASE_URL" not in output
    assert "POSTGRES_PASSWORD" not in output
    assert "payload" not in output
    assert "postgres_version=" in output
    assert "row_count=" in output
    assert "heap_bytes=" in output
    assert "index_bytes=" in output
    assert "active_transactions=" in output
    assert "relation_locks=" in output
    assert "waiting_locks=" in output
