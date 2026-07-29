import asyncio
import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from tests.integration.database import require_test_database_url
from tests.test_normalization_parsers import raw
from trading_bot.archive.exporter import ArchiveExporter, ArchiveRequest
from trading_bot.archive.manifest import sha256_bytes
from trading_bot.archive.retention import RetentionCandidate, RetentionExecutor
from trading_bot.archive.store import LocalArchiveStore
from trading_bot.normalization.resources import GIB, MIB
from trading_bot.storage.database import create_engine, create_session_factory
from trading_bot.storage.models import MarketEvent


class SafeProbe:
    def disk_free_bytes(self, path: Path) -> int:
        return 10 * GIB

    def rss_bytes(self) -> int:
        return 10 * MIB


class InterruptingStore(LocalArchiveStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.fail = True
        self.calls = 0

    def publish_file(self, key: str, source: Path) -> None:
        self.calls += 1
        if self.fail and self.calls == 2:
            raise OSError("injected archive interruption")
        super().publish_file(key, source)


def test_interrupted_export_resumes_and_duplicate_run_is_idempotent(tmp_path: Path) -> None:
    async def check() -> None:
        engine = create_engine(require_test_database_url())
        factory = create_session_factory(engine)
        symbol = f"ARCHIVE-{uuid4().hex[:20]}"
        start = datetime(2036, 1, 1, tzinfo=UTC)
        events = []
        for raw_id, fixture in enumerate(("ask_bid_price", "mark_price", "orderbook_snapshot"), 1):
            event = raw(fixture, raw_id=raw_id)
            event.id = None
            event.symbol = symbol
            event.payload = copy.deepcopy(event.payload)
            event.payload["symbol"] = symbol
            event.received_at = start + timedelta(seconds=raw_id)
            events.append(event)
        store = InterruptingStore(tmp_path / "store")
        request = ArchiveRequest(
            start=start,
            end=start + timedelta(days=1),
            symbol=symbol,
            work_dir=tmp_path / "work",
            capacity_path=tmp_path,
            batch_size=2,
        )
        try:
            async with factory.begin() as session:
                session.add_all(events)
            exporter = ArchiveExporter(factory, store, resource_probe=SafeProbe())
            with pytest.raises(OSError, match="interruption"):
                await exporter.export_day(request)
            store.fail = False
            first = await exporter.export_day(request)
            second = await exporter.export_day(request)
            assert first == second
            assert first.raw_row_count == 3
            assert first.verification_status == "verified"
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_retention_executor_deletes_only_one_bounded_verified_chunk(tmp_path: Path) -> None:
    async def check() -> None:
        engine = create_engine(require_test_database_url())
        factory = create_session_factory(engine)
        symbol = f"RETENTION-{uuid4().hex[:18]}"
        try:
            async with factory.begin() as session:
                events = []
                for index in range(3):
                    event = raw("mark_price", raw_id=index + 1)
                    event.id = None
                    event.symbol = symbol
                    event.payload = copy.deepcopy(event.payload)
                    event.payload["symbol"] = symbol
                    events.append(event)
                session.add_all(events)
            minimum = min(event.id for event in events)
            maximum = max(event.id for event in events)
            candidate = RetentionCandidate(
                interval_start_utc="2026-07-01T00:00:00+00:00",
                interval_end_utc="2026-07-02T00:00:00+00:00",
                min_raw_event_id=minimum,
                max_raw_event_id=maximum,
                row_count=3,
                manifest_sha256=sha256_bytes(b"manifest"),
            )
            executor = RetentionExecutor(
                factory,
                LocalArchiveStore(tmp_path / "audit"),
                test_mode=True,
            )
            deleted = await executor.delete_verified_chunk(
                candidate,
                limit=2,
                confirmation="DELETE_VERIFIED_ARCHIVE",
            )
            assert deleted == 2
            async with factory() as session:
                remaining = await session.scalar(
                    select(func.count())
                    .select_from(MarketEvent)
                    .where(MarketEvent.symbol == symbol)
                )
            assert remaining == 1
        finally:
            await engine.dispose()

    asyncio.run(check())
