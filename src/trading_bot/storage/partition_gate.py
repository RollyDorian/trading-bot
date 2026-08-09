"""Archive identity gate for RAW generation DROP eligibility.

Research quality and paper-admission status deliberately do not appear in
this contract. Rejected/quarantined but structurally verified RAW may still
be DROP_ELIGIBLE under the storage-integrity rules.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from trading_bot.storage.partitions import (
    GenerationArchiveEvidence,
    GenerationRecord,
    validate_archive_evidence_for_drop,
)


def build_generation_archive_evidence(
    *,
    generation: GenerationRecord,
    min_raw_event_id: int,
    max_raw_event_id: int,
    observed_row_count: int,
    checksums_pass: bool,
    manifest_pass: bool,
    remote_completed: bool,
    download_verification_pass: bool,
    storage_reconciliation_pass: bool,
    id_coverage_contiguous: bool,
    extra: dict[str, Any] | None = None,
) -> GenerationArchiveEvidence:
    """Build evidence and fail closed on structural mismatches."""

    expected_row_count = max_raw_event_id - min_raw_event_id + 1
    payload = {
        "generation_key": generation.generation_key,
        "min_raw_event_id": min_raw_event_id,
        "max_raw_event_id": max_raw_event_id,
        "expected_row_count": expected_row_count,
        "observed_row_count": observed_row_count,
        "checksums_pass": checksums_pass,
        "manifest_pass": manifest_pass,
        "remote_completed": remote_completed,
        "download_verification_pass": download_verification_pass,
        "storage_reconciliation_pass": storage_reconciliation_pass,
        "id_coverage_contiguous": id_coverage_contiguous,
        "extra": extra or {},
    }
    evidence_sha256 = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    evidence = GenerationArchiveEvidence(
        generation_key=generation.generation_key,
        min_raw_event_id=min_raw_event_id,
        max_raw_event_id=max_raw_event_id,
        expected_row_count=expected_row_count,
        observed_row_count=observed_row_count,
        checksums_pass=checksums_pass,
        manifest_pass=manifest_pass,
        remote_completed=remote_completed,
        download_verification_pass=download_verification_pass,
        storage_reconciliation_pass=storage_reconciliation_pass,
        id_coverage_contiguous=id_coverage_contiguous,
        evidence_sha256=evidence_sha256,
    )
    validate_archive_evidence_for_drop(generation, evidence)
    return evidence


def evidence_to_public_dict(evidence: GenerationArchiveEvidence) -> dict[str, Any]:
    """Serialize evidence without inventing research-admission fields."""

    return asdict(evidence)
