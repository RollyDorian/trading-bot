"""Closed-generation archive workflow (storage integrity, not research quality).

Archives one CLOSED generation at a time while the collector may write to the
next ACTIVE generation. Queries are always bounded to generation id bounds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from trading_bot.storage.partition_gate import build_generation_archive_evidence
from trading_bot.storage.partitions import (
    PARENT_TABLE,
    GenerationArchiveEvidence,
    GenerationRecord,
    GenerationState,
    PartitionLifecycleError,
    list_generations,
    mark_drop_eligible,
    mark_generation_state,
    validate_archive_evidence_for_drop,
)


@dataclass(frozen=True, slots=True)
class ClosedGenerationStats:
    """Bounded identity stats for one closed generation."""

    generation_key: str
    id_start: int
    id_end_exclusive: int
    min_raw_event_id: int | None
    max_raw_event_id: int | None
    row_count: int
    topic_counts: dict[str, int]
    trade_count: int
    schema_versions: dict[int, int]


class GenerationArchiveBackend(Protocol):
    """Pluggable store verify contract; production uses existing B2 primitives."""

    def archive_and_verify(
        self,
        generation: GenerationRecord,
        stats: ClosedGenerationStats,
    ) -> GenerationArchiveEvidence:
        """Upload + verify; raise on transport/verify failure."""


@dataclass(frozen=True, slots=True)
class MockGenerationArchiveBackend:
    """Local/disposable proof backend (no network)."""

    fail_upload: bool = False
    fail_checksum: bool = False
    fail_completed: bool = False
    fail_download_verify: bool = False
    fail_reconcile: bool = False

    def archive_and_verify(
        self,
        generation: GenerationRecord,
        stats: ClosedGenerationStats,
    ) -> GenerationArchiveEvidence:
        if self.fail_upload:
            raise PartitionLifecycleError("mock B2 unavailable / upload failed")
        if stats.row_count < 1 or stats.min_raw_event_id is None or stats.max_raw_event_id is None:
            raise PartitionLifecycleError("closed generation has no rows to archive")
        # Success path uses the real gate builder. Failure flags return evidence
        # that archive_closed_generation classifies as VERIFY_FAILED without
        # treating transport as failed.
        if (
            self.fail_checksum
            or self.fail_completed
            or self.fail_download_verify
            or self.fail_reconcile
        ):
            expected = stats.max_raw_event_id - stats.min_raw_event_id + 1
            return GenerationArchiveEvidence(
                generation_key=generation.generation_key,
                min_raw_event_id=stats.min_raw_event_id,
                max_raw_event_id=stats.max_raw_event_id,
                expected_row_count=expected,
                observed_row_count=stats.row_count,
                checksums_pass=not self.fail_checksum,
                manifest_pass=True,
                remote_completed=not self.fail_completed,
                download_verification_pass=not self.fail_download_verify,
                storage_reconciliation_pass=not self.fail_reconcile,
                id_coverage_contiguous=True,
                evidence_sha256="mock-verify-failed",
            )
        return build_generation_archive_evidence(
            generation=generation,
            min_raw_event_id=stats.min_raw_event_id,
            max_raw_event_id=stats.max_raw_event_id,
            observed_row_count=stats.row_count,
            checksums_pass=True,
            manifest_pass=True,
            remote_completed=True,
            download_verification_pass=True,
            storage_reconciliation_pass=True,
            id_coverage_contiguous=True,
            extra={
                "topic_counts": stats.topic_counts,
                "trade_count": stats.trade_count,
                "schema_versions": {str(k): v for k, v in stats.schema_versions.items()},
                "backend": "mock",
            },
        )


async def load_closed_generation_stats(
    connection: AsyncConnection | AsyncSession,
    generation: GenerationRecord,
) -> ClosedGenerationStats:
    """Derive archive source bounds directly from generation metadata."""

    if generation.state not in {
        GenerationState.CLOSED_UNARCHIVED,
        GenerationState.ARCHIVING,
        GenerationState.ARCHIVE_FAILED,
        GenerationState.VERIFY_FAILED,
        GenerationState.VERIFIED,
        GenerationState.DROP_ELIGIBLE,
    }:
        raise PartitionLifecycleError(
            f"generation {generation.generation_key} state {generation.state} "
            "is not an archive source"
        )
    bounds = (
        await connection.execute(
            text(
                f"""
                SELECT
                    MIN(id) AS min_id,
                    MAX(id) AS max_id,
                    COUNT(*)::bigint AS row_count
                FROM {PARENT_TABLE}
                WHERE id >= :id_start AND id < :id_end
                """
            ),
            {"id_start": generation.id_start, "id_end": generation.id_end},
        )
    ).one()
    topic_rows = (
        await connection.execute(
            text(
                f"""
                SELECT event_type, COUNT(*)::bigint AS n
                FROM {PARENT_TABLE}
                WHERE id >= :id_start AND id < :id_end
                GROUP BY event_type
                ORDER BY event_type
                """
            ),
            {"id_start": generation.id_start, "id_end": generation.id_end},
        )
    ).mappings().all()
    schema_rows = (
        await connection.execute(
            text(
                f"""
                SELECT schema_version, COUNT(*)::bigint AS n
                FROM {PARENT_TABLE}
                WHERE id >= :id_start AND id < :id_end
                GROUP BY schema_version
                ORDER BY schema_version
                """
            ),
            {"id_start": generation.id_start, "id_end": generation.id_end},
        )
    ).mappings().all()
    topic_counts = {str(r["event_type"]): int(r["n"]) for r in topic_rows}
    return ClosedGenerationStats(
        generation_key=generation.generation_key,
        id_start=generation.id_start,
        id_end_exclusive=generation.id_end,
        min_raw_event_id=(
            int(bounds.min_id) if bounds.min_id is not None else None
        ),
        max_raw_event_id=(
            int(bounds.max_id) if bounds.max_id is not None else None
        ),
        row_count=int(bounds.row_count or 0),
        topic_counts=topic_counts,
        trade_count=int(topic_counts.get("trade", 0)),
        schema_versions={int(r["schema_version"]): int(r["n"]) for r in schema_rows},
    )


async def select_next_archive_candidate(
    connection: AsyncConnection | AsyncSession,
) -> GenerationRecord | None:
    """One generation at a time; oldest CLOSED_UNARCHIVED / retryable failure."""

    preferred = (
        GenerationState.CLOSED_UNARCHIVED,
        GenerationState.ARCHIVE_FAILED,
        GenerationState.VERIFY_FAILED,
    )
    generations = await list_generations(connection)
    for state in preferred:
        candidates = [g for g in generations if g.state == state]
        if candidates:
            return min(candidates, key=lambda g: g.id_start)
    return None


async def archive_closed_generation(
    connection: AsyncConnection | AsyncSession,
    generation_key: str,
    backend: GenerationArchiveBackend,
    *,
    mark_drop_eligible_on_success: bool = True,
) -> GenerationArchiveEvidence:
    """Archive + verify one closed generation; durable state on every outcome.

    Research quality PASS is intentionally not required.
    """

    generations = {g.generation_key: g for g in await list_generations(connection)}
    generation = generations.get(generation_key)
    if generation is None:
        raise PartitionLifecycleError(f"unknown generation {generation_key}")
    if generation.state == GenerationState.ACTIVE:
        raise PartitionLifecycleError("refusing to archive ACTIVE generation")
    if generation.state not in {
        GenerationState.CLOSED_UNARCHIVED,
        GenerationState.ARCHIVE_FAILED,
        GenerationState.VERIFY_FAILED,
    }:
        raise PartitionLifecycleError(
            f"generation {generation_key} state {generation.state} cannot start archive"
        )

    stats = await load_closed_generation_stats(connection, generation)
    await mark_generation_state(
        connection,
        generation_key,
        GenerationState.ARCHIVING,
    )
    try:
        evidence = backend.archive_and_verify(generation, stats)
    except Exception:
        await mark_generation_state(
            connection,
            generation_key,
            GenerationState.ARCHIVE_FAILED,
        )
        raise

    closed = {g.generation_key: g for g in await list_generations(connection)}[
        generation_key
    ]
    try:
        validate_archive_evidence_for_drop(closed, evidence)
        await mark_generation_state(
            connection,
            generation_key,
            GenerationState.VERIFIED,
            archive_evidence_sha256=evidence.evidence_sha256,
        )
    except PartitionLifecycleError:
        await mark_generation_state(
            connection,
            generation_key,
            GenerationState.VERIFY_FAILED,
        )
        raise

    if mark_drop_eligible_on_success:
        await mark_drop_eligible(connection, generation_key, evidence)
    return evidence
