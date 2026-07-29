from datetime import UTC, datetime

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from trading_bot.storage.models import (
    BestQuote,
    FundingEstimate,
    MarketEvent,
    NormalizationError,
    NormalizerCheckpoint,
    OrderBookEvent,
    ReferencePrice,
    SystemEvent,
)
from trading_bot.storage.repository import MarketEventInput


def test_market_event_postgres_schema_uses_jsonb_and_timestamps() -> None:
    ddl = str(CreateTable(MarketEvent.__table__).compile(dialect=postgresql.dialect()))
    assert "JSONB" in ddl
    assert "TIMESTAMP WITH TIME ZONE" in ddl
    assert "received_at" in ddl
    assert "exchange_at" in ddl
    assert "connection_id" in ddl
    assert "local_sequence" in ddl
    assert "exchange_sequence" in ddl
    assert "schema_version" in ddl


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


def test_legacy_market_event_input_defaults_to_raw_v1() -> None:
    event = MarketEventInput(
        received_at=datetime.now(UTC),
        exchange_at=None,
        source="legacy_fixture",
        event_type="trades",
        symbol="ETH/USDT-P",
        sequence=None,
        latency_ms=None,
        payload={},
    )
    assert event.schema_version == 1
    assert event.connection_id is None
    assert event.local_sequence is None
    assert event.exchange_sequence is None


def test_normalized_core_uses_separate_schemas_and_complete_provenance() -> None:
    provenance = {
        "raw_event_id",
        "received_at",
        "available_at",
        "exchange_at",
        "symbol",
        "source",
        "connection_id",
        "local_sequence",
        "exchange_sequence",
        "raw_schema_version",
        "pipeline_version",
        "data_quality",
    }
    for model in (BestQuote, ReferencePrice, FundingEstimate, OrderBookEvent):
        assert model.__table__.schema == "normalized"
        assert provenance <= set(model.__table__.columns.keys())
        assert not model.__table__.foreign_keys
    assert NormalizerCheckpoint.__table__.schema == "pipeline"
    assert NormalizationError.__table__.schema == "pipeline"
    assert not NormalizationError.__table__.foreign_keys
