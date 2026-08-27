"""Unit tests for RAW generation lifecycle and archive DROP gate."""

import pytest

from trading_bot.storage.partition_gate import build_generation_archive_evidence
from trading_bot.storage.partitions import (
    DEFAULT_GENERATION_ROW_SPAN,
    GenerationArchiveEvidence,
    GenerationRecord,
    GenerationState,
    SequenceCursor,
    aligned_generation_bounds,
    estimate_rows_for_target_mib,
    generation_key_for_range,
    partition_name_for_start,
    validate_archive_evidence_for_drop,
)


def test_sequence_cursor_next_id_after_called() -> None:
    # Production: last_value=7471912, is_called=true → next=7471913.
    cursor = SequenceCursor(last_value=7_471_912, is_called=True)
    assert cursor.next_id == 7_471_913


def test_sequence_cursor_next_id_before_called() -> None:
    cursor = SequenceCursor(last_value=1, is_called=False)
    assert cursor.next_id == 1


def test_aligned_generation_bounds_and_names() -> None:
    start, end = aligned_generation_bounds(7_471_913, row_span=DEFAULT_GENERATION_ROW_SPAN)
    assert start == 7_471_913
    assert end == 7_471_913 + DEFAULT_GENERATION_ROW_SPAN
    assert partition_name_for_start(start) == "market_events_g_7471913"
    assert generation_key_for_range(start, end) == f"g_{start}_{end}"


def test_estimate_rows_for_250_mib_target() -> None:
    # Production calibration ≈ 533 B/row → ~490k rows for 250 MiB.
    rows = estimate_rows_for_target_mib(bytes_per_row=533.0, target_mib=250.0)
    assert 450_000 <= rows <= 520_000


def _generation(*, state: GenerationState = GenerationState.VERIFIED) -> GenerationRecord:
    return GenerationRecord(
        generation_key="g_100_500",
        partition_name="market_events_g_100",
        id_start=100,
        id_end=500,
        state=state,
        row_span=400,
        physical_bytes_at_close=None,
        archive_evidence_sha256=None,
        closed_at=None,
        verified_at=None,
        drop_eligible_at=None,
        dropped_at=None,
    )


def test_archive_gate_accepts_exact_closed_range() -> None:
    generation = _generation()
    evidence = build_generation_archive_evidence(
        generation=generation,
        min_raw_event_id=100,
        max_raw_event_id=499,
        observed_row_count=400,
        checksums_pass=True,
        manifest_pass=True,
        remote_completed=True,
        download_verification_pass=True,
        storage_reconciliation_pass=True,
        id_coverage_contiguous=True,
    )
    assert evidence.expected_row_count == 400
    validate_archive_evidence_for_drop(generation, evidence)


def test_archive_gate_rejects_checksum_failure() -> None:
    generation = _generation()
    evidence = GenerationArchiveEvidence(
        generation_key=generation.generation_key,
        min_raw_event_id=100,
        max_raw_event_id=199,
        expected_row_count=100,
        observed_row_count=100,
        checksums_pass=False,
        manifest_pass=True,
        remote_completed=True,
        download_verification_pass=True,
        storage_reconciliation_pass=True,
        id_coverage_contiguous=True,
        evidence_sha256="x",
    )
    with pytest.raises(Exception, match="checksums"):
        validate_archive_evidence_for_drop(generation, evidence)


def test_archive_gate_rejects_row_count_mismatch() -> None:
    generation = _generation()
    evidence = GenerationArchiveEvidence(
        generation_key=generation.generation_key,
        min_raw_event_id=100,
        max_raw_event_id=199,
        expected_row_count=100,
        observed_row_count=99,
        checksums_pass=True,
        manifest_pass=True,
        remote_completed=True,
        download_verification_pass=True,
        storage_reconciliation_pass=True,
        id_coverage_contiguous=True,
        evidence_sha256="x",
    )
    with pytest.raises(Exception, match="observed_row_count"):
        validate_archive_evidence_for_drop(generation, evidence)


def test_default_generation_span_targets_200_300_mib_band() -> None:
    # 400k * 533 B ≈ 203 MiB — inside the design target band.
    mib = (DEFAULT_GENERATION_ROW_SPAN * 533) / (1024 * 1024)
    assert 200 <= mib <= 300
