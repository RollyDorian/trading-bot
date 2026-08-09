import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from tests.integration.database import require_test_database_url
from tests.test_normalization_parsers import raw
from trading_bot.archive.exporter import ArchiveExporter, ArchiveRequest
from trading_bot.archive.manifest import sha256_bytes
from trading_bot.archive.retention import (
    DELETE_CONFIRMATION_TOKEN,
    ArchivedRawRangeTarget,
    BoundedRetentionRunner,
    RetentionCandidate,
    RetentionExecutor,
    RetentionRuntimeGuards,
)
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


def _retention_guards() -> RetentionRuntimeGuards:
    return RetentionRuntimeGuards(
        collector_stopped=True,
        write_quiescent=True,
        postgresql_healthy=True,
        free_disk_bytes=4 * GIB,
        min_free_disk_bytes=3 * GIB,
    )


def test_bounded_retention_runner_resumes_and_preserves_outside_rows(
    tmp_path: Path,
) -> None:
    async def check() -> None:
        engine = create_engine(require_test_database_url())
        factory = create_session_factory(engine)
        symbol = f"BOUNDED-RET-{uuid4().hex[:16]}"
        outside_symbol = f"OUTSIDE-{uuid4().hex[:16]}"
        try:
            async with factory.begin() as session:
                target_events = []
                for index in range(5):
                    event = raw("mark_price", raw_id=index + 1)
                    event.id = None
                    event.symbol = symbol
                    event.payload = copy.deepcopy(event.payload)
                    event.payload["symbol"] = symbol
                    target_events.append(event)
                outside_before = raw("mark_price", raw_id=99)
                outside_before.id = None
                outside_before.symbol = outside_symbol
                outside_after = raw("mark_price", raw_id=199)
                outside_after.id = None
                outside_after.symbol = outside_symbol
                session.add_all([*target_events, outside_before, outside_after])
            async with factory() as session:
                target_ids = list(
                    await session.scalars(
                        select(MarketEvent.id).where(MarketEvent.symbol == symbol)
                    )
                )
            minimum = min(target_ids)
            maximum = max(target_ids)
            target = ArchivedRawRangeTarget(
                min_raw_event_id=minimum,
                max_raw_event_id=maximum,
                expected_row_count=len(target_ids),
                coverage_plan_sha256="integration-test",
                windows=(),
            )
            audit_dir = tmp_path / "audit"
            runner = BoundedRetentionRunner(factory, audit_dir, test_mode=True)
            first = await runner.execute(
                target,
                _retention_guards(),
                confirmation=DELETE_CONFIRMATION_TOKEN,
                confirm_delete=True,
                batch_size=2,
                max_batches=1,
                pause_seconds=0,
            )
            assert first["deleted_rows"] == 2
            second = await runner.execute(
                target,
                _retention_guards(),
                confirmation=DELETE_CONFIRMATION_TOKEN,
                confirm_delete=True,
                batch_size=2,
                max_batches=10,
                pause_seconds=0,
                operation_id=first["operation_id"],
            )
            assert second["status"] == "completed"
            assert second["remaining_rows"] == 0
            third = await runner.execute(
                target,
                _retention_guards(),
                confirmation=DELETE_CONFIRMATION_TOKEN,
                confirm_delete=True,
                batch_size=2,
                pause_seconds=0,
                operation_id=first["operation_id"],
            )
            assert third["deleted_rows"] == 0
            assert third["status"] == "completed"
            async with factory() as session:
                remaining_target = await session.scalar(
                    select(func.count())
                    .select_from(MarketEvent)
                    .where(MarketEvent.symbol == symbol)
                )
                remaining_outside = await session.scalar(
                    select(func.count())
                    .select_from(MarketEvent)
                    .where(MarketEvent.symbol == outside_symbol)
                )
            assert remaining_target == 0
            assert remaining_outside == 2
            progress = json.loads(
                (audit_dir / f"{first['operation_id']}.progress.json").read_text(
                    encoding="utf-8"
                )
            )
            assert progress["status"] == "completed"
            assert progress["cumulative_deleted"] == len(target_ids)
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
