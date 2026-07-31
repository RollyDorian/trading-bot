"""Verified, bounded RAW and normalized Parquet archival."""

from trading_bot.archive.capacity import CapacityInputs, CapacityPlan, plan_capacity
from trading_bot.archive.exporter import ArchiveExporter, ArchiveRequest
from trading_bot.archive.manifest import ArchiveManifest
from trading_bot.archive.store import (
    ArchiveStore,
    ArchiveStoreError,
    BotoS3ArchiveStore,
    LocalArchiveStore,
    PcArchiveStore,
    S3ArchiveStore,
)
from trading_bot.archive.window import (
    OPERATIONAL_DISK_FLOOR_BYTES,
    WindowExportError,
    WindowExportLimits,
    build_archive_bundle,
    load_window_events,
    upload_archive_bundle,
    verify_restore_archive,
)

__all__ = [
    "ArchiveExporter",
    "ArchiveManifest",
    "ArchiveRequest",
    "ArchiveStore",
    "ArchiveStoreError",
    "BotoS3ArchiveStore",
    "CapacityInputs",
    "CapacityPlan",
    "LocalArchiveStore",
    "OPERATIONAL_DISK_FLOOR_BYTES",
    "PcArchiveStore",
    "S3ArchiveStore",
    "WindowExportError",
    "WindowExportLimits",
    "build_archive_bundle",
    "load_window_events",
    "plan_capacity",
    "upload_archive_bundle",
    "verify_restore_archive",
]
