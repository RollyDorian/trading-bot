"""Bounded multi-window batch archive planning and resumable execution.

Plans are immutable JSON with a SHA-256 sidecar. Runs persist progress under
``{batch_root}/_batch/{plan_id}/`` and never invoke retention or delete APIs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.archive.store import ArchiveStore
from trading_bot.archive.window import (
    ARCHIVE_KEY_PREFIX,
    HARD_MAX_BUNDLE_BYTES,
    HARD_MAX_ROWS,
    INCOMPLETE_MARKER_NAME,
    OPERATIONAL_DISK_FLOOR_BYTES,
    VERIFICATION_DIRNAME,
    WindowExportError,
    WindowExportLimits,
    _completed_key,
    build_archive_bundle,
    load_window_events,
    upload_archive_bundle,
    verify_restore_archive,
)
from trading_bot.research.dataset import (
    _git_commit,
    generate_dataset_id,
    sha256_file,
)
from trading_bot.storage.models import MarketEvent

BATCH_SCHEMA_VERSION = 1
PLAN_FILENAME = "archive_batch_plan.json"
PLAN_SHA256_FILENAME = "plan.sha256"

DEFAULT_BATCH_MAX_WINDOWS = 3
HARD_BATCH_MAX_WINDOWS = 24
DEFAULT_BATCH_PLAN_DURATION_SECONDS = 6 * 3600
HARD_BATCH_PLAN_DURATION_SECONDS = 24 * 3600
DEFAULT_WINDOW_SECONDS = 3600
DEFAULT_MAX_UPLOAD_BYTES_PER_RUN = 3 * HARD_MAX_BUNDLE_BYTES

WINDOW_STATE_PENDING = "pending"
WINDOW_STATE_RUNNING = "running"
WINDOW_STATE_COMPLETED_ADMISSIBLE = "completed_admissible"
WINDOW_STATE_COMPLETED_QUARANTINED = "completed_quarantined"
WINDOW_STATE_SKIPPED_VERIFIED = "skipped_verified"
WINDOW_STATE_SKIPPED_QUARANTINED = "skipped_quarantined"
WINDOW_STATE_FAILED_STORAGE = "failed_storage"

# Backward-compatible aliases and legacy progress-file values.
WINDOW_STATE_COMPLETED = WINDOW_STATE_COMPLETED_ADMISSIBLE
WINDOW_STATE_FAILED = WINDOW_STATE_FAILED_STORAGE
LEGACY_WINDOW_STATE_COMPLETED = "completed"
LEGACY_WINDOW_STATE_FAILED = "failed"

STORAGE_COMPLETE_WINDOW_STATES = frozenset(
    {
        WINDOW_STATE_COMPLETED_ADMISSIBLE,
        WINDOW_STATE_COMPLETED_QUARANTINED,
        WINDOW_STATE_SKIPPED_VERIFIED,
        WINDOW_STATE_SKIPPED_QUARANTINED,
        LEGACY_WINDOW_STATE_COMPLETED,
    }
)
ADMISSIBLE_WINDOW_STATES = frozenset(
    {
        WINDOW_STATE_COMPLETED_ADMISSIBLE,
        WINDOW_STATE_SKIPPED_VERIFIED,
        LEGACY_WINDOW_STATE_COMPLETED,
    }
)

RUN_STATUS_PARTIAL = "partial"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_PASS = "pass"
RUN_STATUS_FAILED = "failed"

WindowCountProvider = Callable[[str, datetime, datetime], Awaitable[tuple[int, int]]]


class BatchArchiveError(ValueError):
    """Raised when batch archive planning or execution cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class BatchPlanLimits:
    max_rows: int = HARD_MAX_ROWS
    max_bundle_bytes: int = HARD_MAX_BUNDLE_BYTES
    min_free_disk_bytes: int = OPERATIONAL_DISK_FLOOR_BYTES
    max_plan_duration_seconds: int = DEFAULT_BATCH_PLAN_DURATION_SECONDS
    window_seconds: int = DEFAULT_WINDOW_SECONDS

    def __post_init__(self) -> None:
        if not 1 <= self.max_plan_duration_seconds <= HARD_BATCH_PLAN_DURATION_SECONDS:
            raise ValueError("max_plan_duration_seconds exceeds hard cap")
        if self.min_free_disk_bytes < OPERATIONAL_DISK_FLOOR_BYTES:
            raise ValueError("min_free_disk_bytes cannot be below operational disk floor")
        if self.window_seconds < 1:
            raise ValueError("window_seconds must be positive")


@dataclass(frozen=True, slots=True)
class BatchRunLimits:
    max_windows: int = DEFAULT_BATCH_MAX_WINDOWS
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES_PER_RUN
    min_free_disk_bytes: int = OPERATIONAL_DISK_FLOOR_BYTES
    allow_quality_warnings: bool = False
    confirm_quarantine_upload: bool = False
    allow_new_attempt_after_incomplete: bool = False
    gap_warning_seconds: float = 60.0
    price_discontinuity_percent: float = 20.0
    exchange_boundary_tolerance_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_windows <= HARD_BATCH_MAX_WINDOWS:
            raise ValueError("max_windows exceeds hard cap")
        if self.min_free_disk_bytes < OPERATIONAL_DISK_FLOOR_BYTES:
            raise ValueError("min_free_disk_bytes cannot be below operational disk floor")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise BatchArchiveError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _compact_timestamp(value: datetime) -> str:
    return _utc(value).strftime("%Y%m%dT%H%M%S%fZ")


def _plan_id(symbol: str, start: datetime, end: datetime, window_seconds: int) -> str:
    safe_symbol = symbol.lower().replace("/", "-")
    return (
        f"batch_{safe_symbol}_{_compact_timestamp(start)}_"
        f"{_compact_timestamp(end)}_w{window_seconds}"
    )


def _validate_span(
    start: datetime,
    end: datetime,
    window_seconds: int,
    limits: BatchPlanLimits,
) -> tuple[datetime, datetime, int]:
    start = _utc(start)
    end = _utc(end)
    if start >= end:
        raise BatchArchiveError("plan end must be after start")
    span_seconds = (end - start).total_seconds()
    if span_seconds <= 0:
        raise BatchArchiveError("plan span must be positive")
    if span_seconds % window_seconds != 0:
        raise BatchArchiveError(
            "plan span must be an exact multiple of window_seconds"
        )
    window_count = int(span_seconds // window_seconds)
    if window_count > HARD_BATCH_MAX_WINDOWS:
        raise BatchArchiveError("window count exceeds hard maximum")
    if span_seconds > limits.max_plan_duration_seconds:
        raise BatchArchiveError("plan duration exceeds configured maximum")
    if window_count < 1:
        raise BatchArchiveError("plan must contain at least one window")
    return start, end, window_count


def _build_windows(
    symbol: str,
    start: datetime,
    end: datetime,
    window_seconds: int,
    counts: list[tuple[int, int]],
) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    cursor = start
    index = 0
    while cursor < end:
        window_end = cursor + timedelta(seconds=window_seconds)
        event_count, trade_count = counts[index]
        windows.append(
            {
                "index": index,
                "start_utc": cursor.isoformat(),
                "end_utc": window_end.isoformat(),
                "dataset_id": generate_dataset_id(symbol, cursor, window_end),
                "expected_event_count": event_count,
                "expected_trade_count": trade_count,
            }
        )
        cursor = window_end
        index += 1
    return windows


async def count_window_events(
    session_factory: async_sessionmaker[AsyncSession],
    symbol: str,
    start: datetime,
    end: datetime,
) -> tuple[int, int]:
    """Count total and trade events in a half-open ``[start, end)`` window."""
    start = _utc(start)
    end = _utc(end)
    async with session_factory() as session:
        total = await session.scalar(
            select(func.count())
            .select_from(MarketEvent)
            .where(
                MarketEvent.symbol == symbol,
                MarketEvent.received_at >= start,
                MarketEvent.received_at < end,
            )
        )
        trades = await session.scalar(
            select(func.count())
            .select_from(MarketEvent)
            .where(
                MarketEvent.symbol == symbol,
                MarketEvent.received_at >= start,
                MarketEvent.received_at < end,
                MarketEvent.event_type == "trades",
            )
        )
    return int(total or 0), int(trades or 0)


async def build_batch_plan(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    limits: BatchPlanLimits | None = None,
    count_provider: WindowCountProvider | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, Any]:
    limits = limits or BatchPlanLimits(window_seconds=window_seconds)
    start, end, window_count = _validate_span(start, end, window_seconds, limits)

    if count_provider is None:
        if session_factory is None:
            raise BatchArchiveError("session_factory or count_provider is required")

        async def _default_provider(sym: str, s: datetime, e: datetime) -> tuple[int, int]:
            return await count_window_events(session_factory, sym, s, e)

        count_provider = _default_provider

    counts: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        window_end = cursor + timedelta(seconds=window_seconds)
        counts.append(await count_provider(symbol, cursor, window_end))
        cursor = window_end

    range_events = sum(item[0] for item in counts)
    range_trades = sum(item[1] for item in counts)
    windows = _build_windows(symbol, start, end, window_seconds, counts)

    # Boundary continuity is structural; reject gaps/overlaps at plan time.
    for left, right in zip(windows, windows[1:], strict=False):
        if left["end_utc"] != right["start_utc"]:
            raise BatchArchiveError("windows are not contiguous")

    plan = {
        "plan_id": _plan_id(symbol, start, end, window_seconds),
        "schema_version": BATCH_SCHEMA_VERSION,
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
        "window_seconds": window_seconds,
        "symbol": symbol,
        "windows": windows,
        "range_expected_event_count": range_events,
        "range_expected_trade_count": range_trades,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "created_with_git_commit": _git_commit(),
        "limits": {
            "max_rows": limits.max_rows,
            "max_bundle_bytes": limits.max_bundle_bytes,
            "min_free_disk_bytes": limits.min_free_disk_bytes,
            "max_plan_duration_seconds": limits.max_plan_duration_seconds,
            "window_seconds": window_seconds,
        },
    }
    return plan


def write_batch_plan(plan: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    """Write immutable plan JSON and SHA-256 sidecar; refuse if plan exists."""
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_path = output_dir / PLAN_FILENAME
    sha_path = output_dir / PLAN_SHA256_FILENAME
    if plan_path.exists() or sha_path.exists():
        raise BatchArchiveError(f"plan already exists: {plan_path}")
    payload = json.dumps(plan, indent=2, sort_keys=True) + "\n"
    plan_bytes = payload.encode("utf-8")
    plan_path.write_bytes(plan_bytes)
    sha_path.write_text(sha256_file(plan_path) + "\n", encoding="utf-8", newline="\n")
    return plan_path, sha_path


def load_batch_plan(plan_path: Path) -> tuple[dict[str, Any], str]:
    if not plan_path.is_file():
        raise BatchArchiveError(f"plan file is missing: {plan_path}")
    plan_bytes = plan_path.read_bytes()
    plan = cast(dict[str, Any], json.loads(plan_bytes.decode("utf-8")))
    sha_path = plan_path.parent / PLAN_SHA256_FILENAME
    if not sha_path.is_file():
        raise BatchArchiveError(f"plan checksum sidecar is missing: {sha_path}")
    expected = sha_path.read_text(encoding="utf-8").strip()
    actual = hashlib_sha256_bytes(plan_bytes)
    if actual != expected:
        raise BatchArchiveError("plan checksum mismatch; plan is not immutable")
    return plan, actual


def hashlib_sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def batch_root_for_plan(plan_path: Path, output_dir: Path | None) -> Path:
    """Resolve run artifact root; default is the plan file directory (not its parent)."""
    if output_dir is not None:
        return output_dir.resolve()
    # Plan lives at ``{batch_root}/archive_batch_plan.json``; keep progress alongside it.
    return plan_path.parent.resolve()


def progress_path(batch_root: Path, plan_id: str) -> Path:
    return batch_root / "_batch" / plan_id / "progress.json"


def verification_root(batch_root: Path, plan_id: str) -> Path:
    return batch_root / "_batch" / plan_id / VERIFICATION_DIRNAME


def batch_verification_path(batch_root: Path, plan_id: str) -> Path:
    return batch_root / "_batch" / plan_id / "batch_verification.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _normalize_window_status(status: str) -> str:
    if status == LEGACY_WINDOW_STATE_COMPLETED:
        return WINDOW_STATE_COMPLETED_ADMISSIBLE
    if status == LEGACY_WINDOW_STATE_FAILED:
        return WINDOW_STATE_FAILED_STORAGE
    return status


def _window_is_storage_complete(status: str) -> bool:
    return _normalize_window_status(status) in STORAGE_COMPLETE_WINDOW_STATES


def _window_is_admissible(status: str, *, quarantined: bool = False) -> bool:
    if quarantined:
        return False
    normalized = _normalize_window_status(status)
    if normalized == WINDOW_STATE_SKIPPED_QUARANTINED:
        return False
    if normalized == WINDOW_STATE_COMPLETED_QUARANTINED:
        return False
    return normalized in ADMISSIBLE_WINDOW_STATES


def _storage_complete_status_from_metadata(metadata: dict[str, Any]) -> str:
    if metadata.get("quarantined"):
        return WINDOW_STATE_COMPLETED_QUARANTINED
    if metadata.get("admission_eligible"):
        return WINDOW_STATE_COMPLETED_ADMISSIBLE
    return WINDOW_STATE_COMPLETED_QUARANTINED


def _initial_progress(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "plan_id": plan["plan_id"],
        "schema_version": BATCH_SCHEMA_VERSION,
        "updated_at_utc": datetime.now(UTC).isoformat(),
        "windows": {
            str(window["index"]): {
                "status": WINDOW_STATE_PENDING,
                "dataset_id": window["dataset_id"],
            }
            for window in plan["windows"]
        },
        "upload_bytes_this_run": 0,
        "status": RUN_STATUS_RUNNING,
    }


def load_or_create_progress(batch_root: Path, plan: dict[str, Any]) -> dict[str, Any]:
    path = progress_path(batch_root, str(plan["plan_id"]))
    if path.is_file():
        progress = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
        # Crash recovery: running windows become pending again.
        for entry in progress.get("windows", {}).values():
            if entry.get("status") == WINDOW_STATE_RUNNING:
                entry["status"] = WINDOW_STATE_PENDING
        return progress
    progress = _initial_progress(plan)
    _atomic_write_json(path, progress)
    return progress


def _save_progress(batch_root: Path, plan_id: str, progress: dict[str, Any]) -> None:
    progress["updated_at_utc"] = datetime.now(UTC).isoformat()
    _atomic_write_json(progress_path(batch_root, plan_id), progress)


def _window_entry(progress: dict[str, Any], index: int) -> dict[str, Any]:
    return cast(dict[str, Any], progress["windows"][str(index)])


def _disk_free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _ensure_run_disk_preflight(
    batch_root: Path,
    limits: BatchRunLimits,
    plan_limits: dict[str, Any],
) -> None:
    max_bundle = int(plan_limits["max_bundle_bytes"])
    required = max(limits.min_free_disk_bytes, OPERATIONAL_DISK_FLOOR_BYTES) + 2 * max_bundle
    free_bytes = _disk_free_bytes(batch_root)
    if free_bytes < required:
        raise BatchArchiveError("insufficient free disk for batch window execution")


def _has_incomplete_attempts(store: ArchiveStore, dataset_id: str) -> bool:
    prefix = f"{ARCHIVE_KEY_PREFIX}/{dataset_id}/attempts/"
    marker = f"/{INCOMPLETE_MARKER_NAME}"
    return any(key.endswith(marker) for key in store.list_keys(prefix))


def _verify_completed_window(
    store: ArchiveStore,
    window: dict[str, Any],
    *,
    batch_root: Path,
    plan_id: str,
    run_limits: BatchRunLimits,
) -> dict[str, Any]:
    dataset_id = str(window["dataset_id"])
    restore_parent = batch_root / "_batch" / plan_id / "restore"
    restore_parent.mkdir(parents=True, exist_ok=True)
    work_dir = restore_parent / dataset_id
    if work_dir.exists():
        raise BatchArchiveError(f"restore work directory already exists: {work_dir}")

    result = verify_restore_archive(
        store,
        dataset_id,
        restore_parent,
        gap_warning_seconds=run_limits.gap_warning_seconds,
        price_discontinuity_percent=run_limits.price_discontinuity_percent,
        exchange_boundary_tolerance_seconds=run_limits.exchange_boundary_tolerance_seconds,
    )
    if result.get("status") != "verified":
        return {
            "status": WINDOW_STATE_FAILED_STORAGE,
            "error": result.get("error"),
            "verify": result,
        }

    quality_status = result.get("quality_status")
    if quality_status == "warning" and not run_limits.allow_quality_warnings:
        return {
            "status": WINDOW_STATE_FAILED_STORAGE,
            "error": "quality status warning requires allow_quality_warnings",
            "verify": result,
        }

    completed = store.read_bytes(_completed_key(dataset_id))
    payload = cast(dict[str, Any], json.loads(completed.decode("utf-8")))
    attempt_id = str(payload["attempt_id"])
    metadata_key = f"{ARCHIVE_KEY_PREFIX}/{dataset_id}/attempts/{attempt_id}/archive_metadata.json"
    metadata = cast(
        dict[str, Any],
        json.loads(store.read_bytes(metadata_key).decode("utf-8")),
    )
    quarantined = bool(metadata.get("quarantined"))
    archived_events = int(metadata.get("row_counts", {}).get("events", -1))
    expected_events = int(window["expected_event_count"])
    if archived_events != expected_events:
        return {
            "status": WINDOW_STATE_FAILED_STORAGE,
            "error": "archived event count mismatch vs plan",
            "expected_event_count": expected_events,
            "archived_event_count": archived_events,
            "verify": result,
        }
    topics = metadata.get("topics", {})
    if isinstance(topics, dict) and "trades" in topics:
        archived_trades = int(topics["trades"])
        expected_trades = int(window["expected_trade_count"])
        if archived_trades != expected_trades:
            return {
                "status": WINDOW_STATE_FAILED_STORAGE,
                "error": "archived trade count mismatch vs plan",
                "expected_trade_count": expected_trades,
                "archived_trade_count": archived_trades,
                "verify": result,
            }

    if quarantined:
        return {
            "status": WINDOW_STATE_SKIPPED_QUARANTINED,
            "verify": result,
            "archived_event_count": archived_events,
            "quarantined": True,
            "admission_eligible": False,
        }

    return {
        "status": WINDOW_STATE_SKIPPED_VERIFIED,
        "verify": result,
        "archived_event_count": archived_events,
        "quarantined": False,
        "admission_eligible": bool(metadata.get("admission_eligible")),
    }


async def _export_and_upload_window(
    *,
    window: dict[str, Any],
    plan: dict[str, Any],
    batch_root: Path,
    store: ArchiveStore,
    session_factory: async_sessionmaker[AsyncSession],
    confirm_upload: bool,
    run_limits: BatchRunLimits,
) -> dict[str, Any]:
    symbol = str(plan["symbol"])
    start = datetime.fromisoformat(str(window["start_utc"]))
    end = datetime.fromisoformat(str(window["end_utc"]))
    plan_limits = cast(dict[str, Any], plan["limits"])
    export_limits = WindowExportLimits(
        max_duration_seconds=int(plan_limits["window_seconds"]),
        max_rows=int(plan_limits["max_rows"]),
        max_bundle_bytes=int(plan_limits["max_bundle_bytes"]),
        min_free_disk_bytes=int(plan_limits["min_free_disk_bytes"]),
    )
    windows_dir = batch_root / "windows"
    events = await load_window_events(
        session_factory,
        symbol,
        start,
        end,
        max_rows=export_limits.max_rows,
    )
    bundle_dir = build_archive_bundle(
        symbol=symbol,
        start=start,
        end=end,
        output_dir=windows_dir,
        events=events,
        limits=export_limits,
        gap_warning_seconds=run_limits.gap_warning_seconds,
        price_discontinuity_percent=run_limits.price_discontinuity_percent,
        exchange_boundary_tolerance_seconds=run_limits.exchange_boundary_tolerance_seconds,
    )
    verify_root = verification_root(batch_root, str(plan["plan_id"]))
    upload = upload_archive_bundle(
        bundle_dir,
        store,
        confirm_upload=confirm_upload,
        allow_quality_warnings=run_limits.allow_quality_warnings,
        confirm_quarantine_upload=run_limits.confirm_quarantine_upload,
        verification_root=verify_root,
        gap_warning_seconds=run_limits.gap_warning_seconds,
        price_discontinuity_percent=run_limits.price_discontinuity_percent,
        exchange_boundary_tolerance_seconds=run_limits.exchange_boundary_tolerance_seconds,
    )
    if upload.get("status") != "verified":
        return {
            "status": WINDOW_STATE_FAILED_STORAGE,
            "error": upload.get("error", "upload failed"),
            "upload": upload,
        }
    metadata = json.loads(
        (bundle_dir / "archive_metadata.json").read_text(encoding="utf-8")
    )
    return {
        "status": _storage_complete_status_from_metadata(metadata),
        "upload": upload,
        "row_count": len(events),
        "bundle_dir": str(bundle_dir),
        "quarantined": bool(metadata.get("quarantined")),
        "admission_eligible": bool(metadata.get("admission_eligible")),
    }


def reconcile_batch(
    plan: dict[str, Any],
    progress: dict[str, Any],
    *,
    plan_sha256: str,
) -> dict[str, Any]:
    windows_plan = cast(list[dict[str, Any]], plan["windows"])
    window_statuses: list[dict[str, Any]] = []
    dataset_ids: list[str] = []
    admissible_event_total = 0
    quarantined_event_total = 0
    storage_event_total = 0
    checksums_valid = True
    all_storage_complete = True

    for window in windows_plan:
        entry = _window_entry(progress, int(window["index"]))
        raw_status = str(entry.get("status", WINDOW_STATE_PENDING))
        status = _normalize_window_status(raw_status)
        dataset_id = str(window["dataset_id"])
        dataset_ids.append(dataset_id)
        quarantined = bool(entry.get("quarantined"))
        if status in {
            WINDOW_STATE_SKIPPED_QUARANTINED,
            WINDOW_STATE_COMPLETED_QUARANTINED,
        } or (
            status == WINDOW_STATE_SKIPPED_VERIFIED
            and entry.get("quarantined") is True
        ):
            quarantined = True

        if not _window_is_storage_complete(raw_status):
            all_storage_complete = False
        else:
            if status in {
                WINDOW_STATE_COMPLETED_ADMISSIBLE,
                WINDOW_STATE_COMPLETED_QUARANTINED,
                LEGACY_WINDOW_STATE_COMPLETED,
            }:
                event_count = int(window["expected_event_count"])
            else:
                event_count = int(
                    entry.get("archived_event_count", window["expected_event_count"])
                )
            storage_event_total += event_count
            if quarantined:
                quarantined_event_total += event_count
            elif _window_is_admissible(status, quarantined=quarantined):
                admissible_event_total += event_count

        if entry.get("verify", {}).get("status") == "failed":
            checksums_valid = False
        window_statuses.append(
            {
                "index": window["index"],
                "dataset_id": dataset_id,
                "status": status,
                "quarantined": quarantined,
                "admission_eligible": bool(entry.get("admission_eligible"))
                and not quarantined,
                "expected_event_count": window["expected_event_count"],
                "expected_trade_count": window["expected_trade_count"],
            }
        )

    boundary_ok = all(
        windows_plan[index]["end_utc"] == windows_plan[index + 1]["start_utc"]
        for index in range(len(windows_plan) - 1)
    )
    unique_ids = len(set(dataset_ids)) == len(dataset_ids)
    range_expected = int(plan["range_expected_event_count"])
    storage_event_reconciled = storage_event_total == range_expected
    admissible_event_reconciled = admissible_event_total == range_expected
    has_failed_storage = any(
        _normalize_window_status(
            str(_window_entry(progress, int(window["index"]))["status"])
        )
        == WINDOW_STATE_FAILED_STORAGE
        for window in windows_plan
    )
    has_quarantined = any(item["quarantined"] for item in window_statuses)
    admissible_coverage_continuous = (
        all_storage_complete
        and not has_quarantined
        and not has_failed_storage
        and admissible_event_reconciled
    )

    passed = (
        all_storage_complete
        and unique_ids
        and boundary_ok
        and storage_event_reconciled
        and checksums_valid
        and not has_failed_storage
    )

    return {
        "plan_id": plan["plan_id"],
        "plan_sha256": plan_sha256,
        "windows": window_statuses,
        "all_storage_complete": all_storage_complete,
        "all_completed_or_skipped": all_storage_complete,
        "unique_dataset_ids": unique_ids,
        "boundary_continuity": boundary_ok,
        "event_total_reconciled": storage_event_reconciled,
        "storage_event_total_reconciled": storage_event_reconciled,
        "admissible_event_total_reconciled": admissible_event_reconciled,
        "admissible_event_total": admissible_event_total,
        "quarantined_event_total": quarantined_event_total,
        "admissible_coverage_continuous": admissible_coverage_continuous,
        "checksums_valid": checksums_valid,
        "failed_storage": has_failed_storage,
        "status": RUN_STATUS_PASS if passed else RUN_STATUS_FAILED,
        "retention_authorized": False,
        "note": "batch archive does not authorize retention or delete",
    }


async def run_batch_plan(
    plan_path: Path,
    *,
    batch_root: Path | None = None,
    store: ArchiveStore,
    session_factory: async_sessionmaker[AsyncSession],
    confirm_upload: bool,
    run_limits: BatchRunLimits | None = None,
    provider: str = "b2",
) -> dict[str, Any]:
    run_limits = run_limits or BatchRunLimits()
    plan, plan_sha256 = load_batch_plan(plan_path)
    root = batch_root_for_plan(plan_path, batch_root)
    plan_id = str(plan["plan_id"])
    progress = load_or_create_progress(root, plan)

    if provider == "b2" and not confirm_upload:
        raise BatchArchiveError("remote batch run requires --confirm-upload")

    plan_limits = cast(dict[str, Any], plan["limits"])

    # Fail closed when prior failures exist.
    for window in cast(list[dict[str, Any]], plan["windows"]):
        entry = _window_entry(progress, int(window["index"]))
        if _normalize_window_status(str(entry.get("status", ""))) == WINDOW_STATE_FAILED_STORAGE:
            summary = {
                "plan_id": plan_id,
                "status": RUN_STATUS_FAILED,
                "error": "prior window failure blocks batch resume",
                "failed_window_index": window["index"],
            }
            return summary

    upload_budget = run_limits.max_upload_bytes
    upload_used = int(progress.get("upload_bytes_this_run", 0))
    processed = 0
    windows_plan = cast(list[dict[str, Any]], plan["windows"])

    for window in windows_plan:
        index = int(window["index"])
        entry = _window_entry(progress, index)
        status = str(entry.get("status", WINDOW_STATE_PENDING))
        if _window_is_storage_complete(status):
            continue
        if _normalize_window_status(status) == WINDOW_STATE_FAILED_STORAGE:
            break
        if processed >= run_limits.max_windows:
            progress["status"] = RUN_STATUS_PARTIAL
            _save_progress(root, plan_id, progress)
            return {
                "plan_id": plan_id,
                "status": RUN_STATUS_PARTIAL,
                "processed_this_run": processed,
                "upload_bytes_this_run": upload_used,
            }

        max_bundle = int(plan_limits["max_bundle_bytes"])
        if upload_used + max_bundle > upload_budget:
            progress["status"] = RUN_STATUS_PARTIAL
            _save_progress(root, plan_id, progress)
            return {
                "plan_id": plan_id,
                "status": RUN_STATUS_PARTIAL,
                "reason": "max_upload_bytes budget exhausted",
                "processed_this_run": processed,
                "upload_bytes_this_run": upload_used,
            }

        try:
            _ensure_run_disk_preflight(root, run_limits, plan_limits)
        except BatchArchiveError as error:
            progress["status"] = RUN_STATUS_PARTIAL
            _save_progress(root, plan_id, progress)
            return {
                "plan_id": plan_id,
                "status": RUN_STATUS_PARTIAL,
                "reason": str(error),
                "processed_this_run": processed,
            }

        entry["status"] = WINDOW_STATE_RUNNING
        _save_progress(root, plan_id, progress)

        dataset_id = str(window["dataset_id"])
        try:
            if store.exists(_completed_key(dataset_id)):
                outcome = _verify_completed_window(
                    store,
                    window,
                    batch_root=root,
                    plan_id=plan_id,
                    run_limits=run_limits,
                )
            elif _has_incomplete_attempts(store, dataset_id):
                if not run_limits.allow_new_attempt_after_incomplete:
                    outcome = {
                        "status": WINDOW_STATE_FAILED_STORAGE,
                        "error": "incomplete attempt exists; fail-closed policy",
                    }
                else:
                    outcome = await _export_and_upload_window(
                        window=window,
                        plan=plan,
                        batch_root=root,
                        store=store,
                        session_factory=session_factory,
                        confirm_upload=confirm_upload,
                        run_limits=run_limits,
                    )
            else:
                outcome = await _export_and_upload_window(
                    window=window,
                    plan=plan,
                    batch_root=root,
                    store=store,
                    session_factory=session_factory,
                    confirm_upload=confirm_upload,
                    run_limits=run_limits,
                )
        except (WindowExportError, BatchArchiveError) as error:
            outcome = {"status": WINDOW_STATE_FAILED_STORAGE, "error": str(error)}

        entry.update(outcome)
        entry["dataset_id"] = dataset_id
        if _normalize_window_status(str(outcome.get("status", ""))) in {
            WINDOW_STATE_COMPLETED_ADMISSIBLE,
            WINDOW_STATE_COMPLETED_QUARANTINED,
            LEGACY_WINDOW_STATE_COMPLETED,
        }:
            upload_used += max_bundle
            progress["upload_bytes_this_run"] = upload_used
        if outcome.get("status") in {
            WINDOW_STATE_SKIPPED_VERIFIED,
            WINDOW_STATE_SKIPPED_QUARANTINED,
        }:
            entry["archived_event_count"] = outcome.get("archived_event_count")

        _save_progress(root, plan_id, progress)
        processed += 1

        if _normalize_window_status(str(outcome.get("status", ""))) == WINDOW_STATE_FAILED_STORAGE:
            progress["status"] = RUN_STATUS_FAILED
            _save_progress(root, plan_id, progress)
            return {
                "plan_id": plan_id,
                "status": RUN_STATUS_FAILED,
                "failed_window_index": index,
                "error": outcome.get("error"),
                "processed_this_run": processed,
            }

    # Final reconciliation when every window is completed or skipped.
    reconciliation = reconcile_batch(plan, progress, plan_sha256=plan_sha256)
    all_terminal = all(
        _window_is_storage_complete(
            str(_window_entry(progress, int(window["index"]))["status"])
        )
        for window in windows_plan
    )
    if all_terminal:
        _atomic_write_json(batch_verification_path(root, plan_id), reconciliation)
        progress["status"] = reconciliation["status"]
        _save_progress(root, plan_id, progress)
        return {
            "plan_id": plan_id,
            "status": reconciliation["status"],
            "batch_verification": str(batch_verification_path(root, plan_id)),
            "reconciliation": reconciliation,
        }

    progress["status"] = RUN_STATUS_PARTIAL
    _save_progress(root, plan_id, progress)
    return {
        "plan_id": plan_id,
        "status": RUN_STATUS_PARTIAL,
        "processed_this_run": processed,
        "upload_bytes_this_run": upload_used,
    }


def redacted_plan_summary(plan: dict[str, Any], plan_path: Path, sha_path: Path) -> dict[str, Any]:
    return {
        "plan_id": plan["plan_id"],
        "plan_path": str(plan_path),
        "plan_sha256_path": str(sha_path),
        "window_count": len(plan["windows"]),
        "range_expected_event_count": plan["range_expected_event_count"],
        "range_expected_trade_count": plan["range_expected_trade_count"],
        "status": "planned",
    }
