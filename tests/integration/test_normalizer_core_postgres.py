import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from tests.integration.database import require_test_database_url
from trading_bot.normalization.pilot import _wal_delta, _wal_lsn
from trading_bot.normalization.resources import GIB, MIB
from trading_bot.normalization.runner import (
    ConcurrentNormalizerError,
    RawEventNormalizer,
    _lock_key,
)
from trading_bot.storage.database import create_engine, create_session_factory
from trading_bot.storage.models import (
    BestQuote,
    MarketEvent,
    NormalizationError,
    NormalizerCheckpoint,
    OrderBookEvent,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "hibachi"


class SafeProbe:
    def disk_free_bytes(self, path: Path) -> int:
        return 10 * GIB

    def rss_bytes(self) -> int:
        return 10 * MIB


def _payload(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _event(name: str, sequence: int) -> MarketEvent:
    body = _payload(name)
    return MarketEvent(
        received_at=datetime.now(UTC),
        exchange_at=None,
        source="hibachi_ws",
        event_type=str(body["topic"]),
        symbol=str(body["symbol"]),
        sequence=None,
        connection_id=str(uuid4()),
        local_sequence=sequence,
        exchange_sequence=None,
        schema_version=2,
        latency_ms=None,
        payload=body,
    )


def test_atomic_batch_checkpoint_poison_error_and_rerun_idempotency() -> None:
    async def check() -> None:
        engine = create_engine(require_test_database_url())
        factory = create_session_factory(engine)
        consumer = f"core-{uuid4()}"
        try:
            async with factory.begin() as session:
                previous = int(
                    await session.scalar(
                        select(func.coalesce(func.max(MarketEvent.id), 0))
                    )
                    or 0
                )
                quote = _event("ask_bid_price", 1)
                book = _event("orderbook_snapshot", 2)
                unsupported = _event("mark_price", 3)
                unsupported.event_type = "unsupported_fixture"
                session.add_all([quote, book, unsupported])
            normalizer = RawEventNormalizer(
                factory,
                consumer=consumer,
                batch_size=10,
                resource_probe=SafeProbe(),
            )
            first = await normalizer.normalize_batch(initial_raw_event_id=previous)
            second = await normalizer.normalize_batch()
            assert (first.raw_rows_read, first.normalized_rows, first.error_rows) == (3, 2, 1)
            assert second.raw_rows_read == 0
            async with factory() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(BestQuote)
                        .where(BestQuote.raw_event_id == quote.id)
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(OrderBookEvent)
                        .where(OrderBookEvent.raw_event_id == book.id)
                    )
                    == 1
                )
                error = await session.scalar(
                    select(NormalizationError).where(
                        NormalizationError.raw_event_id == unsupported.id
                    )
                )
                checkpoint = await session.get(NormalizerCheckpoint, consumer)
                assert error is not None
                assert error.error_code == "unsupported_event_type"
                assert checkpoint is not None
                assert checkpoint.last_raw_event_id == unsupported.id
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_partial_batch_failure_rolls_back_insert_and_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def check() -> None:
        engine = create_engine(require_test_database_url())
        factory = create_session_factory(engine)
        consumer = f"rollback-{uuid4()}"
        try:
            async with factory.begin() as session:
                previous = int(
                    await session.scalar(
                        select(func.coalesce(func.max(MarketEvent.id), 0))
                    )
                    or 0
                )
                quote = _event("ask_bid_price", 1)
                session.add(quote)
            normalizer = RawEventNormalizer(
                factory,
                consumer=consumer,
                resource_probe=SafeProbe(),
            )
            monkeypatch.setattr(
                "trading_bot.normalization.runner._model_insert",
                lambda record: (_ for _ in ()).throw(RuntimeError("injected failure")),
            )
            with pytest.raises(RuntimeError, match="injected failure"):
                await normalizer.normalize_batch(initial_raw_event_id=previous)
            async with factory() as session:
                assert await session.get(NormalizerCheckpoint, consumer) is None
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(BestQuote)
                        .where(BestQuote.raw_event_id == quote.id)
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_same_consumer_concurrency_fails_closed() -> None:
    async def check() -> None:
        engine = create_engine(require_test_database_url())
        factory = create_session_factory(engine)
        consumer = f"locked-{uuid4()}"
        try:
            async with factory.begin() as lock_session:
                assert await lock_session.scalar(
                    select(func.pg_try_advisory_xact_lock(_lock_key(consumer)))
                )
                normalizer = RawEventNormalizer(
                    factory,
                    consumer=consumer,
                    resource_probe=SafeProbe(),
                )
                with pytest.raises(ConcurrentNormalizerError):
                    await normalizer.normalize_batch()
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_normalized_tables_do_not_reference_raw_with_foreign_keys() -> None:
    async def check() -> None:
        engine = create_engine(require_test_database_url())
        try:
            async with engine.connect() as connection:
                count = await connection.scalar(
                    text(
                        """
                        SELECT count(*)
                        FROM pg_constraint
                        WHERE contype = 'f'
                          AND connamespace IN (
                              'normalized'::regnamespace,
                              'pipeline'::regnamespace
                          )
                        """
                    )
                )
            assert count == 0
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_pilot_wal_measurement_uses_a_typed_lsn() -> None:
    async def check() -> None:
        engine = create_engine(require_test_database_url())
        factory = create_session_factory(engine)
        try:
            async with factory() as session:
                start = await _wal_lsn(session)
                assert await _wal_delta(session, start) >= 0
        finally:
            await engine.dispose()

    asyncio.run(check())
