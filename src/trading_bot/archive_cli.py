import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trading_bot.archive.b2 import (
    DEFAULT_SMOKE_MAX_SIZE_BYTES,
    B2ArchiveClient,
    B2ArchiveConfig,
    run_roundtrip_smoke,
)
from trading_bot.archive.capacity import CapacityInputs, plan_capacity
from trading_bot.archive.exporter import ArchiveExporter, ArchiveRequest
from trading_bot.archive.manifest import ArchiveManifest, sha256_bytes
from trading_bot.archive.retention import plan_retention
from trading_bot.archive.ssh_source import SshArchiveBatchReader
from trading_bot.archive.store import LocalArchiveStore, PcArchiveStore, S3ArchiveStore
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
    capacity.add_argument("--raw-hot-days", default=3, type=int)
    capacity.add_argument("--normalized-hot-days", default=0, type=int)
    capacity.add_argument("--allow-degraded-two-day", action="store_true")
    export = subparsers.add_parser("export-day")
    export.add_argument("--start", required=True, type=datetime.fromisoformat)
    export.add_argument("--end", required=True, type=datetime.fromisoformat)
    export.add_argument("--symbol", required=True)
    export.add_argument("--work-dir", required=True, type=Path)
    export.add_argument("--capacity-path", required=True, type=Path)
    export.add_argument("--batch-size", default=5000, type=int)
    export.add_argument("--inter-batch-delay-seconds", default=0.0, type=float)
    export.add_argument("--initial-raw-event-id", default=0, type=int)
    export.add_argument("--store", required=True, choices=("filesystem", "s3"))
    export.add_argument("--root", type=Path)
    pc_export = subparsers.add_parser("pc-export-day")
    pc_export.add_argument("--start", required=True, type=datetime.fromisoformat)
    pc_export.add_argument("--end", required=True, type=datetime.fromisoformat)
    pc_export.add_argument("--symbol", required=True)
    pc_export.add_argument("--work-dir", required=True, type=Path)
    pc_export.add_argument("--capacity-path", required=True, type=Path)
    pc_export.add_argument("--root", required=True, type=Path)
    pc_export.add_argument("--batch-size", default=1000, type=int)
    pc_export.add_argument("--inter-batch-delay-seconds", default=10.0, type=float)
    pc_export.add_argument("--initial-raw-event-id", default=0, type=int)
    pc_export.add_argument("--ssh-alias", required=True)
    pc_export.add_argument("--ssh-config", type=Path)
    pc_export.add_argument("--ssh-executable", default="ssh")
    pc_export.add_argument("--remote-project-dir", required=True)
    pc_export.add_argument("--remote-env-file", required=True)
    retention = subparsers.add_parser("retention-plan")
    retention.add_argument("--manifest-key", action="append", required=True)
    retention.add_argument("--hot-raw-days", default=3, type=int)
    retention.add_argument("--now", required=True, type=datetime.fromisoformat)
    retention.add_argument("--store", required=True, choices=("filesystem", "s3"))
    retention.add_argument("--root", type=Path)
    subparsers.add_parser("archive-check-config")
    smoke = subparsers.add_parser("archive-roundtrip-smoke")
    smoke.add_argument("--size-bytes", default=2048, type=int)
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
                inter_batch_delay_seconds=args.inter_batch_delay_seconds,
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


def _pc_summary(
    manifest: ArchiveManifest,
    store: PcArchiveStore,
    work_dir: Path,
) -> dict[str, object]:
    counts: Counter[str] = Counter()
    parquet_bytes = 0
    work_dir.mkdir(parents=True, exist_ok=True)
    work_dir.chmod(0o700)
    for item in manifest.objects:
        parquet_bytes += item.size_bytes
        if item.dataset != "raw":
            continue
        with tempfile.TemporaryDirectory(dir=work_dir) as temporary:
            path = Path(temporary) / "raw.parquet"
            store.download_file(item.key, path)
            with path.open("rb") as handle:
                parquet = pq.ParquetFile(handle)
                for batch in parquet.iter_batches(
                    batch_size=10000,
                    columns=["event_type"],
                ):
                    counts.update(str(value) for value in batch.column(0).to_pylist())
    return {
        "status": manifest.verification_status,
        "raw_rows": manifest.raw_row_count,
        "objects": len(manifest.objects),
        "parquet_bytes": parquet_bytes,
        "event_type_counts": dict(sorted(counts.items())),
        "destination": manifest.destination,
    }


async def _pc_export(args: argparse.Namespace) -> None:
    store = PcArchiveStore(args.root)
    reader = SshArchiveBatchReader(
        ssh_alias=args.ssh_alias,
        remote_project_dir=args.remote_project_dir,
        remote_env_file=args.remote_env_file,
        ssh_config=args.ssh_config,
        ssh_executable=args.ssh_executable,
    )
    exporter = ArchiveExporter(None, store, batch_reader=reader)
    manifest = await exporter.export_day(
        ArchiveRequest(
            start=args.start,
            end=args.end,
            symbol=args.symbol,
            work_dir=args.work_dir,
            capacity_path=args.capacity_path,
            batch_size=args.batch_size,
            initial_raw_event_id=args.initial_raw_event_id,
            inter_batch_delay_seconds=args.inter_batch_delay_seconds,
        )
    )
    print(
        json.dumps(
            _pc_summary(manifest, store, args.work_dir),
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _archive_check_config() -> None:
    summary = B2ArchiveConfig.from_environ().redacted_summary()
    print(json.dumps(summary, separators=(",", ":"), sort_keys=True))


def _archive_roundtrip_smoke(args: argparse.Namespace) -> None:
    if args.size_bytes <= 0 or args.size_bytes > DEFAULT_SMOKE_MAX_SIZE_BYTES:
        raise ValueError("size-bytes exceeds smoke maximum")
    config = B2ArchiveConfig.from_environ()
    client = B2ArchiveClient(config)
    with tempfile.TemporaryDirectory() as temporary:
        result = run_roundtrip_smoke(
            client,
            work_dir=Path(temporary),
            size_bytes=args.size_bytes,
            max_size_bytes=DEFAULT_SMOKE_MAX_SIZE_BYTES,
        )
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    # Fail closed when body/metadata verification did not succeed.
    if result.get("verified") is not True:
        raise SystemExit(1)


def main() -> None:
    args = _parser().parse_args()
    if args.command == "archive-check-config":
        _archive_check_config()
        return
    if args.command == "archive-roundtrip-smoke":
        try:
            _archive_roundtrip_smoke(args)
        except Exception:
            print("B2 archive smoke failed", file=sys.stderr)
            raise SystemExit(2) from None
        return
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
            allow_degraded_two_day=args.allow_degraded_two_day,
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
    if args.command == "pc-export-day":
        try:
            asyncio.run(_pc_export(args))
        except Exception:
            print("PC archive failed", file=sys.stderr)
            raise SystemExit(2) from None
        return
    asyncio.run(_export(args))


if __name__ == "__main__":
    main()
