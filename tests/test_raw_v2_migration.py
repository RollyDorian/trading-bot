from pathlib import Path


def test_raw_v2_migration_avoids_indexes_and_combines_column_adds() -> None:
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260729_0002_raw_v2_envelope.py"
    ).read_text(encoding="utf-8")
    assert migration.count("ALTER TABLE market_events") == 2
    assert "CREATE INDEX" not in migration.upper()
    assert "create_index" not in migration
    assert "lock_timeout" in migration
