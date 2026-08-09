"""Bounded RAW ``market_events`` generation (partition) lifecycle.

Physical DROP of a verified closed generation returns relation files to the
filesystem. Ordinary DELETE remains available as an emergency/legacy path and
must not be treated as space reclamation.

Destructive DROP stays operator-gated; this module never enables unattended
production DROP.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

# Deterministic id-span boundary. Physical size is an additional safety signal.
DEFAULT_GENERATION_ROW_SPAN = 400_000
# Soft physical ceiling calibrated from production (~533 B/row total ≈ 200 MiB
# at 400k) with headroom under the 200–300 MiB active-generation target.
GENERATION_PHYSICAL_SOFT_LIMIT_BYTES = 300 * 1024 * 1024
# Provision the next empty generation before the active range is exhausted.
PRE_BOUNDARY_PROVISION_ROWS = 50_000
# Fail closed if fewer than this many ids remain without a writable successor.
MINIMUM_WRITABLE_HEADROOM_ROWS = 1

MARKET_EVENTS_SEQUENCE = "market_events_id_seq"
GENERATIONS_TABLE = "market_event_generations"
PARENT_TABLE = "market_events"

# Operator confirmation for the maintenance DROP path (never automatic).
DROP_GENERATION_CONFIRMATION_TOKEN = "DROP_VERIFIED_GENERATION"


class GenerationState(StrEnum):
    """Durable generation lifecycle states."""

    PROVISIONED = "PROVISIONED"
    ACTIVE = "ACTIVE"
    CLOSED_UNARCHIVED = "CLOSED_UNARCHIVED"
    ARCHIVING = "ARCHIVING"
    VERIFIED = "VERIFIED"
    DROP_ELIGIBLE = "DROP_ELIGIBLE"
    DROPPED = "DROPPED"
    ARCHIVE_FAILED = "ARCHIVE_FAILED"
    VERIFY_FAILED = "VERIFY_FAILED"


# States that may receive collector inserts after activation / range routing.
INSERT_TARGET_STATES = frozenset({GenerationState.ACTIVE, GenerationState.PROVISIONED})


class PartitionLifecycleError(RuntimeError):
    """Fail-closed partition / generation lifecycle error."""


@dataclass(frozen=True, slots=True)
class SequenceCursor:
    """Next id the global sequence will assign without advancing it."""

    last_value: int
    is_called: bool

    @property
    def next_id(self) -> int:
        return self.last_value + 1 if self.is_called else self.last_value


@dataclass(frozen=True, slots=True)
class GenerationRecord:
    generation_key: str
    partition_name: str
    id_start: int
    id_end: int
    state: GenerationState
    row_span: int
    physical_bytes_at_close: int | None
    archive_evidence_sha256: str | None
    closed_at: datetime | None
    verified_at: datetime | None
    drop_eligible_at: datetime | None
    dropped_at: datetime | None


@dataclass(frozen=True, slots=True)
class GenerationArchiveEvidence:
    """Storage-integrity evidence required before DROP_ELIGIBLE.

    Research quality / paper-admission status must not appear here and must
    not control deletion.
    """

    generation_key: str
    min_raw_event_id: int
    max_raw_event_id: int
    expected_row_count: int
    observed_row_count: int
    checksums_pass: bool
    manifest_pass: bool
    remote_completed: bool
    download_verification_pass: bool
    storage_reconciliation_pass: bool
    id_coverage_contiguous: bool
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class RelationSizeSample:
    relation: str
    heap_bytes: int
    index_bytes: int
    total_bytes: int


def partition_name_for_start(id_start: int) -> str:
    return f"market_events_g_{id_start}"


def generation_key_for_range(id_start: int, id_end: int) -> str:
    return f"g_{id_start}_{id_end}"


def aligned_generation_bounds(
    first_id: int,
    *,
    row_span: int = DEFAULT_GENERATION_ROW_SPAN,
) -> tuple[int, int]:
    if first_id < 1:
        raise ValueError("first_id must be >= 1")
    if row_span < 1:
        raise ValueError("row_span must be >= 1")
    return first_id, first_id + row_span


async def read_sequence_cursor(
    connection: AsyncConnection | AsyncSession,
) -> SequenceCursor:
    row = (
        await connection.execute(
            text(
                f"""
                SELECT last_value, is_called
                FROM {MARKET_EVENTS_SEQUENCE}
                """
            )
        )
    ).one()
    return SequenceCursor(last_value=int(row.last_value), is_called=bool(row.is_called))


async def is_market_events_partitioned(
    connection: AsyncConnection | AsyncSession,
) -> bool:
    value = await connection.scalar(
        text(
            """
            SELECT c.relkind = 'p'
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = :table
            """
        ),
        {"table": PARENT_TABLE},
    )
    return bool(value)


async def measure_relation_size(
    connection: AsyncConnection | AsyncSession,
    relation: str,
) -> RelationSizeSample:
    """Measure heap/index/total bytes for a table or partitioned parent.

    Partitioned parents store no heap themselves; this sums attached partitions
    so capacity math matches operator expectations.
    """

    partitioned_sum = (
        await connection.execute(
            text(
                """
                SELECT
                    COALESCE(SUM(pg_relation_size(child.oid)), 0) AS heap_bytes,
                    COALESCE(SUM(pg_indexes_size(child.oid)), 0) AS index_bytes,
                    COALESCE(SUM(pg_total_relation_size(child.oid)), 0) AS total_bytes
                FROM pg_class parent
                JOIN pg_namespace n ON n.oid = parent.relnamespace
                JOIN pg_inherits i ON i.inhparent = parent.oid
                JOIN pg_class child ON child.oid = i.inhrelid
                WHERE n.nspname = 'public' AND parent.relname = :rel
                """
            ),
            {"rel": relation},
        )
    ).one()
    if relation == PARENT_TABLE and await is_market_events_partitioned(connection):
        return RelationSizeSample(
            relation=relation,
            heap_bytes=int(partitioned_sum.heap_bytes or 0),
            index_bytes=int(partitioned_sum.index_bytes or 0),
            total_bytes=int(partitioned_sum.total_bytes or 0),
        )
    if int(partitioned_sum.total_bytes or 0) > 0:
        return RelationSizeSample(
            relation=relation,
            heap_bytes=int(partitioned_sum.heap_bytes or 0),
            index_bytes=int(partitioned_sum.index_bytes or 0),
            total_bytes=int(partitioned_sum.total_bytes or 0),
        )
    row = (
        await connection.execute(
            text(
                """
                SELECT
                    pg_relation_size(to_regclass(:rel)) AS heap_bytes,
                    pg_indexes_size(to_regclass(:rel)) AS index_bytes,
                    pg_total_relation_size(to_regclass(:rel)) AS total_bytes
                """
            ),
            {"rel": f"public.{relation}"},
        )
    ).one()
    return RelationSizeSample(
        relation=relation,
        heap_bytes=int(row.heap_bytes or 0),
        index_bytes=int(row.index_bytes or 0),
        total_bytes=int(row.total_bytes or 0),
    )


async def list_generations(
    connection: AsyncConnection | AsyncSession,
) -> list[GenerationRecord]:
    rows = (
        await connection.execute(
            text(
                f"""
                SELECT generation_key, partition_name, id_start, id_end, state,
                       row_span, physical_bytes_at_close, archive_evidence_sha256,
                       closed_at, verified_at, drop_eligible_at, dropped_at
                FROM {GENERATIONS_TABLE}
                ORDER BY id_start
                """
            )
        )
    ).mappings().all()
    return [
        GenerationRecord(
            generation_key=str(row["generation_key"]),
            partition_name=str(row["partition_name"]),
            id_start=int(row["id_start"]),
            id_end=int(row["id_end"]),
            state=GenerationState(str(row["state"])),
            row_span=int(row["row_span"]),
            physical_bytes_at_close=(
                int(row["physical_bytes_at_close"])
                if row["physical_bytes_at_close"] is not None
                else None
            ),
            archive_evidence_sha256=(
                str(row["archive_evidence_sha256"])
                if row["archive_evidence_sha256"] is not None
                else None
            ),
            closed_at=row["closed_at"],
            verified_at=row["verified_at"],
            drop_eligible_at=row["drop_eligible_at"],
            dropped_at=row["dropped_at"],
        )
        for row in rows
    ]


async def get_active_generation(
    connection: AsyncConnection | AsyncSession,
) -> GenerationRecord | None:
    actives = [g for g in await list_generations(connection) if g.state == GenerationState.ACTIVE]
    if len(actives) > 1:
        raise PartitionLifecycleError("multiple ACTIVE generations are not allowed")
    return actives[0] if actives else None


async def find_generation_for_id(
    connection: AsyncConnection | AsyncSession,
    raw_id: int,
) -> GenerationRecord | None:
    for generation in await list_generations(connection):
        if generation.id_start <= raw_id < generation.id_end:
            return generation
    return None


async def ensure_writable_cover(
    connection: AsyncConnection | AsyncSession,
    *,
    next_id: int | None = None,
    headroom_rows: int = MINIMUM_WRITABLE_HEADROOM_ROWS,
) -> GenerationRecord:
    """Fail closed unless a writable generation covers ``next_id`` (+ headroom)."""

    if next_id is None:
        target_id = (await read_sequence_cursor(connection)).next_id
    else:
        target_id = int(next_id)
    end_needed = target_id + max(headroom_rows, 1) - 1
    covering = await find_generation_for_id(connection, target_id)
    if covering is None:
        raise PartitionLifecycleError(
            f"no generation covers next raw id {target_id}; refusing to accept events"
        )
    if covering.state not in INSERT_TARGET_STATES:
        raise PartitionLifecycleError(
            f"generation {covering.generation_key} state {covering.state} is not writable"
        )
    if end_needed >= covering.id_end:
        successor = await find_generation_for_id(connection, covering.id_end)
        if successor is None or successor.state not in INSERT_TARGET_STATES:
            raise PartitionLifecycleError(
                f"writable headroom exhausted at id {covering.id_end}; "
                "provision the next generation before continuing"
            )
    return covering


def validate_archive_evidence_for_drop(
    generation: GenerationRecord,
    evidence: GenerationArchiveEvidence,
) -> None:
    """Raise unless storage-integrity evidence authorizes DROP_ELIGIBLE."""

    if evidence.generation_key != generation.generation_key:
        raise PartitionLifecycleError("archive evidence generation_key mismatch")
    expected_count = generation.id_end - generation.id_start
    # Closed generations may be partially filled if closed early; evidence must
    # match the archived closed range, not necessarily the full capacity span.
    if evidence.min_raw_event_id < generation.id_start:
        raise PartitionLifecycleError("archive min_raw_event_id below generation start")
    if evidence.max_raw_event_id >= generation.id_end:
        raise PartitionLifecycleError("archive max_raw_event_id outside generation end")
    if evidence.min_raw_event_id > evidence.max_raw_event_id:
        raise PartitionLifecycleError("archive id bounds inverted")
    span = evidence.max_raw_event_id - evidence.min_raw_event_id + 1
    if evidence.expected_row_count != span:
        raise PartitionLifecycleError("expected_row_count does not match id span")
    if evidence.observed_row_count != evidence.expected_row_count:
        raise PartitionLifecycleError("observed_row_count does not match expected_row_count")
    if not evidence.id_coverage_contiguous:
        raise PartitionLifecycleError("archive id coverage is not contiguous")
    if not evidence.checksums_pass:
        raise PartitionLifecycleError("archive checksums did not pass")
    if not evidence.manifest_pass:
        raise PartitionLifecycleError("archive manifest did not pass")
    if not evidence.remote_completed:
        raise PartitionLifecycleError("remote COMPLETED marker missing")
    if not evidence.download_verification_pass:
        raise PartitionLifecycleError("download verification did not pass")
    if not evidence.storage_reconciliation_pass:
        raise PartitionLifecycleError("storage reconciliation did not pass")
    # expected_count retained for operators comparing capacity vs archived rows.
    _ = expected_count


async def mark_generation_state(
    connection: AsyncConnection | AsyncSession,
    generation_key: str,
    new_state: GenerationState,
    *,
    archive_evidence_sha256: str | None = None,
    physical_bytes_at_close: int | None = None,
) -> None:
    now = datetime.now(UTC)
    assignments = ["state = :state", "updated_at = :now"]
    params: dict[str, Any] = {
        "state": new_state.value,
        "now": now,
        "generation_key": generation_key,
    }
    if archive_evidence_sha256 is not None:
        assignments.append("archive_evidence_sha256 = :evidence")
        params["evidence"] = archive_evidence_sha256
    if physical_bytes_at_close is not None:
        assignments.append("physical_bytes_at_close = :physical_bytes")
        params["physical_bytes"] = physical_bytes_at_close
    if new_state == GenerationState.CLOSED_UNARCHIVED:
        assignments.append("closed_at = :now")
    if new_state == GenerationState.VERIFIED:
        assignments.append("verified_at = :now")
    if new_state == GenerationState.DROP_ELIGIBLE:
        assignments.append("drop_eligible_at = :now")
    if new_state == GenerationState.DROPPED:
        assignments.append("dropped_at = :now")
    result = await connection.execute(
        text(
            f"""
            UPDATE {GENERATIONS_TABLE}
            SET {", ".join(assignments)}
            WHERE generation_key = :generation_key
            """
        ),
        params,
    )
    if getattr(result, "rowcount", -1) != 1:
        raise PartitionLifecycleError(f"generation {generation_key} not updated")


async def close_active_generation(
    connection: AsyncConnection | AsyncSession,
    *,
    reason: str,
) -> GenerationRecord:
    """Close the ACTIVE generation; does not archive or drop."""

    active = await get_active_generation(connection)
    if active is None:
        raise PartitionLifecycleError("no ACTIVE generation to close")
    size = await measure_relation_size(connection, active.partition_name)
    await mark_generation_state(
        connection,
        active.generation_key,
        GenerationState.CLOSED_UNARCHIVED,
        physical_bytes_at_close=size.total_bytes,
    )
    # Activate a provisioned successor covering the next id when present.
    cursor = await read_sequence_cursor(connection)
    successor = await find_generation_for_id(connection, cursor.next_id)
    if successor is not None and successor.state == GenerationState.PROVISIONED:
        await mark_generation_state(
            connection,
            successor.generation_key,
            GenerationState.ACTIVE,
        )
    _ = reason
    refreshed = await find_generation_for_id(connection, active.id_start)
    if refreshed is None:
        raise PartitionLifecycleError("closed generation disappeared")
    return refreshed


async def provision_generation(
    connection: AsyncConnection | AsyncSession,
    *,
    id_start: int,
    row_span: int = DEFAULT_GENERATION_ROW_SPAN,
    activate: bool = False,
) -> GenerationRecord:
    """Create an empty RANGE partition and durable metadata row."""

    if not await is_market_events_partitioned(connection):
        raise PartitionLifecycleError("market_events is not partitioned")
    id_start, id_end = aligned_generation_bounds(id_start, row_span=row_span)
    name = partition_name_for_start(id_start)
    key = generation_key_for_range(id_start, id_end)
    existing = await find_generation_for_id(connection, id_start)
    if existing is not None:
        raise PartitionLifecycleError(f"id {id_start} already covered by {existing.generation_key}")
    # Partition bounds must be literals; asyncpg rejects bound parameters in DDL.
    await connection.execute(
        text(
            f"""
            CREATE TABLE {name}
            PARTITION OF {PARENT_TABLE}
            FOR VALUES FROM ({id_start}) TO ({id_end})
            """
        )
    )
    state = GenerationState.ACTIVE if activate else GenerationState.PROVISIONED
    if activate:
        current = await get_active_generation(connection)
        if current is not None:
            raise PartitionLifecycleError("cannot activate while another generation is ACTIVE")
    await connection.execute(
        text(
            f"""
            INSERT INTO {GENERATIONS_TABLE} (
                generation_key, partition_name, id_start, id_end, state, row_span
            ) VALUES (
                :generation_key, :partition_name, :id_start, :id_end, :state, :row_span
            )
            """
        ),
        {
            "generation_key": key,
            "partition_name": name,
            "id_start": id_start,
            "id_end": id_end,
            "state": state.value,
            "row_span": row_span,
        },
    )
    created = await find_generation_for_id(connection, id_start)
    if created is None:
        raise PartitionLifecycleError("provisioned generation was not recorded")
    return created


async def provision_next_if_needed(
    connection: AsyncConnection | AsyncSession,
    *,
    row_span: int = DEFAULT_GENERATION_ROW_SPAN,
    remaining_threshold: int = PRE_BOUNDARY_PROVISION_ROWS,
) -> GenerationRecord | None:
    """Create the next generation when the ACTIVE range is nearly exhausted."""

    active = await get_active_generation(connection)
    if active is None:
        raise PartitionLifecycleError("no ACTIVE generation")
    cursor = await read_sequence_cursor(connection)
    remaining = active.id_end - cursor.next_id
    if remaining > remaining_threshold:
        return None
    successor = await find_generation_for_id(connection, active.id_end)
    if successor is not None:
        return successor
    return await provision_generation(
        connection,
        id_start=active.id_end,
        row_span=row_span,
        activate=False,
    )


async def mark_drop_eligible(
    connection: AsyncConnection | AsyncSession,
    generation_key: str,
    evidence: GenerationArchiveEvidence,
) -> GenerationRecord:
    generations = {g.generation_key: g for g in await list_generations(connection)}
    generation = generations.get(generation_key)
    if generation is None:
        raise PartitionLifecycleError(f"unknown generation {generation_key}")
    # Only storage-verified generations may become DROP_ELIGIBLE.
    if generation.state != GenerationState.VERIFIED:
        raise PartitionLifecycleError(
            f"generation {generation_key} in state {generation.state} "
            "cannot become DROP_ELIGIBLE (VERIFIED required)"
        )
    validate_archive_evidence_for_drop(generation, evidence)
    await mark_generation_state(
        connection,
        generation_key,
        GenerationState.DROP_ELIGIBLE,
        archive_evidence_sha256=evidence.evidence_sha256,
    )
    updated = {g.generation_key: g for g in await list_generations(connection)}[generation_key]
    return updated


async def assert_not_droppable_unless_eligible(
    connection: AsyncConnection | AsyncSession,
    generation_key: str,
) -> GenerationRecord:
    generation = next(
        (g for g in await list_generations(connection) if g.generation_key == generation_key),
        None,
    )
    if generation is None:
        raise PartitionLifecycleError(f"unknown generation {generation_key}")
    if generation.state != GenerationState.DROP_ELIGIBLE:
        raise PartitionLifecycleError(
            f"refusing DROP for {generation_key}: state is {generation.state}, "
            f"required {GenerationState.DROP_ELIGIBLE}"
        )
    return generation


async def drop_eligible_generation(
    connection: AsyncConnection | AsyncSession,
    generation_key: str,
    *,
    confirmation_token: str,
    operator_approved: bool,
) -> RelationSizeSample:
    """Physically DROP a DROP_ELIGIBLE partition after explicit operator approval.

    Requires a privileged session (table owner / maintenance). Runtime research
    credentials must not reach this path.
    """

    if confirmation_token != DROP_GENERATION_CONFIRMATION_TOKEN:
        raise PartitionLifecycleError("invalid DROP confirmation token")
    if not operator_approved:
        raise PartitionLifecycleError("operator approval required for generation DROP")
    generation = await assert_not_droppable_unless_eligible(connection, generation_key)
    before = await measure_relation_size(connection, generation.partition_name)
    # DROP TABLE on a partition removes heap/index files and detaches from parent.
    await connection.execute(text(f"DROP TABLE {generation.partition_name}"))
    await mark_generation_state(
        connection,
        generation_key,
        GenerationState.DROPPED,
    )
    # Post-drop the relation should be gone.
    exists = await connection.scalar(
        text("SELECT to_regclass(:rel)"),
        {"rel": f"public.{generation.partition_name}"},
    )
    if exists is not None:
        raise PartitionLifecycleError("partition relation still present after DROP")
    return before


def estimate_rows_for_target_mib(
    *,
    bytes_per_row: float,
    target_mib: float,
) -> int:
    if bytes_per_row <= 0 or target_mib <= 0:
        raise ValueError("bytes_per_row and target_mib must be positive")
    return max(1, int((target_mib * 1024 * 1024) / bytes_per_row))
