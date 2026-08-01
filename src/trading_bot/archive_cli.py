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
from trading_bot.archive.batch import (
    DEFAULT_BATCH_MAX_WINDOWS,
    DEFAULT_BATCH_PLAN_DURATION_SECONDS,
    DEFAULT_MAX_UPLOAD_BYTES_PER_RUN,
    DEFAULT_WINDOW_SECONDS,
    HARD_BATCH_MAX_WINDOWS,
    HARD_BATCH_PLAN_DURATION_SECONDS,
    BatchArchiveError,
    BatchPlanLimits,
    BatchRunLimits,
    build_batch_plan,
    reconcile_remote_quarantine_windows,
    redacted_plan_summary,
    run_batch_plan,
    write_batch_plan,
)
from trading_bot.archive.capacity import CapacityInputs, plan_capacity
from trading_bot.archive.exporter import ArchiveExporter, ArchiveRequest
from trading_bot.archive.manifest import ArchiveManifest, sha256_bytes
from trading_bot.archive.retention import plan_retention
from trading_bot.archive.ssh_source import SshArchiveBatchReader
from trading_bot.archive.store import (
    BotoS3ArchiveStore,
    LocalArchiveStore,
    PcArchiveStore,
    S3ArchiveStore,
)
from trading_bot.archive.window import (
    DEFAULT_MAX_BUNDLE_BYTES,
    DEFAULT_MAX_DURATION_SECONDS,
    DEFAULT_MAX_ROWS,
    DEFAULT_MIN_FREE_DISK_BYTES,
    HARD_MAX_BUNDLE_BYTES,
    HARD_MAX_DURATION_SECONDS,
    HARD_MAX_ROWS,
    OPERATIONAL_DISK_FLOOR_BYTES,
    WindowExportError,
    WindowExportLimits,
    build_archive_bundle,
    load_window_events,
    upload_archive_bundle,
    verify_restore_archive,
)
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
    export_window = subparsers.add_parser("archive-export-window")
    export_window.add_argument("--start", required=True, type=datetime.fromisoformat)
    export_window.add_argument("--end", required=True, type=datetime.fromisoformat)
    export_window.add_argument("--output-dir", required=True, type=Path)
    export_window.add_argument("--provider", choices=("b2",), default="b2")
    export_window.add_argument("--confirm-upload", action="store_true")
    export_window.add_argument("--symbol", default=None)
    export_window.add_argument(
        "--max-duration-seconds",
        default=DEFAULT_MAX_DURATION_SECONDS,
        type=int,
    )
    export_window.add_argument("--max-rows", default=DEFAULT_MAX_ROWS, type=int)
    export_window.add_argument(
        "--max-bytes",
        default=DEFAULT_MAX_BUNDLE_BYTES,
        type=int,
    )
    export_window.add_argument(
        "--min-disk-bytes",
        default=DEFAULT_MIN_FREE_DISK_BYTES,
        type=int,
    )
    export_window.add_argument("--allow-quality-warnings", action="store_true")
    export_window.add_argument("--confirm-quarantine-upload", action="store_true")
    export_window.add_argument("--gap-warning-seconds", default=60.0, type=float)
    export_window.add_argument("--price-discontinuity-percent", default=20.0, type=float)
    export_window.add_argument(
        "--exchange-boundary-tolerance-seconds",
        default=5.0,
        type=float,
    )
    verify_restore = subparsers.add_parser("archive-verify-restore")
    verify_restore.add_argument("--dataset-id", required=True)
    verify_restore.add_argument("--output-dir", required=True, type=Path)
    verify_restore.add_argument("--provider", choices=("b2",), default="b2")
    verify_restore.add_argument("--gap-warning-seconds", default=60.0, type=float)
    verify_restore.add_argument("--price-discontinuity-percent", default=20.0, type=float)
    verify_restore.add_argument(
        "--exchange-boundary-tolerance-seconds",
        default=5.0,
        type=float,
    )
    batch_plan = subparsers.add_parser("archive-batch-plan")
    batch_plan.add_argument("--start", required=True, type=datetime.fromisoformat)
    batch_plan.add_argument("--end", required=True, type=datetime.fromisoformat)
    batch_plan.add_argument("--output-dir", required=True, type=Path)
    batch_plan.add_argument("--symbol", default=None)
    batch_plan.add_argument("--window-seconds", default=DEFAULT_WINDOW_SECONDS, type=int)
    batch_plan.add_argument(
        "--max-plan-duration-seconds",
        default=DEFAULT_BATCH_PLAN_DURATION_SECONDS,
        type=int,
    )
    batch_plan.add_argument("--max-rows", default=HARD_MAX_ROWS, type=int)
    batch_plan.add_argument("--max-bytes", default=HARD_MAX_BUNDLE_BYTES, type=int)
    batch_plan.add_argument(
        "--min-disk-bytes",
        default=OPERATIONAL_DISK_FLOOR_BYTES,
        type=int,
    )
    batch_run = subparsers.add_parser("archive-batch-run")
    batch_run.add_argument("--plan", required=True, type=Path)
    batch_run.add_argument("--provider", choices=("b2",), default="b2")
    batch_run.add_argument("--confirm-upload", action="store_true")
    batch_run.add_argument("--max-windows", default=DEFAULT_BATCH_MAX_WINDOWS, type=int)
    batch_run.add_argument("--output-dir", type=Path)
    batch_run.add_argument("--allow-new-attempt-after-incomplete", action="store_true")
    batch_run.add_argument(
        "--max-upload-bytes",
        default=DEFAULT_MAX_UPLOAD_BYTES_PER_RUN,
        type=int,
    )
    batch_run.add_argument(
        "--min-disk-bytes",
        default=OPERATIONAL_DISK_FLOOR_BYTES,
        type=int,
    )
    batch_run.add_argument("--allow-quality-warnings", action="store_true")
    batch_run.add_argument("--confirm-quarantine-upload", action="store_true")
    batch_run.add_argument("--gap-warning-seconds", default=60.0, type=float)
    batch_run.add_argument("--price-discontinuity-percent", default=20.0, type=float)
    batch_run.add_argument(
        "--exchange-boundary-tolerance-seconds",
        default=5.0,
        type=float,
    )
    batch_reconcile = subparsers.add_parser("archive-batch-reconcile")
    batch_reconcile.add_argument("--plan", required=True, type=Path)
    batch_reconcile.add_argument("--provider", choices=("b2",), default="b2")
    batch_reconcile.add_argument("--output-dir", type=Path)
    batch_reconcile.add_argument("--dataset-id")
    batch_reconcile.add_argument("--allow-quality-warnings", action="store_true")
    batch_reconcile.add_argument("--gap-warning-seconds", default=60.0, type=float)
    batch_reconcile.add_argument("--price-discontinuity-percent", default=20.0, type=float)
    batch_reconcile.add_argument(
        "--exchange-boundary-tolerance-seconds",
        default=5.0,
        type=float,
    )
    return parser


def _b2_store() -> BotoS3ArchiveStore:
    config = B2ArchiveConfig.from_environ()
    return S3ArchiveStore.for_b2(config)


def _batch_plan_limits(args: argparse.Namespace) -> BatchPlanLimits:
    if args.max_plan_duration_seconds > HARD_BATCH_PLAN_DURATION_SECONDS:
        raise ValueError("max-plan-duration-seconds exceeds hard cap")
    if args.max_rows > HARD_MAX_ROWS:
        raise ValueError("max-rows exceeds hard cap")
    if args.max_bytes > HARD_MAX_BUNDLE_BYTES:
        raise ValueError("max-bytes exceeds hard cap")
    if args.min_disk_bytes < OPERATIONAL_DISK_FLOOR_BYTES:
        raise ValueError(
            f"--min-disk-bytes cannot be below operational floor "
            f"({OPERATIONAL_DISK_FLOOR_BYTES})"
        )
    return BatchPlanLimits(
        max_rows=args.max_rows,
        max_bundle_bytes=args.max_bytes,
        min_free_disk_bytes=args.min_disk_bytes,
        max_plan_duration_seconds=args.max_plan_duration_seconds,
        window_seconds=args.window_seconds,
    )


def _batch_run_limits(args: argparse.Namespace) -> BatchRunLimits:
    if args.max_windows > HARD_BATCH_MAX_WINDOWS:
        raise ValueError("max-windows exceeds hard cap")
    if args.min_disk_bytes < OPERATIONAL_DISK_FLOOR_BYTES:
        raise ValueError(
            f"--min-disk-bytes cannot be below operational floor "
            f"({OPERATIONAL_DISK_FLOOR_BYTES})"
        )
    return BatchRunLimits(
        max_windows=args.max_windows,
        max_upload_bytes=args.max_upload_bytes,
        min_free_disk_bytes=args.min_disk_bytes,
        allow_quality_warnings=args.allow_quality_warnings,
        confirm_quarantine_upload=args.confirm_quarantine_upload,
        allow_new_attempt_after_incomplete=args.allow_new_attempt_after_incomplete,
        gap_warning_seconds=args.gap_warning_seconds,
        price_discontinuity_percent=args.price_discontinuity_percent,
        exchange_boundary_tolerance_seconds=args.exchange_boundary_tolerance_seconds,
    )


async def _archive_batch_plan(args: argparse.Namespace) -> dict[str, object]:
    settings = Settings()
    symbol = args.symbol or settings.hibachi_symbol
    limits = _batch_plan_limits(args)
    engine = create_engine(settings.database_url)
    try:
        plan = await build_batch_plan(
            symbol=symbol,
            start=args.start,
            end=args.end,
            window_seconds=args.window_seconds,
            limits=limits,
            session_factory=create_session_factory(engine),
        )
    finally:
        await engine.dispose()
    plan_path, sha_path = write_batch_plan(plan, args.output_dir)
    summary = redacted_plan_summary(plan, plan_path, sha_path)
    return summary


def _batch_reconcile_limits(args: argparse.Namespace) -> BatchRunLimits:
    return BatchRunLimits(
        allow_quality_warnings=args.allow_quality_warnings,
        gap_warning_seconds=args.gap_warning_seconds,
        price_discontinuity_percent=args.price_discontinuity_percent,
        exchange_boundary_tolerance_seconds=args.exchange_boundary_tolerance_seconds,
    )


async def _archive_batch_run(args: argparse.Namespace) -> dict[str, object]:
    if args.provider != "b2":
        raise ValueError("batch run requires provider b2")
    settings = Settings()
    engine = create_engine(settings.database_url)
    try:
        return await run_batch_plan(
            args.plan,
            batch_root=args.output_dir,
            store=_b2_store() if args.confirm_upload else LocalArchiveStore(
                (args.output_dir or args.plan.parent) / ".dry-run-store"
            ),
            session_factory=create_session_factory(engine),
            confirm_upload=args.confirm_upload,
            run_limits=_batch_run_limits(args),
            provider=args.provider,
        )
    finally:
        await engine.dispose()


def _archive_batch_reconcile(args: argparse.Namespace) -> dict[str, object]:
    if args.provider != "b2":
        raise ValueError("batch reconcile requires provider b2")
    return reconcile_remote_quarantine_windows(
        args.plan,
        batch_root=args.output_dir,
        store=_b2_store(),
        run_limits=_batch_reconcile_limits(args),
        dataset_id=args.dataset_id,
    )


def _window_limits(args: argparse.Namespace) -> WindowExportLimits:
    if args.max_duration_seconds > HARD_MAX_DURATION_SECONDS:
        raise ValueError("max-duration-seconds exceeds hard cap")
    if args.max_rows > HARD_MAX_ROWS:
        raise ValueError("max-rows exceeds hard cap")
    if args.max_bytes > HARD_MAX_BUNDLE_BYTES:
        raise ValueError("max-bytes exceeds hard cap")
    if args.min_disk_bytes < OPERATIONAL_DISK_FLOOR_BYTES:
        raise ValueError(
            f"--min-disk-bytes cannot be below operational floor "
            f"({OPERATIONAL_DISK_FLOOR_BYTES})"
        )
    return WindowExportLimits(
        max_duration_seconds=args.max_duration_seconds,
        max_rows=args.max_rows,
        max_bundle_bytes=args.max_bytes,
        min_free_disk_bytes=args.min_disk_bytes,
    )


async def _archive_export_window(args: argparse.Namespace) -> dict[str, object]:
    settings = Settings()
    symbol = args.symbol or settings.hibachi_symbol
    limits = _window_limits(args)
    engine = create_engine(settings.database_url)
    try:
        events = await load_window_events(
            create_session_factory(engine),
            symbol,
            args.start,
            args.end,
            max_rows=limits.max_rows,
        )
        bundle_dir = build_archive_bundle(
            symbol=symbol,
            start=args.start,
            end=args.end,
            output_dir=args.output_dir,
            events=events,
            limits=limits,
            gap_warning_seconds=args.gap_warning_seconds,
            price_discontinuity_percent=args.price_discontinuity_percent,
            exchange_boundary_tolerance_seconds=args.exchange_boundary_tolerance_seconds,
        )
    finally:
        await engine.dispose()

    summary: dict[str, object] = {
        "dataset_id": bundle_dir.name,
        "bundle_dir": str(bundle_dir),
        "row_count": len(events),
        "status": "local_ready",
    }
    if args.confirm_upload:
        if args.provider != "b2":
            raise ValueError("upload requires provider b2")
        upload_summary = upload_archive_bundle(
            bundle_dir,
            _b2_store(),
            confirm_upload=True,
            allow_quality_warnings=args.allow_quality_warnings,
            confirm_quarantine_upload=args.confirm_quarantine_upload,
            verification_root=args.output_dir / "_verification",
            gap_warning_seconds=args.gap_warning_seconds,
            price_discontinuity_percent=args.price_discontinuity_percent,
            exchange_boundary_tolerance_seconds=args.exchange_boundary_tolerance_seconds,
        )
        summary.update(upload_summary)
    else:
        dry_run = upload_archive_bundle(
            bundle_dir,
            LocalArchiveStore(args.output_dir / ".dry-run-store"),
            confirm_upload=False,
            allow_quality_warnings=args.allow_quality_warnings,
            confirm_quarantine_upload=args.confirm_quarantine_upload,
            verification_root=args.output_dir / "_verification",
        )
        summary["upload"] = dry_run
    return summary


def _archive_verify_restore(args: argparse.Namespace) -> dict[str, object]:
    if args.provider != "b2":
        raise ValueError("restore verification requires provider b2")
    return verify_restore_archive(
        _b2_store(),
        args.dataset_id,
        args.output_dir,
        gap_warning_seconds=args.gap_warning_seconds,
        price_discontinuity_percent=args.price_discontinuity_percent,
        exchange_boundary_tolerance_seconds=args.exchange_boundary_tolerance_seconds,
    )


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
    if args.command == "archive-batch-plan":
        try:
            summary = asyncio.run(_archive_batch_plan(args))
        except (BatchArchiveError, ValueError) as error:
            print(f"archive-batch-plan: {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
        return
    if args.command == "archive-batch-run":
        try:
            summary = asyncio.run(_archive_batch_run(args))
        except (BatchArchiveError, ValueError) as error:
            print(f"archive-batch-run: {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
        if summary.get("status") == "failed":
            raise SystemExit(1)
        return
    if args.command == "archive-batch-reconcile":
        try:
            summary = _archive_batch_reconcile(args)
        except (BatchArchiveError, ValueError) as error:
            print(f"archive-batch-reconcile: {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
        if summary.get("status") == "failed":
            raise SystemExit(1)
        return
    if args.command == "archive-export-window":
        try:
            summary = asyncio.run(_archive_export_window(args))
        except (WindowExportError, ValueError) as error:
            print(f"archive-export-window: {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
        if summary.get("status") in {"failed"}:
            raise SystemExit(1)
        return
    if args.command == "archive-verify-restore":
        try:
            summary = _archive_verify_restore(args)
        except (WindowExportError, ValueError) as error:
            print(f"archive-verify-restore: {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
        if summary.get("status") != "verified":
            raise SystemExit(1)
        return
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
