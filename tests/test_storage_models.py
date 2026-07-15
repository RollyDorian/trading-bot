from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from trading_bot.storage.models import MarketEvent, SystemEvent


def test_market_event_postgres_schema_uses_jsonb_and_timestamps() -> None:
    ddl = str(CreateTable(MarketEvent.__table__).compile(dialect=postgresql.dialect()))
    assert "JSONB" in ddl
    assert "TIMESTAMP WITH TIME ZONE" in ddl
    assert "received_at" in ddl
    assert "exchange_at" in ddl


def test_system_event_schema_is_append_only_payload() -> None:
    columns = SystemEvent.__table__.columns
    assert "details" in columns
    assert "event_type" in columns
    assert "occurred_at" in columns


def test_expected_replay_indexes_exist() -> None:
    names = {index.name for index in MarketEvent.__table__.indexes}
    assert names == {
        "ix_market_events_source_sequence",
        "ix_market_events_symbol_exchange_at",
        "ix_market_events_type_received_at",
    }
