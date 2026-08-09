import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.archive.manifest import ArchiveManifest
from trading_bot.archive.retention_identity import require_retention_mutation_identity
from trading_bot.archive.store import ArchiveStore
from trading_bot.archive.window import (
    WindowExportError,
    dataset_has_incomplete_marker,
    load_completed_attempt_metadata,
)
from trading_bot.storage.models import MarketEvent

MAX_DELETE_CHUNK = 1000
COUNT_STATEMENT_TIMEOUT = "60s"
DELETE_CONFIRMATION_TOKEN = "DELETE_VERIFIED_ARCHIVE"
VERIFIED_EXTERNAL_DESTINATIONS = frozenset({"s3", "pc_filesystem"})
PRODUCTION_ARCHIVE_DESTINATIONS = frozenset({"s3", "b2_s3", "pc_filesystem"})

AUDIT_ROWS_DELETED_HISTORY_CAP = 100
PROGRESS_STATUS_PLANNED = "planned"
PROGRESS_STATUS_RUNNING = "running"
PROGRESS_STATUS_COMPLETED = "completed"
PROGRESS_STATUS_FAILED = "failed"
PROGRESS_STATUS_BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    interval_start_utc: str
    interval_end_utc: str
    min_raw_event_id: int
    max_raw_event_id: int
    row_count: int
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    generated_at_utc: str
    hot_raw_days: int
    dry_run: bool
    eligible_rows: int
    candidates: tuple[RetentionCandidate, ...]
    state: str


@dataclass(frozen=True, slots=True)
class CoverageWindow:
    dataset_id: str
    expected_event_count: int
    min_raw_event_id: int | None
    max_raw_event_id: int | None
    start_utc: str | None
    end_utc: str | None


@dataclass(frozen=True, slots=True)
class ArchivedRawRangeTarget:
    min_raw_event_id: int
    max_raw_event_id: int
    expected_row_count: int
    coverage_plan_sha256: str
    windows: tuple[CoverageWindow, ...]


@dataclass(frozen=True, slots=True)
class RetentionRuntimeGuards:
    collector_stopped: bool
    write_quiescent: bool
    postgresql_healthy: bool
    free_disk_bytes: int
    min_free_disk_bytes: int


def plan_retention(
    manifests: list[tuple[ArchiveManifest, str]],
    *,
    now: datetime,
    hot_raw_days: int,
) -> RetentionPlan:
    if now.tzinfo is None or hot_raw_days < 1:
        raise ValueError("retention inputs are invalid")
    cutoff = now.astimezone(UTC) - timedelta(days=hot_raw_days)
    candidates: list[RetentionCandidate] = []
    previous_end: datetime | None = None
    for manifest, digest in sorted(
        manifests,
        key=lambda item: item[0].interval_start_utc,
    ):
        start = datetime.fromisoformat(manifest.interval_start_utc)
        end = datetime.fromisoformat(manifest.interval_end_utc)
        if (
            manifest.verification_status != "verified"
            or manifest.destination not in VERIFIED_EXTERNAL_DESTINATIONS
            or manifest.dataset_group != "raw_and_normalized"
            or end > cutoff
            or end - start != timedelta(days=1)
        ):
            continue
        if previous_end is not None and start != previous_end:
            raise RuntimeError("verified archive intervals contain a gap")
        raw_rows = sum(
            item.row_count for item in manifest.objects if item.dataset == "raw"
        )
        if raw_rows != manifest.raw_row_count:
            raise RuntimeError("verified archive coverage is inconsistent")
        candidates.append(
            RetentionCandidate(
                interval_start_utc=manifest.interval_start_utc,
                interval_end_utc=manifest.interval_end_utc,
                min_raw_event_id=manifest.min_raw_event_id,
                max_raw_event_id=manifest.max_raw_event_id,
                row_count=manifest.raw_row_count,
                manifest_sha256=digest,
            )
        )
        previous_end = end
    return RetentionPlan(
        generated_at_utc=now.astimezone(UTC).isoformat(),
        hot_raw_days=hot_raw_days,
        dry_run=True,
        eligible_rows=sum(item.row_count for item in candidates),
        candidates=tuple(candidates),
        state="eligible" if candidates else "nothing_eligible",
    )


def _parse_coverage_windows(raw_windows: list[dict[str, Any]]) -> tuple[CoverageWindow, ...]:
    windows: list[CoverageWindow] = []
    for entry in raw_windows:
        dataset_id = entry.get("dataset_id")
        expected = entry.get("expected_event_count")
        if not dataset_id or expected is None:
            raise ValueError("coverage window missing dataset_id or expected_event_count")
        min_id = entry.get("min_raw_event_id", entry.get("min_id"))
        max_id = entry.get("max_raw_event_id", entry.get("max_id"))
        windows.append(
            CoverageWindow(
                dataset_id=str(dataset_id),
                expected_event_count=int(expected),
                min_raw_event_id=int(min_id) if min_id is not None else None,
                max_raw_event_id=int(max_id) if max_id is not None else None,
                start_utc=entry.get("start_utc"),
                end_utc=entry.get("end_utc"),
            )
        )
    return tuple(windows)


def _validate_coverage_contiguity(
    target: ArchivedRawRangeTarget,
) -> None:
    windows = sorted(
        target.windows,
        key=lambda item: (
            item.min_raw_event_id if item.min_raw_event_id is not None else -1,
            item.dataset_id,
        ),
    )
    if not windows:
        raise ValueError("coverage plan must contain at least one window")
    window_sum = sum(window.expected_event_count for window in windows)
    if window_sum != target.expected_row_count:
        raise ValueError(
            "coverage window event counts do not sum to expected_row_count"
        )
    for window in windows:
        if window.min_raw_event_id is None or window.max_raw_event_id is None:
            raise ValueError("coverage windows must all include id bounds")
    id_windows = windows
    if id_windows[0].min_raw_event_id != target.min_raw_event_id:
        raise ValueError("coverage windows do not start at target min_raw_event_id")
    if id_windows[-1].max_raw_event_id != target.max_raw_event_id:
        raise ValueError("coverage windows do not end at target max_raw_event_id")
    for left, right in zip(id_windows, id_windows[1:], strict=False):
        if left.max_raw_event_id is None or right.min_raw_event_id is None:
            raise ValueError("coverage window id bounds are incomplete")
        if left.max_raw_event_id + 1 != right.min_raw_event_id:
            raise ValueError("coverage window id ranges are not contiguous")


def load_coverage_plan(path: Path) -> ArchivedRawRangeTarget:
    """Load a native or hot-buffer master coverage plan and validate contiguity."""
    raw_bytes = path.read_bytes()
    payload = cast(dict[str, Any], json.loads(raw_bytes.decode("utf-8")))
    plan_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if "source_bounds" in payload:
        bounds = payload["source_bounds"]
        min_id = bounds.get("min_id")
        max_id = bounds.get("max_id")
        expected_rows = bounds.get("expected_rows")
        if min_id is None or max_id is None or expected_rows is None:
            raise ValueError("hot-buffer coverage plan source_bounds is incomplete")
        target = ArchivedRawRangeTarget(
            min_raw_event_id=int(min_id),
            max_raw_event_id=int(max_id),
            expected_row_count=int(expected_rows),
            coverage_plan_sha256=plan_sha256,
            windows=_parse_coverage_windows(cast(list[dict[str, Any]], payload["windows"])),
        )
    else:
        min_id = payload.get("min_raw_event_id")
        max_id = payload.get("max_raw_event_id")
        expected_rows = payload.get("expected_row_count")
        if min_id is None or max_id is None or expected_rows is None:
            raise ValueError("native coverage plan bounds are incomplete")
        target = ArchivedRawRangeTarget(
            min_raw_event_id=int(min_id),
            max_raw_event_id=int(max_id),
            expected_row_count=int(expected_rows),
            coverage_plan_sha256=plan_sha256,
            windows=_parse_coverage_windows(cast(list[dict[str, Any]], payload["windows"])),
        )
    _validate_coverage_contiguity(target)
    return target


def verify_archive_coverage_for_retention(
    store: ArchiveStore,
    target: ArchivedRawRangeTarget,
) -> dict[str, Any]:
    """Verify external archive storage completeness for retention (not admission)."""
    reasons: list[str] = []
    window_results: list[dict[str, Any]] = []
    storage_event_total = 0
    all_windows_pass = True
    id_windows = [
        window
        for window in target.windows
        if window.min_raw_event_id is not None and window.max_raw_event_id is not None
    ]

    for window in target.windows:
        window_pass = True
        window_reasons: list[str] = []
        try:
            metadata_bundle = load_completed_attempt_metadata(store, window.dataset_id)
            archive_metadata = metadata_bundle["archive_metadata"]
        except WindowExportError as error:
            window_pass = False
            window_reasons.append(str(error))
            archive_metadata = {}
            metadata_bundle = {"attempt_id": None, "completed": {}, "archive_metadata": {}}
        else:
            if dataset_has_incomplete_marker(store, window.dataset_id):
                window_pass = False
                window_reasons.append("INCOMPLETE marker present")

            row_counts = archive_metadata.get("row_counts")
            if not isinstance(row_counts, dict):
                window_pass = False
                window_reasons.append("archive_metadata row_counts missing")
                events = None
            else:
                events = row_counts.get("events")
                if events is None:
                    window_pass = False
                    window_reasons.append("archive_metadata row_counts.events missing")
                elif int(events) != window.expected_event_count:
                    window_pass = False
                    window_reasons.append(
                        "archive_metadata row_counts.events mismatch"
                    )
                else:
                    storage_event_total += int(events)

        if not window_pass:
            all_windows_pass = False
            reasons.extend(
                f"{window.dataset_id}: {reason}" for reason in window_reasons
            )
        window_results.append(
            {
                "dataset_id": window.dataset_id,
                "expected_event_count": window.expected_event_count,
                "attempt_id": metadata_bundle.get("attempt_id"),
                "status": "pass" if window_pass else "fail",
                "reasons": window_reasons,
            }
        )

    if storage_event_total != target.expected_row_count:
        all_windows_pass = False
        reasons.append("archived event total does not match expected_row_count")

    storage_coverage_continuous = False
    if all_windows_pass and storage_event_total == target.expected_row_count:
        if id_windows:
            sorted_windows = sorted(id_windows, key=lambda item: item.min_raw_event_id or 0)
            contiguous = (
                sorted_windows[0].min_raw_event_id == target.min_raw_event_id
                and sorted_windows[-1].max_raw_event_id == target.max_raw_event_id
                and all(
                    left.max_raw_event_id is not None
                    and right.min_raw_event_id is not None
                    and left.max_raw_event_id + 1 == right.min_raw_event_id
                    for left, right in zip(sorted_windows, sorted_windows[1:], strict=False)
                )
            )
            storage_coverage_continuous = contiguous
            if not contiguous:
                reasons.append("archived window id ranges are not contiguous")
        else:
            storage_coverage_continuous = True

    status = "pass" if all_windows_pass and storage_coverage_continuous else "fail"
    return {
        "status": status,
        "reasons": reasons,
        "storage_event_total": storage_event_total,
        "expected_row_count": target.expected_row_count,
        "storage_coverage_continuous": storage_coverage_continuous,
        "windows": window_results,
        "coverage_plan_sha256": target.coverage_plan_sha256,
    }


def assert_retention_guards(guards: RetentionRuntimeGuards) -> None:
    if not guards.collector_stopped:
        raise RuntimeError("collector must be stopped before retention")
    if not guards.write_quiescent:
        raise RuntimeError("writes must be quiescent before retention")
    if not guards.postgresql_healthy:
        raise RuntimeError("postgresql must be healthy before retention")
    if guards.free_disk_bytes < guards.min_free_disk_bytes:
        raise RuntimeError("insufficient free disk for retention")


def _validate_batch_size(batch_size: int) -> int:
    if not 1 <= batch_size <= MAX_DELETE_CHUNK:
        raise ValueError(f"batch_size must be between 1 and {MAX_DELETE_CHUNK}")
    return batch_size


def _audit_path(audit_dir: Path, operation_id: str) -> Path:
    return audit_dir / f"{operation_id}.json"


def _progress_path(audit_dir: Path, operation_id: str) -> Path:
    return audit_dir / f"{operation_id}.progress.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
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


def _read_json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _find_matching_progress(
    audit_dir: Path,
    target: ArchivedRawRangeTarget,
) -> dict[str, Any] | None:
    if not audit_dir.is_dir():
        return None
    matches: list[tuple[str, dict[str, Any]]] = []
    for path in audit_dir.glob("*.progress.json"):
        progress = _read_json(path)
        if (
            progress.get("min_raw_event_id") == target.min_raw_event_id
            and progress.get("max_raw_event_id") == target.max_raw_event_id
            and progress.get("expected_rows") == target.expected_row_count
        ):
            matches.append((path.stem.removesuffix(".progress"), progress))
    if not matches:
        return None
    matches.sort(key=lambda item: item[1].get("updated_at_utc", ""), reverse=True)
    operation_id, progress = matches[0]
    return {"operation_id": operation_id, **progress}


async def _delete_bounded_chunk(
    session: AsyncSession,
    *,
    min_id: int,
    max_id: int,
    limit: int,
) -> list[int]:
    """Delete up to ``limit`` locked rows inside the inclusive target range.

    Always selects the lowest remaining ids in range. Do not seek past a prior
    max id — that could skip undeleted rows after SKIP LOCKED / interruption.

    PostgreSQL requires UPDATE privilege on ``market_events`` for
    ``SELECT ... FOR UPDATE SKIP LOCKED`` even though this path issues DELETE
    DML only; the retention role is granted UPDATE solely for row locking.
    """
    await session.execute(text("SET LOCAL statement_timeout = '5s'"))
    await session.execute(text("SET LOCAL lock_timeout = '2s'"))
    ids = list(
        await session.scalars(
            select(MarketEvent.id)
            .where(
                MarketEvent.id >= min_id,
                MarketEvent.id <= max_id,
            )
            .order_by(MarketEvent.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    if ids:
        await session.execute(delete(MarketEvent).where(MarketEvent.id.in_(ids)))
    return ids


class BoundedRetentionRunner:
    """Bounded, resumable RAW retention with filesystem audit/progress."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        audit_dir: Path,
        *,
        test_mode: bool = False,
        audit_store: ArchiveStore | None = None,
    ) -> None:
        self._factory = session_factory
        self._audit_dir = audit_dir
        self._test_mode = test_mode
        self._audit_store = audit_store

    async def count_target_rows(self, min_id: int, max_id: int) -> int:
        async with self._factory() as session:
            await session.execute(
                text(f"SET LOCAL statement_timeout = '{COUNT_STATEMENT_TIMEOUT}'")
            )
            count = await session.scalar(
                select(func.count())
                .select_from(MarketEvent)
                .where(
                    MarketEvent.id >= min_id,
                    MarketEvent.id <= max_id,
                )
            )
        return int(count or 0)

    def _publish_audit_copy(self, operation_id: str, payload: dict[str, Any]) -> None:
        if self._audit_store is None:
            return
        key = f"_retention/{operation_id}.json"
        self._audit_store.publish_bytes(
            key,
            (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode(),
        )

    def _init_audit_record(
        self,
        *,
        operation_id: str,
        target: ArchivedRawRangeTarget,
        batch_size: int,
        git_sha: str | None,
        confirmation_provided: bool,
    ) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        return {
            "operation_id": operation_id,
            "git_sha": git_sha,
            "min_raw_event_id": target.min_raw_event_id,
            "max_raw_event_id": target.max_raw_event_id,
            "expected_rows": target.expected_row_count,
            "coverage_plan_sha256": target.coverage_plan_sha256,
            "confirmation_provided": confirmation_provided,
            "batch_size": batch_size,
            "rows_deleted_per_batch": [],
            "cumulative_deleted": 0,
            "status": PROGRESS_STATUS_PLANNED,
            "started_at_utc": now,
            "updated_at_utc": now,
            "completed_at_utc": None,
            "final_remaining_target_rows": None,
            "last_error": None,
        }

    def _init_progress_record(
        self,
        *,
        operation_id: str,
        target: ArchivedRawRangeTarget,
        batch_size: int,
    ) -> dict[str, Any]:
        return {
            "operation_id": operation_id,
            "min_raw_event_id": target.min_raw_event_id,
            "max_raw_event_id": target.max_raw_event_id,
            "expected_rows": target.expected_row_count,
            "batch_size": batch_size,
            "cumulative_deleted": 0,
            "batches_completed": 0,
            "last_deleted_max_id": None,
            "status": PROGRESS_STATUS_PLANNED,
            "updated_at_utc": datetime.now(UTC).isoformat(),
            "last_error": None,
        }

    def _resolve_progress(
        self,
        target: ArchivedRawRangeTarget,
        operation_id: str | None,
    ) -> tuple[str, dict[str, Any] | None]:
        if operation_id is not None:
            progress_path = _progress_path(self._audit_dir, operation_id)
            if progress_path.is_file():
                return operation_id, _read_json(progress_path)
            return operation_id, None
        matched = _find_matching_progress(self._audit_dir, target)
        if matched is None:
            return str(uuid.uuid4()), None
        resolved_id = str(matched.pop("operation_id"))
        return resolved_id, matched

    def _progress_needs_reconcile(self, progress: dict[str, Any] | None) -> bool:
        if progress is None:
            return False
        cumulative_deleted = int(progress.get("cumulative_deleted", 0))
        status = progress.get("status")
        return (
            status in {
                PROGRESS_STATUS_RUNNING,
                PROGRESS_STATUS_FAILED,
                PROGRESS_STATUS_BLOCKED,
            }
            or cumulative_deleted > 0
        )

    async def _reconcile_progress_from_db(
        self,
        target: ArchivedRawRangeTarget,
        *,
        progress_record: dict[str, Any],
        audit: dict[str, Any],
        audit_path: Path,
        progress_path: Path,
        operation_id: str,
        persist: bool,
    ) -> int:
        """Reconcile cumulative_deleted from DB when resuming. Returns remaining rows."""
        remaining = await self.count_target_rows(
            target.min_raw_event_id,
            target.max_raw_event_id,
        )
        cumulative_deleted = int(progress_record.get("cumulative_deleted", 0))
        if self._progress_needs_reconcile(progress_record):
            if remaining > target.expected_row_count:
                raise RuntimeError(
                    "remaining target rows exceed expected_row_count (unexpected growth)"
                )
            reconciled_cumulative = target.expected_row_count - remaining
            if reconciled_cumulative != cumulative_deleted:
                cumulative_deleted = reconciled_cumulative
                progress_record["cumulative_deleted"] = cumulative_deleted
                audit["cumulative_deleted"] = cumulative_deleted
        else:
            expected_remaining = target.expected_row_count - cumulative_deleted
            if remaining != expected_remaining:
                raise RuntimeError(
                    "remaining target rows do not match coverage expectation"
                )
        if persist:
            now = datetime.now(UTC).isoformat()
            progress_record["updated_at_utc"] = now
            audit["updated_at_utc"] = now
            _write_json(audit_path, audit)
            _write_json(progress_path, progress_record)
            self._publish_audit_copy(operation_id, audit)
        return remaining

    async def dry_run(
        self,
        target: ArchivedRawRangeTarget,
        guards: RetentionRuntimeGuards,
        *,
        batch_size: int = MAX_DELETE_CHUNK,
        operation_id: str | None = None,
        git_sha: str | None = None,
        coverage_store: ArchiveStore | None = None,
    ) -> dict[str, Any]:
        batch_size = _validate_batch_size(batch_size)
        gate_result: dict[str, Any] | None = None
        if coverage_store is not None:
            gate_result = verify_archive_coverage_for_retention(coverage_store, target)
            if gate_result["status"] != "pass":
                raise RuntimeError("archive coverage gate failed")
        elif not self._test_mode:
            raise RuntimeError("coverage_store is required outside test_mode")

        assert_retention_guards(guards)
        resolved_id, progress = self._resolve_progress(target, operation_id)
        audit_path = _audit_path(self._audit_dir, resolved_id)
        progress_path = _progress_path(self._audit_dir, resolved_id)

        if progress and progress.get("status") == PROGRESS_STATUS_COMPLETED:
            return {
                "operation_id": resolved_id,
                "status": PROGRESS_STATUS_COMPLETED,
                "dry_run": True,
                "remaining_rows": 0,
                "planned_batches": 0,
                "batch_size": batch_size,
                "gate_result": gate_result,
                "cumulative_deleted": int(progress.get("cumulative_deleted", 0)),
            }

        audit = (
            _read_json(audit_path)
            if audit_path.is_file()
            else self._init_audit_record(
                operation_id=resolved_id,
                target=target,
                batch_size=batch_size,
                git_sha=git_sha,
                confirmation_provided=False,
            )
        )
        progress_record = progress or self._init_progress_record(
            operation_id=resolved_id,
            target=target,
            batch_size=batch_size,
        )
        remaining = await self._reconcile_progress_from_db(
            target,
            progress_record=progress_record,
            audit=audit,
            audit_path=audit_path,
            progress_path=progress_path,
            operation_id=resolved_id,
            persist=progress is not None,
        )
        cumulative_deleted = int(progress_record.get("cumulative_deleted", 0))
        planned_batches = (remaining + batch_size - 1) // batch_size if remaining else 0

        if progress is None:
            audit["status"] = PROGRESS_STATUS_PLANNED
            audit["final_remaining_target_rows"] = remaining
            progress_record["status"] = PROGRESS_STATUS_PLANNED
            progress_record["updated_at_utc"] = datetime.now(UTC).isoformat()
            _write_json(audit_path, audit)
            _write_json(progress_path, progress_record)
            self._publish_audit_copy(resolved_id, audit)
        elif progress_record.get("status") != PROGRESS_STATUS_PLANNED:
            audit["final_remaining_target_rows"] = remaining
            _write_json(audit_path, audit)
            _write_json(progress_path, progress_record)
            self._publish_audit_copy(resolved_id, audit)

        return_status = str(progress_record.get("status", PROGRESS_STATUS_PLANNED))
        return {
            "operation_id": resolved_id,
            "status": return_status,
            "dry_run": True,
            "remaining_rows": remaining,
            "planned_batches": planned_batches,
            "batch_size": batch_size,
            "gate_result": gate_result,
            "cumulative_deleted": cumulative_deleted,
        }

    async def execute(
        self,
        target: ArchivedRawRangeTarget,
        guards: RetentionRuntimeGuards,
        *,
        confirmation: str = "",
        confirm_delete: bool = False,
        batch_size: int = MAX_DELETE_CHUNK,
        max_batches: int | None = None,
        pause_seconds: float = 0.05,
        operation_id: str | None = None,
        git_sha: str | None = None,
        coverage_store: ArchiveStore | None = None,
        inter_batch_health_check: Callable[[], RetentionRuntimeGuards] | None = None,
    ) -> dict[str, Any]:
        if not confirm_delete:
            result = await self.dry_run(
                target,
                guards,
                batch_size=batch_size,
                operation_id=operation_id,
                git_sha=git_sha,
                coverage_store=coverage_store,
            )
            return {
                **result,
                "mutation": False,
                "confirm_delete": False,
            }
        if confirmation != DELETE_CONFIRMATION_TOKEN:
            raise PermissionError("retention confirmation is invalid")

        batch_size = _validate_batch_size(batch_size)
        gate_result: dict[str, Any] | None = None
        if coverage_store is not None:
            gate_result = verify_archive_coverage_for_retention(coverage_store, target)
            if gate_result["status"] != "pass":
                raise RuntimeError("archive coverage gate failed")
        elif not self._test_mode:
            raise RuntimeError("coverage_store is required outside test_mode")

        assert_retention_guards(guards)
        resolved_id, progress = self._resolve_progress(target, operation_id)
        audit_path = _audit_path(self._audit_dir, resolved_id)
        progress_path = _progress_path(self._audit_dir, resolved_id)

        if progress and progress.get("status") == PROGRESS_STATUS_COMPLETED:
            return {
                "operation_id": resolved_id,
                "status": PROGRESS_STATUS_COMPLETED,
                "deleted_rows": 0,
                "cumulative_deleted": int(progress.get("cumulative_deleted", 0)),
                "gate_result": gate_result,
                "dry_run": False,
            }

        audit = (
            _read_json(audit_path)
            if audit_path.is_file()
            else self._init_audit_record(
                operation_id=resolved_id,
                target=target,
                batch_size=batch_size,
                git_sha=git_sha,
                confirmation_provided=True,
            )
        )
        progress_record = progress or self._init_progress_record(
            operation_id=resolved_id,
            target=target,
            batch_size=batch_size,
        )
        if (
            progress is not None
            and progress.get("status") == PROGRESS_STATUS_FAILED
            and int(progress.get("cumulative_deleted", 0)) == 0
        ):
            raise RuntimeError(
                "cannot resume failed retention operation with zero deletions; "
                "provision a new --operation-id for the next production canary"
            )
        audit["confirmation_provided"] = True
        audit["status"] = PROGRESS_STATUS_RUNNING
        audit["updated_at_utc"] = datetime.now(UTC).isoformat()
        progress_record["status"] = PROGRESS_STATUS_RUNNING
        progress_record["updated_at_utc"] = audit["updated_at_utc"]
        _write_json(audit_path, audit)
        _write_json(progress_path, progress_record)
        self._publish_audit_copy(resolved_id, audit)

        cumulative_deleted = int(progress_record.get("cumulative_deleted", 0))
        batches_completed = int(progress_record.get("batches_completed", 0))
        deleted_this_run = 0
        batches_run = 0

        try:
            remaining = await self._reconcile_progress_from_db(
                target,
                progress_record=progress_record,
                audit=audit,
                audit_path=audit_path,
                progress_path=progress_path,
                operation_id=resolved_id,
                persist=True,
            )
            cumulative_deleted = int(progress_record.get("cumulative_deleted", 0))

            if not self._test_mode:
                async with self._factory() as session:
                    await require_retention_mutation_identity(session)

            while True:
                if remaining == 0:
                    break
                if max_batches is not None and batches_run >= max_batches:
                    remaining = await self.count_target_rows(
                        target.min_raw_event_id,
                        target.max_raw_event_id,
                    )
                    break

                async with self._factory.begin() as session:
                    ids = await _delete_bounded_chunk(
                        session,
                        min_id=target.min_raw_event_id,
                        max_id=target.max_raw_event_id,
                        limit=batch_size,
                    )
                deleted_count = len(ids)
                if deleted_count == 0:
                    audit["status"] = PROGRESS_STATUS_BLOCKED
                    audit["last_error"] = "no deletable rows found in target range"
                    progress_record["status"] = PROGRESS_STATUS_BLOCKED
                    progress_record["last_error"] = audit["last_error"]
                    break

                deleted_this_run += deleted_count
                batches_run += 1
                batches_completed += 1
                cumulative_deleted += deleted_count
                remaining -= deleted_count
                last_deleted_max_id = ids[-1]
                history = list(audit.get("rows_deleted_per_batch", []))
                history.append(deleted_count)
                audit["rows_deleted_per_batch"] = history[-AUDIT_ROWS_DELETED_HISTORY_CAP:]
                audit["cumulative_deleted"] = cumulative_deleted
                progress_record["cumulative_deleted"] = cumulative_deleted
                progress_record["batches_completed"] = batches_completed
                progress_record["last_deleted_max_id"] = last_deleted_max_id
                now = datetime.now(UTC).isoformat()
                audit["updated_at_utc"] = now
                progress_record["updated_at_utc"] = now
                _write_json(audit_path, audit)
                _write_json(progress_path, progress_record)
                self._publish_audit_copy(resolved_id, audit)

                if inter_batch_health_check is not None:
                    assert_retention_guards(inter_batch_health_check())

                if pause_seconds > 0:
                    await asyncio.sleep(pause_seconds)

                if deleted_count < batch_size:
                    remaining = await self.count_target_rows(
                        target.min_raw_event_id,
                        target.max_raw_event_id,
                    )
                    if remaining == 0:
                        break

            final_remaining = await self.count_target_rows(
                target.min_raw_event_id,
                target.max_raw_event_id,
            )
            audit["final_remaining_target_rows"] = final_remaining
            if final_remaining == 0 and audit.get("status") != PROGRESS_STATUS_BLOCKED:
                audit["status"] = PROGRESS_STATUS_COMPLETED
                progress_record["status"] = PROGRESS_STATUS_COMPLETED
                audit["completed_at_utc"] = datetime.now(UTC).isoformat()
            elif audit.get("status") != PROGRESS_STATUS_BLOCKED:
                audit["status"] = PROGRESS_STATUS_RUNNING
                progress_record["status"] = PROGRESS_STATUS_RUNNING
            audit["updated_at_utc"] = datetime.now(UTC).isoformat()
            progress_record["updated_at_utc"] = audit["updated_at_utc"]
            _write_json(audit_path, audit)
            _write_json(progress_path, progress_record)
            self._publish_audit_copy(resolved_id, audit)
        except Exception as error:
            audit["status"] = PROGRESS_STATUS_FAILED
            audit["last_error"] = str(error)
            progress_record["status"] = PROGRESS_STATUS_FAILED
            progress_record["last_error"] = str(error)
            audit["updated_at_utc"] = datetime.now(UTC).isoformat()
            progress_record["updated_at_utc"] = audit["updated_at_utc"]
            _write_json(audit_path, audit)
            _write_json(progress_path, progress_record)
            self._publish_audit_copy(resolved_id, audit)
            raise

        return {
            "operation_id": resolved_id,
            "status": audit["status"],
            "deleted_rows": deleted_this_run,
            "cumulative_deleted": cumulative_deleted,
            "remaining_rows": final_remaining,
            "gate_result": gate_result,
            "dry_run": False,
        }


class RetentionExecutor:
    """Bounded executor; not exposed by the production CLI in this milestone."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        audit_store: ArchiveStore,
        *,
        test_mode: bool = False,
    ) -> None:
        self._factory = session_factory
        self._store = audit_store
        self._test_mode = test_mode

    async def delete_verified_chunk(
        self,
        candidate: RetentionCandidate,
        *,
        limit: int,
        confirmation: str,
    ) -> int:
        if confirmation != DELETE_CONFIRMATION_TOKEN:
            raise PermissionError("retention confirmation is invalid")
        if not 1 <= limit <= MAX_DELETE_CHUNK:
            raise ValueError(f"retention chunk must be between 1 and {MAX_DELETE_CHUNK}")
        if (
            self._store.destination_label not in PRODUCTION_ARCHIVE_DESTINATIONS
            and not self._test_mode
        ):
            raise RuntimeError("production retention requires verified external storage")
        audit_id = str(uuid.uuid4())
        key = f"_retention/{audit_id}.json"
        started = {
            "audit_id": audit_id,
            "status": "started",
            "candidate": asdict(candidate),
            "limit": limit,
            "created_at_utc": datetime.now(UTC).isoformat(),
        }
        self._store.publish_bytes(
            key,
            (json.dumps(started, separators=(",", ":"), sort_keys=True) + "\n").encode(),
        )
        async with self._factory.begin() as session:
            ids = await _delete_bounded_chunk(
                session,
                min_id=candidate.min_raw_event_id,
                max_id=candidate.max_raw_event_id,
                limit=limit,
            )
        completed = {
            **started,
            "status": "completed",
            "deleted_rows": len(ids),
            "completed_at_utc": datetime.now(UTC).isoformat(),
        }
        self._store.publish_bytes(
            key,
            (json.dumps(completed, separators=(",", ":"), sort_keys=True) + "\n").encode(),
        )
        return len(ids)
