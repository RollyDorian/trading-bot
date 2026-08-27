"""Capacity policy for continuous RAW generation rotation on a constrained VPS.

Production operator emergency floor remains 5 GiB in this milestone and is not
lowered here. Archive tooling still keeps the immutable 3 GiB operational floor.

Generation size calibration prefers the first production generation DROP
measurement (203546624 B ≈ 194.1 MiB for 400000 rows) over the earlier canary
density projection when estimating backlog headroom.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from trading_bot.storage.partitions import DEFAULT_GENERATION_ROW_SPAN

# Immutable floors for this milestone (bytes).
OPERATOR_EMERGENCY_FLOOR_BYTES = 5 * 1024**3
HARD_ARCHIVE_FLOOR_BYTES = 3 * 1024**3

# First production generation physical size after FULL close (DROP evidence).
MEASURED_GENERATION_BYTES = 203_546_624
MEASURED_GENERATION_ROWS = 400_000
MEASURED_BYTES_PER_EVENT = MEASURED_GENERATION_BYTES / MEASURED_GENERATION_ROWS

# Events/hour remains the canary/production rate projection (~59k/h).
PRELIMINARY_EVENTS_PER_HOUR = 59_000.0
# Keep alias so older call sites continue to import a density constant.
PRELIMINARY_BYTES_PER_EVENT = MEASURED_BYTES_PER_EVENT

# Transient/WAL cushion while ACTIVE grows and archive runs.
WAL_AND_TRANSIENT_BUFFER_BYTES = 128 * 1024 * 1024
# Prefer archiving before free space can no longer hold one closed generation.
ARCHIVE_PRESSURE_CLOSED_HEADROOM_FACTOR = 1.25

# Bounded DROP backlog (manual DROP only). Conservative for ~6.2 GiB free /
# 5 GiB floor (~1.2 GiB headroom): allow at most one CLOSED* and two
# DROP_ELIGIBLE (~388 MiB) while ACTIVE may still grow to ~194 MiB.
MAX_CLOSED_UNARCHIVED_GENERATIONS = 1
MAX_DROP_ELIGIBLE_GENERATIONS = 2
MAX_LOCAL_HISTORICAL_GENERATION_BYTES = (
    MAX_CLOSED_UNARCHIVED_GENERATIONS + MAX_DROP_ELIGIBLE_GENERATIONS
) * MEASURED_GENERATION_BYTES


class CapacityState(StrEnum):
    """Conservative capacity states for continuous collection."""

    READY = "READY"
    ARCHIVE_PRESSURE = "ARCHIVE_PRESSURE"
    DROP_APPROVAL_REQUIRED = "DROP_APPROVAL_REQUIRED"
    STOP_REQUIRED = "STOP_REQUIRED"


@dataclass(frozen=True, slots=True)
class CapacityInputs:
    free_disk_bytes: int
    bytes_per_event: float = MEASURED_BYTES_PER_EVENT
    events_per_hour: float = PRELIMINARY_EVENTS_PER_HOUR
    generation_row_span: int = DEFAULT_GENERATION_ROW_SPAN
    closed_unarchived_count: int = 0
    closed_unarchived_bytes: int = 0
    drop_eligible_count: int = 0
    drop_eligible_bytes: int = 0
    active_generation_bytes: int = 0
    operator_emergency_floor_bytes: int = OPERATOR_EMERGENCY_FLOOR_BYTES
    hard_archive_floor_bytes: int = HARD_ARCHIVE_FLOOR_BYTES
    wal_buffer_bytes: int = WAL_AND_TRANSIENT_BUFFER_BYTES
    max_closed_unarchived: int = MAX_CLOSED_UNARCHIVED_GENERATIONS
    max_drop_eligible: int = MAX_DROP_ELIGIBLE_GENERATIONS
    measured_generation_bytes: int = MEASURED_GENERATION_BYTES


@dataclass(frozen=True, slots=True)
class CapacityAssessment:
    state: CapacityState
    free_disk_bytes: int
    estimated_generation_bytes: int
    hours_per_generation: float
    ready_requires_bytes: int
    archive_pressure_below_bytes: int
    headroom_above_emergency_floor_bytes: int
    continuous_operation_feasible: bool
    safe_drop_eligible_backlog: int
    projected_local_historical_bytes: int
    reasons: tuple[str, ...]
    assumptions: tuple[str, ...]


def estimate_generation_bytes(
    *,
    bytes_per_event: float = MEASURED_BYTES_PER_EVENT,
    row_span: int = DEFAULT_GENERATION_ROW_SPAN,
) -> int:
    if bytes_per_event <= 0 or row_span < 1:
        raise ValueError("bytes_per_event and row_span must be positive")
    return int(bytes_per_event * row_span)


def hours_per_generation(
    *,
    events_per_hour: float = PRELIMINARY_EVENTS_PER_HOUR,
    row_span: int = DEFAULT_GENERATION_ROW_SPAN,
) -> float:
    if events_per_hour <= 0 or row_span < 1:
        raise ValueError("events_per_hour and row_span must be positive")
    return row_span / events_per_hour


def safe_coexisting_generation_budget(*, free_disk_bytes: int) -> int:
    """How many full ~194 MiB generations fit above the 5 GiB floor (conservative)."""

    headroom = free_disk_bytes - OPERATOR_EMERGENCY_FLOOR_BYTES
    if headroom <= 0:
        return 0
    # Reserve one ACTIVE growth slot + WAL before counting backlog slots.
    usable = headroom - MEASURED_GENERATION_BYTES - WAL_AND_TRANSIENT_BUFFER_BYTES
    if usable < 0:
        return 0
    return usable // MEASURED_GENERATION_BYTES


def assess_capacity(inputs: CapacityInputs) -> CapacityAssessment:
    """Classify capacity without mutating thresholds or freeing disk."""

    gen_bytes = estimate_generation_bytes(
        bytes_per_event=inputs.bytes_per_event,
        row_span=inputs.generation_row_span,
    )
    # Prefer measured closed size when estimating known backlog pressure.
    measured = max(inputs.measured_generation_bytes, 1)
    hours = hours_per_generation(
        events_per_hour=inputs.events_per_hour,
        row_span=inputs.generation_row_span,
    )
    # READY needs emergency floor + one full closed generation + WAL cushion.
    # Assumption: archive does not free disk until operator DROP after verify.
    ready_requires = (
        inputs.operator_emergency_floor_bytes
        + gen_bytes
        + inputs.wal_buffer_bytes
    )
    archive_pressure_below = (
        inputs.operator_emergency_floor_bytes
        + int(ARCHIVE_PRESSURE_CLOSED_HEADROOM_FACTOR * gen_bytes)
    )
    headroom = inputs.free_disk_bytes - inputs.operator_emergency_floor_bytes
    closed_bytes = inputs.closed_unarchived_bytes
    if inputs.closed_unarchived_count > 0 and closed_bytes <= 0:
        closed_bytes = inputs.closed_unarchived_count * measured
    drop_bytes = inputs.drop_eligible_bytes
    if inputs.drop_eligible_count > 0 and drop_bytes <= 0:
        drop_bytes = inputs.drop_eligible_count * measured
    historical_bytes = closed_bytes + drop_bytes
    safe_backlog = min(
        inputs.max_drop_eligible,
        safe_coexisting_generation_budget(free_disk_bytes=inputs.free_disk_bytes),
    )
    reasons: list[str] = []
    assumptions = (
        "generation bytes calibrated from production DROP "
        f"({MEASURED_GENERATION_BYTES} B / {MEASURED_GENERATION_ROWS} rows)",
        "events_per_hour is preliminary canary/production projection (~59k/h)",
        "archive does not reclaim filesystem until verified DROP",
        "at most one CLOSED_UNARCHIVED/ARCHIVING generation by default",
        f"at most {MAX_DROP_ELIGIBLE_GENERATIONS} DROP_ELIGIBLE generations "
        "before STOP_REQUIRED",
        "operator emergency floor remains 5 GiB (not lowered here)",
        "hard archive floor remains 3 GiB",
        "physical DROP remains human-approved only",
    )

    if inputs.free_disk_bytes < inputs.hard_archive_floor_bytes:
        reasons.append("free disk below hard archive floor (3 GiB)")
        state = CapacityState.STOP_REQUIRED
    elif inputs.free_disk_bytes < inputs.operator_emergency_floor_bytes:
        reasons.append("free disk below operator emergency floor (5 GiB)")
        state = CapacityState.STOP_REQUIRED
    elif inputs.closed_unarchived_count > inputs.max_closed_unarchived:
        reasons.append(
            "closed/unarchived generation backlog exceeds bounded policy "
            f"({inputs.closed_unarchived_count} > {inputs.max_closed_unarchived})"
        )
        state = CapacityState.STOP_REQUIRED
    elif inputs.drop_eligible_count > inputs.max_drop_eligible:
        reasons.append(
            "DROP_ELIGIBLE backlog exceeds bounded policy "
            f"({inputs.drop_eligible_count} > {inputs.max_drop_eligible}); "
            "operator must DROP before collection continues"
        )
        state = CapacityState.STOP_REQUIRED
    elif (
        inputs.closed_unarchived_count > 0
        and headroom < closed_bytes + inputs.wal_buffer_bytes
    ):
        reasons.append(
            "closed unarchived generation leaves insufficient emergency headroom"
        )
        state = CapacityState.STOP_REQUIRED
    elif historical_bytes + gen_bytes + inputs.wal_buffer_bytes > max(headroom, 0):
        reasons.append(
            "local historical generations plus ACTIVE/WAL no longer fit above "
            "the emergency floor; STOP_REQUIRED until operator DROP"
        )
        state = CapacityState.STOP_REQUIRED
    elif inputs.closed_unarchived_count > 0:
        state = CapacityState.ARCHIVE_PRESSURE
        reasons.append("closed generation pending archive/verify")
    elif inputs.drop_eligible_count > 0:
        reasons.append(
            "verified DROP_ELIGIBLE generation(s) require explicit "
            "DROP_VERIFIED_GENERATION approval (one generation per authorization)"
        )
        state = CapacityState.DROP_APPROVAL_REQUIRED
    elif inputs.free_disk_bytes < ready_requires:
        reasons.append(
            "free disk cannot hold emergency floor + one closed generation + WAL buffer"
        )
        state = CapacityState.ARCHIVE_PRESSURE
        if inputs.free_disk_bytes < archive_pressure_below:
            reasons.append("prioritize archive/verify and operator DROP approval")
    else:
        state = CapacityState.READY
        reasons.append("free disk covers emergency floor + one closed generation + WAL")

    continuous = (
        state
        in {
            CapacityState.READY,
            CapacityState.DROP_APPROVAL_REQUIRED,
            CapacityState.ARCHIVE_PRESSURE,
        }
        and headroom >= gen_bytes + inputs.wal_buffer_bytes
        and inputs.closed_unarchived_count <= inputs.max_closed_unarchived
        and inputs.drop_eligible_count <= inputs.max_drop_eligible
        and historical_bytes + gen_bytes + inputs.wal_buffer_bytes <= max(headroom, 0)
    )
    # ARCHIVE_PRESSURE may still collect if successor cover exists, but only when
    # measured historical + ACTIVE growth still fits above the emergency floor.
    if state == CapacityState.ARCHIVE_PRESSURE and inputs.closed_unarchived_count > 0:
        continuous = continuous and headroom >= closed_bytes + gen_bytes + inputs.wal_buffer_bytes
    if not continuous:
        reasons.append(
            "continuous full-generation cycle is not feasible at current free disk "
            "without reclaiming a verified closed generation or adding capacity"
        )

    return CapacityAssessment(
        state=state,
        free_disk_bytes=inputs.free_disk_bytes,
        estimated_generation_bytes=gen_bytes,
        hours_per_generation=hours,
        ready_requires_bytes=ready_requires,
        archive_pressure_below_bytes=archive_pressure_below,
        headroom_above_emergency_floor_bytes=headroom,
        continuous_operation_feasible=continuous,
        safe_drop_eligible_backlog=safe_backlog,
        projected_local_historical_bytes=historical_bytes,
        reasons=tuple(reasons),
        assumptions=assumptions,
    )


def normal_ready_target_bytes(
    *,
    floor_bytes: int = OPERATOR_EMERGENCY_FLOOR_BYTES,
    generation_bytes: int = MEASURED_GENERATION_BYTES,
    wal_bytes: int = WAL_AND_TRANSIENT_BUFFER_BYTES,
) -> int:
    """Free bytes required before COLLECT may resume after a capacity pause."""

    return floor_bytes + generation_bytes + wal_bytes


def collector_must_pause_for_capacity(state: CapacityState) -> bool:
    """Capacity STOP must arrest COLLECT. Provisioning still runs first."""

    return state == CapacityState.STOP_REQUIRED


def collector_may_resume_after_capacity(
    *,
    state: CapacityState,
    free_disk_bytes: int,
    collect_hold: bool,
    ready_target_bytes: int | None = None,
) -> bool:
    """Resume is explicit and requires READY margin, not merely above 5 GiB."""

    if collect_hold:
        return False
    target = ready_target_bytes if ready_target_bytes is not None else normal_ready_target_bytes()
    return state == CapacityState.READY and free_disk_bytes >= target


def drop_backlog_blocks_new_archive(
    drop_eligible_count: int,
    *,
    limit: int = MAX_DROP_ELIGIBLE_GENERATIONS,
) -> bool:
    """True when auto-archive must not start or resume another generation.

    Physical DROP stays human-approved. The bounded queue is
    ``DROP_ELIGIBLE <= 2``. A new archive would push the queue to 3, so the
    executor must persist ``DROP_BACKLOG_LIMIT`` and leave CLOSED/FAILED
    generations intact until an operator DROP lowers the count.
    """

    if drop_eligible_count < 0:
        raise ValueError("drop_eligible_count must be >= 0")
    if limit < 1:
        raise ValueError("DROP_ELIGIBLE limit must be >= 1")
    return drop_eligible_count >= limit


def emergency_archive_allowed(
    free_disk_bytes: int,
    *,
    floor_bytes: int = HARD_ARCHIVE_FLOOR_BYTES,
    max_bundle_bytes: int = 64 * 1024 * 1024,
) -> bool:
    """Emergency archive (COLLECT paused) may use the 3 GiB operational floor.

    After worst-case local write + verify download, remaining free must stay
    strictly above the emergency archive floor. Does not change the 5 GiB
    operator floor for normal COLLECT.
    """

    if free_disk_bytes <= floor_bytes:
        return False
    remaining_after_temp = free_disk_bytes - 2 * max_bundle_bytes
    return remaining_after_temp > floor_bytes
