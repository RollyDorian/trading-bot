import argparse
import asyncio
import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from trading_bot.archive.capacity import CapacityInputs, plan_capacity
from trading_bot.archive.exporter import ArchiveExporter, ArchiveRequest
from trading_bot.archive.manifest import ArchiveManifest, sha256_bytes
from trading_bot.archive.retention import plan_retention
from trading_bot.archive.store import LocalArchiveStore, S3ArchiveStore
from trading_bot.config import Settings
from trading_bot.storage.database import create_engine, create_session_factory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hibachi-archive")
    subparsers = parser.add_subparsers(dest="command", required=True)
    capacity = subparsers.add_parser("capacity")
    capacity.add_argument("--disk-free-mib", required=True, type=float)
    capacity.add_argument("--raw-mib-day", required=True, type=float)
    capacity.add_argument("--normalized-mib-day", required=True, type=float)
    capacity.add_argument("--parquet-mib-day", required=True, type=float)
    capacity.add_argument("--wal-mib-day", required=True, type=float)
    capacity.add_argument("--measured-days", required=True, type=float)
    capacity.add_argument("--raw-hot-days", default=2, type=int)
    capacity.add_argument("--normalized-hot-days", default=0, type=int)
    export = subparsers.add_parser("export-day")
    export.add_argument("--start", required=True, type=datetime.fromisoformat)
    export.add_argument("--end", required=True, type=datetime.fromisoformat)
    export.add_argument("--symbol", required=True)
    export.add_argument("--work-dir", required=True, type=Path)
    export.add_argument("--capacity-path", required=True, type=Path)
    export.add_argument("--batch-size", default=5000, type=int)
    export.add_argument("--initial-raw-event-id", default=0, type=int)
    export.add_argument("--store", required=True, choices=("filesystem", "s3"))
    export.add_argument("--root", type=Path)
    retention = subparsers.add_parser("retention-plan")
    retention.add_argument("--manifest-key", action="append", required=True)
    retention.add_argument("--hot-raw-days", default=2, type=int)
    retention.add_argument("--now", required=True, type=datetime.fromisoformat)
    retention.add_argument("--store", required=True, choices=("filesystem", "s3"))
    retention.add_argument("--root", type=Path)
    return parser


def _store(args: argparse.Namespace) -> LocalArchiveStore | S3ArchiveStore:
    if args.store == "filesystem":
        if args.root is None:
            raise ValueError("--root is required for filesystem storage")
        return LocalArchiveStore(args.root)
    bucket = os.environ.get("ARCHIVE_S3_BUCKET")
    if not bucket:
        raise ValueError("ARCHIVE_S3_BUCKET is required")
    return S3ArchiveStore(
        bucket=bucket,
        prefix=os.environ.get("ARCHIVE_S3_PREFIX", ""),
        endpoint_override=os.environ.get("ARCHIVE_S3_ENDPOINT"),
        access_key=os.environ.get("ARCHIVE_S3_ACCESS_KEY"),
        secret_key=os.environ.get("ARCHIVE_S3_SECRET_KEY"),
    )


async def _export(args: argparse.Namespace) -> None:
    settings = Settings()
    engine = create_engine(settings.database_url)
    try:
        exporter = ArchiveExporter(create_session_factory(engine), _store(args))
        manifest = await exporter.export_day(
            ArchiveRequest(
                start=args.start,
                end=args.end,
                symbol=args.symbol,
                work_dir=args.work_dir,
                capacity_path=args.capacity_path,
                batch_size=args.batch_size,
                initial_raw_event_id=args.initial_raw_event_id,
            )
        )
    finally:
        await engine.dispose()
    print(
        json.dumps(
            {
                "status": manifest.verification_status,
                "raw_rows": manifest.raw_row_count,
                "objects": len(manifest.objects),
                "destination": manifest.destination,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def main() -> None:
    args = _parser().parse_args()
    if args.command == "capacity":
        inputs = CapacityInputs(
            disk_free_bytes=int(args.disk_free_mib * 1024**2),
            raw_mib_per_day=args.raw_mib_day,
            normalized_mib_per_day=args.normalized_mib_day,
            parquet_mib_per_day=args.parquet_mib_day,
            wal_mib_per_day=args.wal_mib_day,
            measured_days=args.measured_days,
            requested_raw_hot_days=args.raw_hot_days,
            requested_normalized_hot_days=args.normalized_hot_days,
        )
        capacity_plan = plan_capacity(
            inputs
        )
        print(
            json.dumps(
                {"inputs": asdict(inputs), "plan": asdict(capacity_plan)},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return
    if args.command == "retention-plan":
        store = _store(args)
        manifests = []
        for key in args.manifest_key:
            value = store.read_bytes(key)
            manifests.append((ArchiveManifest.from_bytes(value), sha256_bytes(value)))
        retention_plan = plan_retention(
            manifests,
            now=args.now,
            hot_raw_days=args.hot_raw_days,
        )
        print(json.dumps(asdict(retention_plan), separators=(",", ":"), sort_keys=True))
        return
    asyncio.run(_export(args))


if __name__ == "__main__":
    main()
