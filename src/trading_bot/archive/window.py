"""Bounded RAW window export to immutable research bundles for remote archive.

Checksum model
--------------
* **Logical identity** (``logical_checksums.sha256``): content-only artifacts
  that exclude volatile provenance (no git SHA, no wall-clock export fields).
  Used as the stable dataset identity across rebuilds with different commits.
* **Physical checksums** (``checksums.sha256``): every immutable bundle file
  uploaded for a given attempt, including ``manifest.json``, ``provenance.json``,
  and ``logical_checksums.sha256``. Volatile provenance lives only in
  ``provenance.json`` (and manifest for research compatibility).
* **Verification reports** (``remote_verification.json``) live outside the bundle
  under ``{output_root}/_verification/{dataset_id}/`` and are never checksummed.

Upload model
------------
Objects publish under ``archives/{dataset_id}/attempts/{attempt_id}/``. A
canonical ``archives/{dataset_id}/COMPLETED`` marker is written only after
upload, remote checksum verification, and restore validation all succeed.
Partial attempts publish an ``INCOMPLETE`` marker at the attempt prefix; no
delete APIs are called.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.archive.store import ArchiveStore
from trading_bot.research.dataset import (
    SCHEMA_VERSION,
    sha256_file,
    write_dataset,
)
from trading_bot.research.quality import QUALITY_REPORT_VERSION, validate_dataset
from trading_bot.storage.models import MarketEvent

DEFAULT_MAX_DURATION_SECONDS = 3_600
HARD_MAX_DURATION_SECONDS = 6 * 3_600
DEFAULT_MAX_ROWS = 50_000
HARD_MAX_ROWS = 200_000
DEFAULT_MAX_BUNDLE_BYTES = 64 * 1024 * 1024
HARD_MAX_BUNDLE_BYTES = 256 * 1024 * 1024
OPERATIONAL_DISK_FLOOR_BYTES = 3 * 1024**3
DEFAULT_MIN_FREE_DISK_BYTES = OPERATIONAL_DISK_FLOOR_BYTES

# Content identity: excludes manifest/provenance (volatile git + export timestamps).
LOGICAL_CHECKSUM_ARTIFACTS = (
    "events.parquet",
    "candles_1s.parquet",
    "README.md",
    "quality_report.json",
    "archive_metadata.json",
)
PHYSICAL_CHECKSUM_ARTIFACTS = (
    *LOGICAL_CHECKSUM_ARTIFACTS,
    "manifest.json",
    "provenance.json",
    "logical_checksums.sha256",
)
UPLOAD_ARTIFACTS = (*PHYSICAL_CHECKSUM_ARTIFACTS, "checksums.sha256")
ARCHIVE_KEY_PREFIX = "archives"
COMPLETED_MARKER_NAME = "COMPLETED"
INCOMPLETE_MARKER_NAME = "INCOMPLETE"
VERIFICATION_DIRNAME = "_verification"
QUARANTINE_REGISTRY_KEY = f"{ARCHIVE_KEY_PREFIX}/quarantine/registry.jsonl"


class WindowExportError(ValueError):
    """Raised when a bounded archive window export cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class WindowExportLimits:
    max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS
    max_rows: int = DEFAULT_MAX_ROWS
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES
    min_free_disk_bytes: int = DEFAULT_MIN_FREE_DISK_BYTES

    def __post_init__(self) -> None:
        if not 1 <= self.max_duration_seconds <= HARD_MAX_DURATION_SECONDS:
            raise ValueError("max_duration_seconds exceeds hard cap")
        if not 1 <= self.max_rows <= HARD_MAX_ROWS:
            raise ValueError("max_rows exceeds hard cap")
        if not 1 <= self.max_bundle_bytes <= HARD_MAX_BUNDLE_BYTES:
            raise ValueError("max_bundle_bytes exceeds hard cap")
        if self.min_free_disk_bytes < OPERATIONAL_DISK_FLOOR_BYTES:
            raise ValueError(
                "min_free_disk_bytes cannot be below operational disk floor "
                f"({OPERATIONAL_DISK_FLOOR_BYTES} bytes)"
            )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise WindowExportError("window timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _validate_window(
    start: datetime,
    end: datetime,
    limits: WindowExportLimits,
) -> tuple[datetime, datetime]:
    start = _utc(start)
    end = _utc(end)
    if start >= end:
        raise WindowExportError("window end must be after start")
    duration = (end - start).total_seconds()
    if duration > limits.max_duration_seconds:
        raise WindowExportError("window duration exceeds configured maximum")
    return start, end


def _disk_free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _ensure_disk_preflight(output_dir: Path, limits: WindowExportLimits) -> None:
    """Require floor + 2× worst-case bundle for write + verification download temp."""
    output_dir.mkdir(parents=True, exist_ok=True)
    free_bytes = _disk_free_bytes(output_dir)
    required = OPERATIONAL_DISK_FLOOR_BYTES + 2 * limits.max_bundle_bytes
    if free_bytes < limits.min_free_disk_bytes:
        raise WindowExportError("insufficient free disk space for archive bundle")
    if free_bytes < required:
        raise WindowExportError(
            "insufficient free disk for bundle build and verification temp footprint"
        )


def _ensure_disk_post_build(output_dir: Path, bundle_bytes: int) -> None:
    """After bundle write, reserve floor + one verification download copy."""
    free_bytes = _disk_free_bytes(output_dir)
    required = OPERATIONAL_DISK_FLOOR_BYTES + bundle_bytes
    if free_bytes < required:
        raise WindowExportError(
            "insufficient free disk after bundle build for verification temp"
        )


def _bundle_size_bytes(bundle_dir: Path) -> int:
    return sum(path.stat().st_size for path in bundle_dir.iterdir() if path.is_file())


def _write_archive_metadata(
    bundle_dir: Path,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    events: list[MarketEvent],
    manifest: dict[str, Any],
    quality_report: dict[str, Any],
) -> dict[str, Any]:
    source_counts = Counter(event.source for event in events)
    topic_counts = Counter(event.event_type for event in events)
    research_quality_status = str(quality_report.get("status", "rejected"))
    quarantined = research_quality_status == "rejected"
    admission_eligible = research_quality_status == "pass"
    quarantine_reasons = (
        list(quality_report.get("findings", [])) if quarantined else []
    )
    metadata = {
        "schema_version": manifest["schema_version"],
        "dataset_id": manifest["dataset_id"],
        "symbol": symbol,
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
        "row_counts": manifest["row_counts"],
        "sources": {name: source_counts[name] for name in sorted(source_counts)},
        "topics": {name: topic_counts[name] for name in sorted(topic_counts)},
        "quarantined": quarantined,
        "research_quality_status": research_quality_status,
        "admission_eligible": admission_eligible,
        "quarantine_reasons": quarantine_reasons,
    }
    path = bundle_dir / "archive_metadata.json"
    path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return metadata


def _write_provenance(bundle_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Volatile export provenance; excluded from logical checksum identity."""
    software = manifest.get("software", {})
    provenance = {
        "dataset_id": manifest["dataset_id"],
        "schema_version": manifest["schema_version"],
        "exported_at_utc": manifest.get("exported_at_utc"),
        "git_commit": software.get("git_commit"),
        "tool": "hibachi-archive",
        "tool_version": software.get("version", "0.1.0"),
    }
    path = bundle_dir / "provenance.json"
    path.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return provenance


def _logical_artifact_digest(name: str, path: Path) -> str:
    """Hash logical identity; strip provenance-volatile fields where needed."""
    if name == "quality_report.json":
        report = cast(
            dict[str, Any],
            json.loads(path.read_text(encoding="utf-8")),
        )
        # manifest_sha256 and validated_at_utc tie to export provenance, not market content.
        volatile = {"manifest_sha256", "validated_at_utc"}
        normalized = {key: value for key, value in report.items() if key not in volatile}
        payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return sha256_file(path)


def _write_checksum_file(
    bundle_dir: Path,
    *,
    filename: str,
    artifact_names: tuple[str, ...],
    logical: bool,
) -> dict[str, str]:
    digests: dict[str, str] = {}
    for name in artifact_names:
        path = bundle_dir / name
        if not path.is_file():
            raise WindowExportError(f"missing checksum artifact: {name}")
        digests[name] = (
            _logical_artifact_digest(name, path) if logical else sha256_file(path)
        )
    lines = [f"{digests[name]}  {name}" for name in sorted(digests)]
    checksum_path = bundle_dir / filename
    checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return digests


def _write_logical_checksums(bundle_dir: Path) -> dict[str, str]:
    return _write_checksum_file(
        bundle_dir,
        filename="logical_checksums.sha256",
        artifact_names=LOGICAL_CHECKSUM_ARTIFACTS,
        logical=True,
    )


def _write_physical_checksums(bundle_dir: Path) -> dict[str, str]:
    return _write_checksum_file(
        bundle_dir,
        filename="checksums.sha256",
        artifact_names=PHYSICAL_CHECKSUM_ARTIFACTS,
        logical=False,
    )


def _read_checksum_file(bundle_dir: Path, filename: str, expected: set[str]) -> dict[str, str]:
    checksum_path = bundle_dir / filename
    if not checksum_path.is_file():
        raise WindowExportError(f"{filename} is missing")
    digests: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        if not digest or not name:
            raise WindowExportError(f"{filename} line is invalid")
        digests[name] = digest
    if set(digests) != expected:
        raise WindowExportError(f"{filename} artifact list is unexpected")
    return digests


def _read_logical_checksums(bundle_dir: Path) -> dict[str, str]:
    return _read_checksum_file(
        bundle_dir,
        "logical_checksums.sha256",
        set(LOGICAL_CHECKSUM_ARTIFACTS),
    )


def _read_physical_checksums(bundle_dir: Path) -> dict[str, str]:
    return _read_checksum_file(
        bundle_dir,
        "checksums.sha256",
        set(PHYSICAL_CHECKSUM_ARTIFACTS),
    )


def _verify_checksums(
    bundle_dir: Path,
    digests: dict[str, str],
    *,
    logical: bool,
) -> dict[str, bool]:
    verified: dict[str, bool] = {}
    for name, expected in digests.items():
        path = bundle_dir / name
        if not path.is_file():
            verified[name] = False
            continue
        actual = _logical_artifact_digest(name, path) if logical else sha256_file(path)
        verified[name] = actual == expected
    return verified


def _new_attempt_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}_{secrets.token_hex(4)}"


def _canonical_prefix(dataset_id: str) -> str:
    return f"{ARCHIVE_KEY_PREFIX}/{dataset_id}"


def _attempt_prefix(dataset_id: str, attempt_id: str) -> str:
    return f"{_canonical_prefix(dataset_id)}/attempts/{attempt_id}"


def _attempt_key(dataset_id: str, attempt_id: str, filename: str) -> str:
    return f"{_attempt_prefix(dataset_id, attempt_id)}/{filename}"


def _completed_key(dataset_id: str) -> str:
    return f"{_canonical_prefix(dataset_id)}/{COMPLETED_MARKER_NAME}"


def _incomplete_key(dataset_id: str, attempt_id: str) -> str:
    return f"{_attempt_prefix(dataset_id, attempt_id)}/{INCOMPLETE_MARKER_NAME}"


def _verification_report_path(verification_root: Path, dataset_id: str) -> Path:
    return verification_root / dataset_id / "remote_verification.json"


def _restore_validation_report_path(verification_root: Path, dataset_id: str) -> Path:
    return verification_root / dataset_id / "restore_validation.json"


def _write_restore_validation_report(
    verification_root: Path,
    dataset_id: str,
    summary: dict[str, Any],
) -> Path:
    """Persist derived restore validation outside the immutable bundle directory."""
    path = _restore_validation_report_path(verification_root, dataset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _write_verification_report(
    verification_root: Path,
    dataset_id: str,
    summary: dict[str, Any],
) -> Path:
    path = _verification_report_path(verification_root, dataset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _publish_incomplete_marker(
    store: ArchiveStore,
    dataset_id: str,
    attempt_id: str,
    summary: dict[str, Any],
) -> None:
    payload = {
        "status": INCOMPLETE_MARKER_NAME,
        "dataset_id": dataset_id,
        "attempt_id": attempt_id,
        "uploaded_keys": summary.get("uploaded_keys", []),
        "error": summary.get("error"),
    }
    key = _incomplete_key(dataset_id, attempt_id)
    if not store.exists(key):
        store.publish_bytes(
            key,
            (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )


async def load_window_events(
    session_factory: async_sessionmaker[AsyncSession],
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    max_rows: int,
) -> list[MarketEvent]:
    start = _utc(start)
    end = _utc(end)
    if start >= end:
        raise WindowExportError("window end must be after start")
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(MarketEvent)
            .where(
                MarketEvent.symbol == symbol,
                MarketEvent.received_at >= start,
                MarketEvent.received_at < end,
            )
        )
        if count is None:
            count = 0
        if count > max_rows:
            raise WindowExportError("window row count exceeds configured maximum")
        statement = (
            select(MarketEvent)
            .where(
                MarketEvent.symbol == symbol,
                MarketEvent.received_at >= start,
                MarketEvent.received_at < end,
            )
            .order_by(MarketEvent.received_at, MarketEvent.id)
        )
        return list((await session.scalars(statement)).all())


def build_archive_bundle(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    output_dir: Path,
    events: list[MarketEvent] | None = None,
    limits: WindowExportLimits | None = None,
    gap_warning_seconds: float = 60.0,
    price_discontinuity_percent: float = 20.0,
    exchange_boundary_tolerance_seconds: float = 5.0,
) -> Path:
    """Build a deterministic local research bundle with schema-5 quality evidence."""
    limits = limits or WindowExportLimits()
    start, end = _validate_window(start, end, limits)
    _ensure_disk_preflight(output_dir, limits)
    if events is None:
        raise WindowExportError("events are required for bundle build")
    if len(events) > limits.max_rows:
        raise WindowExportError("event count exceeds configured maximum")

    # Real git commit is recorded in manifest.json (research compatibility).
    dataset_dir = write_dataset(
        events=events,
        symbol=symbol,
        start=start,
        end=end,
        output_root=output_dir,
    )
    quality_report = validate_dataset(
        dataset_dir,
        gap_warning_seconds=gap_warning_seconds,
        price_discontinuity_percent=price_discontinuity_percent,
        exchange_boundary_tolerance_seconds=exchange_boundary_tolerance_seconds,
        now=end,
    )
    if quality_report.get("quality_report_version") != QUALITY_REPORT_VERSION:
        raise WindowExportError("quality_report_version must be schema 5")

    manifest = cast(
        dict[str, Any],
        json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8")),
    )
    _write_archive_metadata(
        dataset_dir,
        symbol=symbol,
        start=start,
        end=end,
        events=events,
        manifest=manifest,
        quality_report=quality_report,
    )
    _write_provenance(dataset_dir, manifest)
    _write_logical_checksums(dataset_dir)
    _write_physical_checksums(dataset_dir)

    bundle_bytes = _bundle_size_bytes(dataset_dir)
    if bundle_bytes > limits.max_bundle_bytes:
        raise WindowExportError("bundle size exceeds configured maximum")
    _ensure_disk_post_build(output_dir, bundle_bytes)
    return dataset_dir


def _read_archive_metadata(bundle_dir: Path) -> dict[str, Any]:
    metadata_path = bundle_dir / "archive_metadata.json"
    if not metadata_path.is_file():
        raise WindowExportError("archive_metadata.json is missing")
    return cast(dict[str, Any], json.loads(metadata_path.read_text(encoding="utf-8")))


def _read_quality_report(bundle_dir: Path) -> dict[str, Any]:
    quality_path = bundle_dir / "quality_report.json"
    if not quality_path.is_file():
        raise WindowExportError("quality_report.json is missing")
    return cast(dict[str, Any], json.loads(quality_path.read_text(encoding="utf-8")))


def _quality_blocks_upload(
    quality_report: dict[str, Any],
    *,
    allow_quality_warnings: bool,
    confirm_quarantine_upload: bool,
) -> str | None:
    if quality_report.get("quality_report_version") != QUALITY_REPORT_VERSION:
        return "quality_report_version must be schema 5"
    status = quality_report.get("status")
    if status == "rejected":
        if confirm_quarantine_upload:
            return None
        return "quality status rejected blocks upload"
    if status == "warning" and not allow_quality_warnings:
        return "quality status warning requires allow_quality_warnings"
    if status not in {"pass", "warning", "rejected"}:
        return "quality status is invalid"
    return None


def _quarantine_metadata_matches_rejected(metadata: dict[str, Any]) -> bool:
    return (
        metadata.get("quarantined") is True
        and metadata.get("admission_eligible") is False
        and metadata.get("research_quality_status") == "rejected"
    )


def _upload_eligibility_from_quality(
    quality_report: dict[str, Any],
    archive_metadata: dict[str, Any],
) -> tuple[Any, bool, bool]:
    """Derive upload summary/COMPLETED eligibility from quality report at upload time."""
    report_status = quality_report.get("status")
    if report_status == "rejected":
        return "rejected", True, False
    return (
        archive_metadata.get("research_quality_status"),
        bool(archive_metadata.get("quarantined")),
        bool(archive_metadata.get("admission_eligible")),
    )


def _append_quarantine_registry(
    store: ArchiveStore,
    *,
    dataset_id: str,
    attempt_id: str,
    metadata: dict[str, Any],
    logical_checksums_sha256: str,
) -> None:
    record = {
        "dataset_id": dataset_id,
        "attempt_id": attempt_id,
        "completed_at": datetime.now(UTC).isoformat(),
        "research_quality_status": metadata.get("research_quality_status"),
        "admission_eligible": False,
        "quarantine_reasons": metadata.get("quarantine_reasons", []),
        "logical_checksums_sha256": logical_checksums_sha256,
        "row_counts": metadata.get("row_counts"),
    }
    line = json.dumps(record, sort_keys=True) + "\n"
    store.append_bytes(QUARANTINE_REGISTRY_KEY, line.encode("utf-8"))


def _restore_validate_bundle(
    bundle_dir: Path,
    *,
    gap_warning_seconds: float,
    price_discontinuity_percent: float,
    exchange_boundary_tolerance_seconds: float,
) -> dict[str, Any]:
    physical = _read_physical_checksums(bundle_dir)
    physical_results = _verify_checksums(bundle_dir, physical, logical=False)
    if not all(physical_results.values()):
        return {
            "status": "failed",
            "error": "physical checksum verification failed",
            "checksum_results": physical_results,
        }
    logical = _read_logical_checksums(bundle_dir)
    logical_results = _verify_checksums(bundle_dir, logical, logical=True)
    if not all(logical_results.values()):
        return {
            "status": "failed",
            "error": "logical checksum verification failed",
            "checksum_results": logical_results,
        }
    # Restore validation must not mutate immutable bundle bytes (checksums.sha256).
    quality_report = validate_dataset(
        bundle_dir,
        gap_warning_seconds=gap_warning_seconds,
        price_discontinuity_percent=price_discontinuity_percent,
        exchange_boundary_tolerance_seconds=exchange_boundary_tolerance_seconds,
        write_report=False,
    )
    if quality_report.get("quality_report_version") != QUALITY_REPORT_VERSION:
        return {
            "status": "failed",
            "error": "quality_report_version must be schema 5",
        }
    return {
        "status": "verified",
        "quality_status": quality_report.get("status"),
        "physical_checksum_results": physical_results,
        "logical_checksum_results": logical_results,
    }


def upload_archive_bundle(
    bundle_dir: Path,
    store: ArchiveStore,
    *,
    confirm_upload: bool,
    allow_quality_warnings: bool = False,
    confirm_quarantine_upload: bool = False,
    verification_root: Path | None = None,
    gap_warning_seconds: float = 60.0,
    price_discontinuity_percent: float = 20.0,
    exchange_boundary_tolerance_seconds: float = 5.0,
) -> dict[str, Any]:
    dataset_id = bundle_dir.name
    attempt_id = _new_attempt_id()
    attempt_prefix = _attempt_prefix(dataset_id, attempt_id)
    verification_dir = verification_root or (bundle_dir.parent / VERIFICATION_DIRNAME)
    archive_metadata = _read_archive_metadata(bundle_dir)
    block_reason: str | None
    try:
        quality_report = _read_quality_report(bundle_dir)
    except WindowExportError as error:
        block_reason = str(error)
        quality_report = {}
        quality_status, quarantined, admission_eligible = _upload_eligibility_from_quality(
            quality_report,
            archive_metadata,
        )
    else:
        block_reason = _quality_blocks_upload(
            quality_report,
            allow_quality_warnings=allow_quality_warnings,
            confirm_quarantine_upload=confirm_quarantine_upload,
        )
        if (
            block_reason is None
            and quality_report.get("status") == "rejected"
            and confirm_quarantine_upload
            and not _quarantine_metadata_matches_rejected(archive_metadata)
        ):
            block_reason = "quarantine metadata inconsistent with rejected quality"
        quality_status, quarantined, admission_eligible = _upload_eligibility_from_quality(
            quality_report,
            archive_metadata,
        )
    logical_digests = _read_logical_checksums(bundle_dir)
    logical_checksums_sha256 = sha256_file(bundle_dir / "logical_checksums.sha256")
    summary: dict[str, Any] = {
        "dataset_id": dataset_id,
        "attempt_id": attempt_id,
        "attempt_prefix": attempt_prefix,
        "destination": store.destination_label,
        "confirm_upload": confirm_upload,
        "confirm_quarantine_upload": confirm_quarantine_upload,
        "quality_status": quality_status,
        "quarantined": quarantined,
        "admission_eligible": admission_eligible,
        "logical_checksums_sha256": logical_checksums_sha256,
        "artifacts": list(UPLOAD_ARTIFACTS),
        "uploaded_keys": [],
        "status": "dry_run",
    }
    if block_reason is not None:
        summary["status"] = "failed"
        summary["error"] = block_reason
        return summary
    if not confirm_upload:
        if quarantined and confirm_quarantine_upload:
            summary["message"] = (
                "local quarantined bundle ready; pass --confirm-upload to publish"
            )
        else:
            summary["message"] = "local bundle ready; pass --confirm-upload to publish"
        return summary

    completed_key = _completed_key(dataset_id)
    if store.exists(completed_key):
        summary["status"] = "failed"
        summary["error"] = "canonical COMPLETED marker already exists"
        summary["completed_key"] = completed_key
        return summary

    remote_keys = [_attempt_key(dataset_id, attempt_id, name) for name in UPLOAD_ARTIFACTS]
    existing = [key for key in remote_keys if store.exists(key)]
    if existing:
        summary["status"] = "failed"
        summary["error"] = "remote object already exists in attempt prefix"
        summary["existing_keys"] = existing
        return summary

    uploaded: list[str] = []
    try:
        for name in UPLOAD_ARTIFACTS:
            key = _attempt_key(dataset_id, attempt_id, name)
            store.publish_file(key, bundle_dir / name)
            uploaded.append(key)
    except Exception as error:
        summary["status"] = "failed"
        summary["error"] = str(error)
        summary["uploaded_keys"] = uploaded
        summary["orphan_risk"] = bool(uploaded)
        _publish_incomplete_marker(store, dataset_id, attempt_id, summary)
        summary["verification_report"] = str(
            _write_verification_report(verification_dir, dataset_id, summary)
        )
        return summary

    physical_digests = _read_physical_checksums(bundle_dir)
    verified_entries: dict[str, Any] = {}
    all_verified = True
    with tempfile.TemporaryDirectory() as temporary:
        temp_dir = Path(temporary)
        for name in PHYSICAL_CHECKSUM_ARTIFACTS:
            key = _attempt_key(dataset_id, attempt_id, name)
            destination = temp_dir / name
            store.download_file(key, destination)
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            matches = digest == physical_digests[name]
            verified_entries[name] = {
                "key": key,
                "sha256": digest,
                "verified": matches,
            }
            if not matches:
                all_verified = False

    summary["uploaded_keys"] = uploaded
    summary["verified_entries"] = verified_entries
    if not all_verified:
        summary["status"] = "failed"
        summary["error"] = "remote checksum verification failed"
        _publish_incomplete_marker(store, dataset_id, attempt_id, summary)
        summary["verification_report"] = str(
            _write_verification_report(verification_dir, dataset_id, summary)
        )
        return summary

    with tempfile.TemporaryDirectory() as temporary:
        restore_dir = Path(temporary) / dataset_id
        restore_dir.mkdir(parents=True)
        for name in UPLOAD_ARTIFACTS:
            store.download_file(
                _attempt_key(dataset_id, attempt_id, name),
                restore_dir / name,
            )
        restore_result = _restore_validate_bundle(
            restore_dir,
            gap_warning_seconds=gap_warning_seconds,
            price_discontinuity_percent=price_discontinuity_percent,
            exchange_boundary_tolerance_seconds=exchange_boundary_tolerance_seconds,
        )
    if restore_result.get("status") != "verified":
        summary["status"] = "failed"
        summary["error"] = restore_result.get("error", "restore validation failed")
        summary["restore_result"] = restore_result
        _publish_incomplete_marker(store, dataset_id, attempt_id, summary)
        summary["verification_report"] = str(
            _write_verification_report(verification_dir, dataset_id, summary)
        )
        return summary

    summary["restore_validation_report"] = str(
        _write_restore_validation_report(
            verification_dir,
            dataset_id,
            {
                "dataset_id": dataset_id,
                "attempt_id": attempt_id,
                "status": restore_result.get("status"),
                "quality_status": restore_result.get("quality_status"),
                "physical_checksum_results": restore_result.get(
                    "physical_checksum_results"
                ),
                "logical_checksum_results": restore_result.get(
                    "logical_checksum_results"
                ),
            },
        )
    )

    completed_payload = {
        "status": COMPLETED_MARKER_NAME,
        "dataset_id": dataset_id,
        "attempt_id": attempt_id,
        "attempt_prefix": attempt_prefix,
        "logical_checksums_sha256": logical_checksums_sha256,
        "logical_artifacts": logical_digests,
        "completed_at_utc": datetime.now(UTC).isoformat(),
        "quarantined": quarantined,
        "admission_eligible": admission_eligible,
        "research_quality_status": quality_status,
    }
    if quarantined:
        try:
            _append_quarantine_registry(
                store,
                dataset_id=dataset_id,
                attempt_id=attempt_id,
                metadata=archive_metadata,
                logical_checksums_sha256=logical_checksums_sha256,
            )
        except Exception as error:
            summary["status"] = "failed"
            summary["error"] = f"quarantine registry append failed: {error}"
            _publish_incomplete_marker(store, dataset_id, attempt_id, summary)
            summary["verification_report"] = str(
                _write_verification_report(verification_dir, dataset_id, summary)
            )
            return summary
    store.publish_bytes(
        completed_key,
        (json.dumps(completed_payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    summary["completed_key"] = completed_key
    summary["status"] = "verified"
    summary["restore_result"] = restore_result
    summary["verification_report"] = str(
        _write_verification_report(verification_dir, dataset_id, summary)
    )
    return summary


def _load_completed_marker(store: ArchiveStore, dataset_id: str) -> dict[str, Any]:
    completed_key = _completed_key(dataset_id)
    if not store.exists(completed_key):
        raise WindowExportError("canonical COMPLETED marker is missing")
    payload = cast(
        dict[str, Any],
        json.loads(store.read_bytes(completed_key).decode("utf-8")),
    )
    if payload.get("status") != COMPLETED_MARKER_NAME:
        raise WindowExportError("canonical marker is not COMPLETED")
    if not payload.get("attempt_id"):
        raise WindowExportError("COMPLETED marker missing attempt_id")
    return payload


def verify_restore_archive(
    store: ArchiveStore,
    dataset_id: str,
    work_dir: Path,
    *,
    gap_warning_seconds: float = 60.0,
    price_discontinuity_percent: float = 20.0,
    exchange_boundary_tolerance_seconds: float = 5.0,
) -> dict[str, Any]:
    """Download a completed remote attempt and verify it read-only.

    Checksums are verified and ``validate_dataset`` runs with ``write_report=False``
    so bundle bytes stay immutable. A derived ``restore_validation.json`` is written
    only under ``{work_dir}/_verification/{dataset_id}/``, never inside the dataset
    directory.
    """
    try:
        completed = _load_completed_marker(store, dataset_id)
    except WindowExportError as error:
        return {
            "dataset_id": dataset_id,
            "status": "failed",
            "error": str(error),
        }

    attempt_id = str(completed["attempt_id"])
    restore_dir = work_dir / dataset_id
    if restore_dir.exists():
        raise WindowExportError("restore target already exists")
    restore_dir.mkdir(parents=True, exist_ok=True)

    downloaded: list[str] = []
    try:
        for name in UPLOAD_ARTIFACTS:
            key = _attempt_key(dataset_id, attempt_id, name)
            if not store.exists(key):
                return {
                    "dataset_id": dataset_id,
                    "status": "failed",
                    "error": f"missing remote object: {key}",
                    "downloaded_keys": downloaded,
                    "attempt_id": attempt_id,
                }
            store.download_file(key, restore_dir / name)
            downloaded.append(key)
    except Exception as error:
        return {
            "dataset_id": dataset_id,
            "status": "failed",
            "error": str(error),
            "downloaded_keys": downloaded,
            "attempt_id": attempt_id,
        }

    restore_result = _restore_validate_bundle(
        restore_dir,
        gap_warning_seconds=gap_warning_seconds,
        price_discontinuity_percent=price_discontinuity_percent,
        exchange_boundary_tolerance_seconds=exchange_boundary_tolerance_seconds,
    )
    if restore_result.get("status") != "verified":
        return {
            "dataset_id": dataset_id,
            "status": "failed",
            "error": restore_result.get("error"),
            "attempt_id": attempt_id,
            "downloaded_keys": downloaded,
            **restore_result,
        }

    expected_logical = completed.get("logical_checksums_sha256")
    actual_logical = sha256_file(restore_dir / "logical_checksums.sha256")
    if expected_logical and actual_logical != expected_logical:
        return {
            "dataset_id": dataset_id,
            "status": "failed",
            "error": "logical checksum identity mismatch vs COMPLETED marker",
            "attempt_id": attempt_id,
            "downloaded_keys": downloaded,
        }

    verification_root = work_dir / VERIFICATION_DIRNAME
    restore_validation_report = _write_restore_validation_report(
        verification_root,
        dataset_id,
        {
            "dataset_id": dataset_id,
            "attempt_id": attempt_id,
            "status": "verified",
            "quality_status": restore_result.get("quality_status"),
            "physical_checksum_results": restore_result.get("physical_checksum_results"),
            "logical_checksum_results": restore_result.get("logical_checksum_results"),
            "logical_checksums_sha256": actual_logical,
            "downloaded_keys": downloaded,
        },
    )

    return {
        "dataset_id": dataset_id,
        "status": "verified",
        "attempt_id": attempt_id,
        "quality_status": restore_result.get("quality_status"),
        "physical_checksum_results": restore_result.get("physical_checksum_results"),
        "logical_checksum_results": restore_result.get("logical_checksum_results"),
        "downloaded_keys": downloaded,
        "schema_version": SCHEMA_VERSION,
        "logical_checksums_sha256": actual_logical,
        "restore_validation_report": str(restore_validation_report),
    }
