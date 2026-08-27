"""Read-only operator status for continuous RAW generation rotation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from trading_bot.storage.capacity import (
    MEASURED_BYTES_PER_EVENT,
    MEASURED_GENERATION_BYTES,
    PRELIMINARY_EVENTS_PER_HOUR,
    CapacityAssessment,
    CapacityInputs,
    CapacityState,
    assess_capacity,
    estimate_generation_bytes,
    hours_per_generation,
)
from trading_bot.storage.partitions import (
    COVER_EXHAUSTION_STOP_ROWS,
    DEFAULT_GENERATION_ROW_SPAN,
    DROP_GENERATION_CONFIRMATION_TOKEN,
    LATE_PROVISION_ROWS,
    PRE_BOUNDARY_PROVISION_ROWS,
    GenerationRecord,
    GenerationState,
    ProvisionUrgency,
    assess_provision_urgency,
    get_active_generation,
    list_generations,
    measure_relation_size,
    read_sequence_cursor,
)


class OperatorAction(StrEnum):
    NONE = "none"
    PROVISION = "provision"
    PROVISION_LATE = "provision_late"
    ARCHIVE = "archive"
    DROP_APPROVAL_REQUIRED = "drop_approval_required"
    # Writable cover exhausted / near exhaustion without successor (partition miss risk).
    COVER_STOP_REQUIRED = "cover_stop_required"
    STOP_REQUIRED = "stop_required"


@dataclass(frozen=True, slots=True)
class ActiveGenerationStatus:
    generation_key: str
    partition_name: str
    id_start: int
    id_end: int
    rows_used: int
    row_span: int
    percent_full: float
    physical_mib: float
    remaining_ids: int
    estimated_hours_to_boundary: float | None


@dataclass(frozen=True, slots=True)
class ClosedGenerationStatus:
    generation_key: str
    state: str
    archive_status: str
    physical_mib: float | None
    id_start: int
    id_end: int


@dataclass(frozen=True, slots=True)
class DropEligibleCandidate:
    """Exact human DROP authorization payload (one generation per approval)."""

    generation_key: str
    partition_name: str
    id_start: int
    id_end: int
    row_span: int
    physical_mib: float | None
    archive_evidence_sha256: str | None
    required_token: str
    estimated_reclaim_mib: float | None


@dataclass(frozen=True, slots=True)
class SuccessorStatus:
    state: str
    generation_key: str | None
    partition_name: str | None
    id_start: int | None
    id_end: int | None


@dataclass(frozen=True, slots=True)
class OperatorStatusReport:
    collector: str
    active: ActiveGenerationStatus | None
    successor: SuccessorStatus
    closed: tuple[ClosedGenerationStatus, ...]
    drop_eligible: tuple[DropEligibleCandidate, ...]
    storage: dict[str, Any]
    b2: dict[str, Any]
    capacity: dict[str, Any]
    action: OperatorAction
    provision_urgency: ProvisionUrgency = ProvisionUrgency.NORMAL
    next_id: int | None = None
    expected_successor: dict[str, Any] | None = None
    last_provision_attempt_utc: str | None = None
    last_provision_error: str | None = None
    read_only: bool = True


def _archive_status_label(state: GenerationState) -> str:
    mapping = {
        GenerationState.CLOSED_UNARCHIVED: "pending_archive",
        GenerationState.ARCHIVING: "archiving",
        GenerationState.ARCHIVE_FAILED: "archive_failed",
        GenerationState.VERIFY_FAILED: "verify_failed",
        GenerationState.VERIFIED: "verified",
        GenerationState.DROP_ELIGIBLE: "drop_eligible",
        GenerationState.DROPPED: "dropped",
    }
    return mapping.get(state, state.value.lower())


def recommend_action(
    *,
    capacity_state: CapacityState,
    active: GenerationRecord | None,
    remaining_ids: int | None,
    provision_threshold: int,
    has_successor: bool,
    closed_pending_archive: bool,
    drop_eligible: bool,
    late_threshold: int = LATE_PROVISION_ROWS,
    cover_stop_threshold: int = COVER_EXHAUSTION_STOP_ROWS,
) -> OperatorAction:
    """Prefer missing-successor cover actions over capacity STOP.

    Incident lesson: archive/disk STOP_REQUIRED must not hide a missing
    successor; otherwise maintain exits before CREATE TABLE and inserts fail.
    """

    urgency = assess_provision_urgency(
        remaining_ids=remaining_ids,
        has_successor=has_successor,
        provision_threshold=provision_threshold,
        late_threshold=late_threshold,
        cover_stop_threshold=cover_stop_threshold,
    )
    if active is not None and urgency == ProvisionUrgency.COVER_STOP_REQUIRED:
        return OperatorAction.COVER_STOP_REQUIRED
    if active is not None and urgency == ProvisionUrgency.PROVISION_LATE:
        return OperatorAction.PROVISION_LATE
    if active is not None and urgency == ProvisionUrgency.PROVISION_REQUIRED:
        return OperatorAction.PROVISION
    if capacity_state == CapacityState.STOP_REQUIRED:
        return OperatorAction.STOP_REQUIRED
    if closed_pending_archive or capacity_state == CapacityState.ARCHIVE_PRESSURE:
        return OperatorAction.ARCHIVE
    if drop_eligible or capacity_state == CapacityState.DROP_APPROVAL_REQUIRED:
        return OperatorAction.DROP_APPROVAL_REQUIRED
    return OperatorAction.NONE


async def build_operator_status(
    connection: AsyncConnection | AsyncSession,
    *,
    free_disk_bytes: int,
    wal_bytes: int | None = None,
    bytes_per_event: float = MEASURED_BYTES_PER_EVENT,
    events_per_hour: float = PRELIMINARY_EVENTS_PER_HOUR,
    provision_threshold: int = PRE_BOUNDARY_PROVISION_ROWS,
    late_threshold: int = LATE_PROVISION_ROWS,
    cover_stop_threshold: int = COVER_EXHAUSTION_STOP_ROWS,
    latest_verified_generation_key: str | None = None,
    collector_state: str = "unknown",
    last_provision_attempt_utc: str | None = None,
    last_provision_error: str | None = None,
) -> OperatorStatusReport:
    """Assemble a single read-only operator view (no mutations)."""

    generations = await list_generations(connection)
    active = await get_active_generation(connection)
    cursor = await read_sequence_cursor(connection)
    active_status: ActiveGenerationStatus | None = None
    remaining: int | None = None
    expected_successor: dict[str, Any] | None = None
    successor = SuccessorStatus(
        state="MISSING",
        generation_key=None,
        partition_name=None,
        id_start=None,
        id_end=None,
    )
    if active is not None:
        size = await measure_relation_size(connection, active.partition_name)
        # Keep signed remaining so past-boundary uncovered ids stay visible.
        remaining = active.id_end - cursor.next_id
        rows_used = max(cursor.next_id - active.id_start, 0)
        # Cap displayed rows_used at span if sequence already crossed (rotation lag).
        rows_used = min(rows_used, active.row_span)
        percent = (100.0 * rows_used / active.row_span) if active.row_span else 0.0
        display_remaining = max(remaining, 0)
        hours = (
            display_remaining / events_per_hour if events_per_hour > 0 else None
        )
        active_status = ActiveGenerationStatus(
            generation_key=active.generation_key,
            partition_name=active.partition_name,
            id_start=active.id_start,
            id_end=active.id_end,
            rows_used=rows_used,
            row_span=active.row_span,
            percent_full=round(percent, 2),
            physical_mib=round(size.total_bytes / (1024 * 1024), 3),
            remaining_ids=remaining,
            estimated_hours_to_boundary=(
                round(hours, 2) if hours is not None else None
            ),
        )
        expected_successor = {
            "partition_name": f"market_events_g_{active.id_end}",
            "generation_key": f"g_{active.id_end}_{active.id_end + active.row_span}",
            "id_start": active.id_end,
            "id_end": active.id_end + active.row_span,
            "exists": False,
        }
        candidate = next(
            (
                g
                for g in generations
                if g.id_start == active.id_end and g.state != GenerationState.DROPPED
            ),
            None,
        )
        if candidate is not None:
            successor = SuccessorStatus(
                state=candidate.state.value,
                generation_key=candidate.generation_key,
                partition_name=candidate.partition_name,
                id_start=candidate.id_start,
                id_end=candidate.id_end,
            )
            expected_successor["exists"] = True
            expected_successor["generation_key"] = candidate.generation_key
            expected_successor["partition_name"] = candidate.partition_name
            expected_successor["id_start"] = candidate.id_start
            expected_successor["id_end"] = candidate.id_end

    closed_statuses: list[ClosedGenerationStatus] = []
    drop_candidates: list[DropEligibleCandidate] = []
    closed_unarchived_bytes = 0
    closed_unarchived_count = 0
    drop_eligible_bytes = 0
    drop_eligible_count = 0
    for generation in generations:
        if generation.state in {
            GenerationState.PROVISIONED,
            GenerationState.ACTIVE,
            GenerationState.DROPPED,
        }:
            continue
        physical: float | None = None
        physical_bytes = generation.physical_bytes_at_close
        if physical_bytes is None and generation.state == GenerationState.DROP_ELIGIBLE:
            # Fall back to measured production generation when metadata lacks size.
            physical_bytes = MEASURED_GENERATION_BYTES
        if physical_bytes is not None:
            physical = round(physical_bytes / (1024 * 1024), 3)
            if generation.state in {
                GenerationState.CLOSED_UNARCHIVED,
                GenerationState.ARCHIVING,
                GenerationState.ARCHIVE_FAILED,
                GenerationState.VERIFY_FAILED,
            }:
                closed_unarchived_count += 1
                closed_unarchived_bytes += physical_bytes
            if generation.state == GenerationState.DROP_ELIGIBLE:
                drop_eligible_count += 1
                drop_eligible_bytes += physical_bytes
        closed_statuses.append(
            ClosedGenerationStatus(
                generation_key=generation.generation_key,
                state=generation.state.value,
                archive_status=_archive_status_label(generation.state),
                physical_mib=physical,
                id_start=generation.id_start,
                id_end=generation.id_end,
            )
        )
        if generation.state == GenerationState.DROP_ELIGIBLE:
            drop_candidates.append(
                DropEligibleCandidate(
                    generation_key=generation.generation_key,
                    partition_name=generation.partition_name,
                    id_start=generation.id_start,
                    id_end=generation.id_end,
                    row_span=generation.row_span,
                    physical_mib=physical,
                    archive_evidence_sha256=generation.archive_evidence_sha256,
                    required_token=DROP_GENERATION_CONFIRMATION_TOKEN,
                    estimated_reclaim_mib=physical,
                )
            )

    active_bytes = 0
    if active is not None:
        active_bytes = (
            await measure_relation_size(connection, active.partition_name)
        ).total_bytes

    assessment = assess_capacity(
        CapacityInputs(
            free_disk_bytes=free_disk_bytes,
            bytes_per_event=bytes_per_event,
            events_per_hour=events_per_hour,
            generation_row_span=DEFAULT_GENERATION_ROW_SPAN,
            closed_unarchived_count=closed_unarchived_count,
            closed_unarchived_bytes=closed_unarchived_bytes,
            drop_eligible_count=drop_eligible_count,
            drop_eligible_bytes=drop_eligible_bytes,
            active_generation_bytes=active_bytes,
            wal_buffer_bytes=max(wal_bytes or 0, 128 * 1024 * 1024),
        )
    )
    pending_archive = any(
        g.state
        in {
            GenerationState.CLOSED_UNARCHIVED,
            GenerationState.ARCHIVE_FAILED,
            GenerationState.VERIFY_FAILED,
        }
        for g in generations
    )
    drop_eligible = drop_eligible_count > 0
    has_successor = successor.state != "MISSING"
    urgency = assess_provision_urgency(
        remaining_ids=remaining,
        has_successor=has_successor,
        provision_threshold=provision_threshold,
        late_threshold=late_threshold,
        cover_stop_threshold=cover_stop_threshold,
    )
    action = recommend_action(
        capacity_state=assessment.state,
        active=active,
        remaining_ids=remaining,
        provision_threshold=provision_threshold,
        has_successor=has_successor,
        closed_pending_archive=pending_archive,
        drop_eligible=drop_eligible,
        late_threshold=late_threshold,
        cover_stop_threshold=cover_stop_threshold,
    )
    gen_est = estimate_generation_bytes(bytes_per_event=bytes_per_event)
    collector_label = collector_state.strip().upper() or "UNKNOWN"
    return OperatorStatusReport(
        collector=collector_label,
        active=active_status,
        successor=successor,
        closed=tuple(closed_statuses),
        drop_eligible=tuple(drop_candidates),
        storage={
            "filesystem_free_gib": round(free_disk_bytes / (1024**3), 3),
            "hard_reserve_gib": 5.0,
            "margin_above_reserve_gib": round(
                assessment.headroom_above_emergency_floor_bytes / (1024**3), 3
            ),
            "wal_mib": (
                round(wal_bytes / (1024 * 1024), 3) if wal_bytes is not None else None
            ),
            "wal_status": (
                "normal"
                if wal_bytes is None or wal_bytes <= 256 * 1024 * 1024
                else "anomalous"
            ),
            "projected_generation_mib": round(gen_est / (1024 * 1024), 1),
            "measured_generation_mib": round(
                MEASURED_GENERATION_BYTES / (1024 * 1024), 1
            ),
            "hours_per_generation": round(
                hours_per_generation(events_per_hour=events_per_hour), 2
            ),
            "projected_safe_headroom_mib": round(
                assessment.headroom_above_emergency_floor_bytes / (1024 * 1024), 1
            ),
            "safe_drop_eligible_backlog": assessment.safe_drop_eligible_backlog,
        },
        b2={
            "latest_verified_generation": latest_verified_generation_key
            or next(
                (
                    g.generation_key
                    for g in reversed(generations)
                    if g.state
                    in {
                        GenerationState.VERIFIED,
                        GenerationState.DROP_ELIGIBLE,
                        GenerationState.DROPPED,
                    }
                    and g.archive_evidence_sha256
                ),
                None,
            ),
        },
        capacity=_capacity_public(assessment),
        action=action,
        provision_urgency=urgency,
        next_id=cursor.next_id,
        expected_successor=expected_successor,
        last_provision_attempt_utc=last_provision_attempt_utc,
        last_provision_error=last_provision_error,
        read_only=True,
    )


def _capacity_public(assessment: CapacityAssessment) -> dict[str, Any]:
    payload = asdict(assessment)
    payload["state"] = assessment.state.value
    return payload


def format_operator_status_text(report: OperatorStatusReport) -> str:
    """Human-oriented read-only status block for operators."""

    lines: list[str] = [f"COLLECTOR: {report.collector}"]
    if report.active is None:
        lines.append("ACTIVE: NONE")
    else:
        a = report.active
        lines.extend(
            [
                f"ACTIVE: {a.generation_key}",
                f"  partition={a.partition_name}",
                f"  rows={a.rows_used}/{a.row_span} ({a.percent_full}%)",
                f"  physical_mib={a.physical_mib}",
                f"  remaining_ids={a.remaining_ids}",
                f"  estimated_hours={a.estimated_hours_to_boundary}",
            ]
        )
    if report.next_id is not None:
        lines.append(f"NEXT_ID: {report.next_id}")
    lines.append(f"PROVISION_URGENCY: {report.provision_urgency.value}")
    s = report.successor
    if s.state == "MISSING":
        lines.append("SUCCESSOR: MISSING")
    else:
        lines.append(
            f"SUCCESSOR: {s.state} {s.generation_key} "
            f"[{s.id_start},{s.id_end})"
        )
    if report.expected_successor is not None:
        exp = report.expected_successor
        lines.append(
            "EXPECTED_SUCCESSOR: "
            f"{exp.get('generation_key')} [{exp.get('id_start')},{exp.get('id_end')}) "
            f"exists={exp.get('exists')}"
        )
    if report.last_provision_attempt_utc or report.last_provision_error:
        lines.append(
            f"LAST_PROVISION_ATTEMPT: utc={report.last_provision_attempt_utc} "
            f"error={report.last_provision_error}"
        )
    if not report.closed:
        lines.append("CLOSED: none")
    else:
        lines.append("CLOSED:")
        for item in report.closed:
            lines.append(
                f"  {item.generation_key} state={item.state} "
                f"archive={item.archive_status} mib={item.physical_mib}"
            )
    if not report.drop_eligible:
        lines.append("DROP_ELIGIBLE: none")
    else:
        lines.append("DROP_ELIGIBLE:")
        for candidate in report.drop_eligible:
            lines.append(
                f"  {candidate.partition_name} "
                f"range=[{candidate.id_start},{candidate.id_end}) "
                f"mib={candidate.physical_mib} "
                f"reclaim_mib={candidate.estimated_reclaim_mib} "
                f"evidence={candidate.archive_evidence_sha256} "
                f"token={candidate.required_token}"
            )
    lines.extend(
        [
            f"FILESYSTEM: free_gib={report.storage.get('filesystem_free_gib')} "
            f"hard_reserve_gib={report.storage.get('hard_reserve_gib')} "
            f"margin_gib={report.storage.get('margin_above_reserve_gib')}",
            f"WAL: mib={report.storage.get('wal_mib')} "
            f"status={report.storage.get('wal_status')}",
            f"CAPACITY: {report.capacity.get('state')}",
            f"ACTION: {report.action.value.upper()}",
        ]
    )
    return "\n".join(lines)


def operator_status_to_dict(report: OperatorStatusReport) -> dict[str, Any]:
    """JSON-friendly projection for CLI/status scripts."""

    return {
        "collector": report.collector,
        "active": asdict(report.active) if report.active else None,
        "successor": asdict(report.successor),
        "closed": [asdict(c) for c in report.closed],
        "drop_eligible": [asdict(c) for c in report.drop_eligible],
        "storage": report.storage,
        "b2": report.b2,
        "capacity": report.capacity,
        "action": report.action.value,
        "provision_urgency": report.provision_urgency.value,
        "next_id": report.next_id,
        "expected_successor": report.expected_successor,
        "last_provision_attempt_utc": report.last_provision_attempt_utc,
        "last_provision_error": report.last_provision_error,
        "read_only": report.read_only,
    }
