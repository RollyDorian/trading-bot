import argparse
import asyncio
import json
import time
from collections import defaultdict
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select, text

from trading_bot.archive.exporter import ArchiveExporter, ArchiveRequest
from trading_bot.archive.store import LocalArchiveStore
from trading_bot.config import integration_test_database_url
from trading_bot.normalization.resources import SystemResourceProbe
from trading_bot.storage.database import create_engine, create_session_factory
from trading_bot.storage.models import MarketEvent

MAX_PILOT_ROWS = 100000


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="archive-capacity-pilot")
    parser.add_argument("--start", required=True, type=datetime.fromisoformat)
    parser.add_argument("--end", required=True, type=datetime.fromisoformat)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--store-root", required=True, type=Path)
    parser.add_argument("--batch-size", default=5000, type=int)
    parser.add_argument("--max-rows", default=MAX_PILOT_ROWS, type=int)
    return parser


async def _monitor_rss(
    probe: SystemResourceProbe,
    stop: asyncio.Event,
    samples: list[int],
) -> None:
    while not stop.is_set():
        samples.append(probe.rss_bytes())
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=0.05)


async def _run(args: argparse.Namespace) -> dict[str, object]:
    if not 1 <= args.max_rows <= MAX_PILOT_ROWS:
        raise ValueError(f"max rows must be between 1 and {MAX_PILOT_ROWS}")
    engine = create_engine(integration_test_database_url())
    factory = create_session_factory(engine)
    probe = SystemResourceProbe()
    try:
        async with factory() as session, session.begin():
            await session.execute(text("SET TRANSACTION READ ONLY"))
            await session.execute(text("SET LOCAL statement_timeout = '5s'"))
            count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MarketEvent)
                    .where(
                        MarketEvent.received_at >= args.start,
                        MarketEvent.received_at < args.end,
                        MarketEvent.symbol == args.symbol,
                    )
                )
                or 0
            )
        if not 1 <= count <= args.max_rows:
            raise RuntimeError("pilot row count is empty or above the hard maximum")
        stop = asyncio.Event()
        rss_samples: list[int] = []
        monitor = asyncio.create_task(_monitor_rss(probe, stop, rss_samples))
        started = time.monotonic()
        try:
            manifest = await ArchiveExporter(
                factory,
                LocalArchiveStore(args.store_root),
            ).export_day(
                ArchiveRequest(
                    start=args.start,
                    end=args.end,
                    symbol=args.symbol,
                    work_dir=args.work_dir,
                    capacity_path=args.work_dir,
                    batch_size=args.batch_size,
                )
            )
        finally:
            stop.set()
            await monitor
        duration = time.monotonic() - started
        dataset_rows: dict[str, int] = defaultdict(int)
        dataset_bytes: dict[str, int] = defaultdict(int)
        for item in manifest.objects:
            dataset_rows[item.dataset] += item.row_count
            dataset_bytes[item.dataset] += item.size_bytes
        return {
            "schema_version": 1,
            "raw_rows": manifest.raw_row_count,
            "dataset_rows": dict(sorted(dataset_rows.items())),
            "dataset_bytes": dict(sorted(dataset_bytes.items())),
            "parquet_bytes": sum(dataset_bytes.values()),
            "duration_seconds": round(duration, 3),
            "throughput_rows_second": round(manifest.raw_row_count / duration, 2),
            "peak_rss_bytes": max(rss_samples, default=probe.rss_bytes()),
            "verified": True,
        }
    finally:
        await engine.dispose()


def main() -> None:
    print(json.dumps(asyncio.run(_run(_parser().parse_args())), separators=(",", ":")))


if __name__ == "__main__":
    main()
