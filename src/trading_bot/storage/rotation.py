"""Collector-facing generation rotation and crash-safe metadata sync.

Collector keeps inserting into logical parent ``market_events``. PostgreSQL
routes by id range. This module only maintains durable generation metadata and
fail-closed writable cover checks.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from trading_bot.storage.partitions import (
    DEFAULT_GENERATION_ROW_SPAN,
    GENERATIONS_TABLE,
    PARENT_TABLE,
    PRE_BOUNDARY_PROVISION_ROWS,
    GenerationRecord,
    GenerationState,
    PartitionLifecycleError,
    close_active_generation,
    ensure_writable_cover,
    find_generation_for_id,
    get_active_generation,
    list_generations,
    mark_generation_state,
    measure_relation_size,
    provision_generation,
    provision_next_if_needed,
    read_sequence_cursor,
)


@dataclass(frozen=True, slots=True)
class RotationAction:
    """What maintain_writable_generations did in one pass."""

    provisioned_successor: bool
    rotated_active: bool
    active: GenerationRecord | None
    successor: GenerationRecord | None


async def catalog_partition_names(
    connection: AsyncConnection | AsyncSession,
) -> set[str]:
    """Physical child partitions attached to ``market_events``."""

    rows = (
        await connection.execute(
            text(
                """
                SELECT child.relname AS name
                FROM pg_class parent
                JOIN pg_namespace n ON n.oid = parent.relnamespace
                JOIN pg_inherits i ON i.inhparent = parent.oid
                JOIN pg_class child ON child.oid = i.inhrelid
                WHERE n.nspname = 'public' AND parent.relname = :parent
                """
            ),
            {"parent": PARENT_TABLE},
        )
    ).mappings().all()
    return {str(row["name"]) for row in rows}


async def assert_catalog_metadata_consistent(
    connection: AsyncConnection | AsyncSession,
) -> None:
    """Fail closed on unexpected catalog/metadata drift (no guessing)."""

    generations = await list_generations(connection)
    catalog = await catalog_partition_names(connection)
    live = [g for g in generations if g.state != GenerationState.DROPPED]
    live_names = {g.partition_name for g in live}
    missing = live_names - catalog
    if missing:
        raise PartitionLifecycleError(
            f"generation metadata references missing partitions: {sorted(missing)}"
        )
    unexpected = catalog - live_names
    if unexpected:
        raise PartitionLifecycleError(
            f"unexpected partitions in catalog without metadata: {sorted(unexpected)}"
        )
    # Ranges must not overlap among non-dropped generations.
    ordered = sorted(live, key=lambda g: g.id_start)
    for prev, cur in zip(ordered, ordered[1:], strict=False):
        if cur.id_start < prev.id_end:
            raise PartitionLifecycleError(
                f"overlapping generations {prev.generation_key} and {cur.generation_key}"
            )
    actives = [g for g in live if g.state == GenerationState.ACTIVE]
    if len(actives) > 1:
        raise PartitionLifecycleError("multiple ACTIVE generations are not allowed")


async def rotate_active_if_sequence_crossed(
    connection: AsyncConnection | AsyncSession,
    *,
    reason: str = "sequence crossed ACTIVE upper bound",
) -> GenerationRecord | None:
    """Close ACTIVE and activate successor when next id is outside ACTIVE.

    PostgreSQL already routes inserts by range; this keeps metadata aligned so
    operators never see the wrong ACTIVE generation.
    """

    active = await get_active_generation(connection)
    if active is None:
        return None
    cursor = await read_sequence_cursor(connection)
    if cursor.next_id < active.id_end:
        return None
    successor = await find_generation_for_id(connection, active.id_end)
    if successor is None:
        raise PartitionLifecycleError(
            f"ACTIVE {active.generation_key} exhausted at id {active.id_end} "
            "with no provisioned successor; refuse rotation"
        )
    if successor.state not in {
        GenerationState.PROVISIONED,
        GenerationState.ACTIVE,
    }:
        raise PartitionLifecycleError(
            f"successor {successor.generation_key} state {successor.state} is not writable"
        )
    closed = await close_active_generation(connection, reason=reason)
    return closed


async def maintain_writable_generations(
    connection: AsyncConnection | AsyncSession,
    *,
    row_span: int = DEFAULT_GENERATION_ROW_SPAN,
    remaining_threshold: int = PRE_BOUNDARY_PROVISION_ROWS,
) -> RotationAction:
    """Idempotent pre-insert maintenance: provision lead + metadata rotation.

    Call before collector batches (and from operator tooling). Missing successor
    at/near boundary fails closed via ``ensure_writable_cover``.
    """

    await assert_catalog_metadata_consistent(connection)
    before_keys = {g.generation_key for g in await list_generations(connection)}
    provisioned = await provision_next_if_needed(
        connection,
        row_span=row_span,
        remaining_threshold=remaining_threshold,
    )
    newly_provisioned = (
        provisioned is not None and provisioned.generation_key not in before_keys
    )
    rotated = await rotate_active_if_sequence_crossed(connection)
    await ensure_writable_cover(connection)
    active = await get_active_generation(connection)
    successor: GenerationRecord | None = None
    if active is not None:
        candidate = await find_generation_for_id(connection, active.id_end)
        if candidate is not None and candidate.generation_key != active.generation_key:
            successor = candidate
    return RotationAction(
        provisioned_successor=newly_provisioned,
        rotated_active=rotated is not None,
        active=active,
        successor=successor,
    )


async def recover_active_from_sequence(
    connection: AsyncConnection | AsyncSession,
) -> GenerationRecord:
    """After crash/restart: align ACTIVE with sequence using catalog truth.

    Does not invent partitions. If metadata claims wrong ACTIVE while sequence
    already belongs to a PROVISIONED successor, rotate. If no cover exists,
    fail closed.
    """

    await assert_catalog_metadata_consistent(connection)
    cursor = await read_sequence_cursor(connection)
    covering = await find_generation_for_id(connection, cursor.next_id)
    if covering is None:
        raise PartitionLifecycleError(
            f"no generation covers next id {cursor.next_id}; refuse recovery"
        )
    active = await get_active_generation(connection)
    if active is None:
        if covering.state == GenerationState.PROVISIONED:
            await mark_generation_state(
                connection,
                covering.generation_key,
                GenerationState.ACTIVE,
            )
        elif covering.state != GenerationState.ACTIVE:
            raise PartitionLifecycleError(
                f"covering generation {covering.generation_key} state "
                f"{covering.state} cannot become ACTIVE"
            )
        refreshed = await get_active_generation(connection)
        if refreshed is None:
            raise PartitionLifecycleError("failed to recover ACTIVE generation")
        return refreshed
    if covering.generation_key == active.generation_key:
        return active
    # Sequence already moved into successor — close stale ACTIVE.
    if covering.id_start == active.id_end and covering.state == GenerationState.PROVISIONED:
        await close_active_generation(
            connection,
            reason="crash recovery: sequence already in successor range",
        )
        refreshed = await get_active_generation(connection)
        if refreshed is None or refreshed.generation_key != covering.generation_key:
            raise PartitionLifecycleError("crash recovery failed to activate successor")
        return refreshed
    raise PartitionLifecycleError(
        f"ACTIVE {active.generation_key} disagrees with covering "
        f"{covering.generation_key} for next id {cursor.next_id}"
    )


async def post_drop_verification(
    connection: AsyncConnection | AsyncSession,
    *,
    dropped_generation_key: str,
    expected_active_key: str | None,
    sequence_next_id_before: int,
) -> dict[str, object]:
    """Verify catalog/metadata/sequence after an operator DROP."""

    await assert_catalog_metadata_consistent(connection)
    generations = {g.generation_key: g for g in await list_generations(connection)}
    dropped = generations.get(dropped_generation_key)
    if dropped is None or dropped.state != GenerationState.DROPPED:
        raise PartitionLifecycleError("dropped generation metadata is not DROPPED")
    exists = await connection.scalar(
        text("SELECT to_regclass(:rel)"),
        {"rel": f"public.{dropped.partition_name}"},
    )
    if exists is not None:
        raise PartitionLifecycleError("physical child still present after DROP")
    parent_ok = await connection.scalar(
        text("SELECT to_regclass(:rel)"),
        {"rel": f"public.{PARENT_TABLE}"},
    )
    if parent_ok is None:
        raise PartitionLifecycleError("logical parent market_events missing after DROP")
    active = await get_active_generation(connection)
    if expected_active_key is not None and (
        active is None or active.generation_key != expected_active_key
    ):
        raise PartitionLifecycleError("ACTIVE generation changed unexpectedly after DROP")
    cursor = await read_sequence_cursor(connection)
    if cursor.next_id != sequence_next_id_before:
        raise PartitionLifecycleError("sequence cursor changed during DROP")
    # Evidence hash must remain for audit after DROP.
    if not dropped.archive_evidence_sha256:
        raise PartitionLifecycleError("DROPPED generation lost archive evidence hash")
    parent_size = await measure_relation_size(connection, PARENT_TABLE)
    return {
        "dropped_generation_key": dropped_generation_key,
        "partition_absent": True,
        "active_generation_key": active.generation_key if active else None,
        "sequence_next_id": cursor.next_id,
        "parent_total_bytes": parent_size.total_bytes,
        "generations_table": GENERATIONS_TABLE,
    }


# Convenience re-export for callers that only import rotation.
__all__ = [
    "RotationAction",
    "assert_catalog_metadata_consistent",
    "catalog_partition_names",
    "maintain_writable_generations",
    "post_drop_verification",
    "provision_generation",
    "recover_active_from_sequence",
    "rotate_active_if_sequence_crossed",
]
