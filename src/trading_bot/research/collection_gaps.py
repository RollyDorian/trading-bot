"""Durable production collection-gap registry for research quality masks.

These intervals are unrecoverable: no RAW rows exist and none may be
synthesized. Normalization and 1s market_state builders must not interpolate
or carry stale tops across them.

The 251-id partition-incident hole is allocated sequence values with no
persisted ``market_events`` rows. Treat it as
``ALLOCATED_NOT_PERSISTED_DURING_PARTITION_FAILURE``, never as deleted market
data and never as a sequence reset candidate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

GapKind = Literal[
    "ALLOCATED_NOT_PERSISTED_DURING_PARTITION_FAILURE",
    "COLLECTION_OUTAGE",
]

INCIDENT_GENERATION_KEY = "g_9071913_9471913"
INCIDENT_PERSISTED_ROW_COUNT = 399_749
INCIDENT_MIN_PERSISTED_ID = 9_072_164
INCIDENT_MAX_PERSISTED_ID = 9_471_912
INCIDENT_ALLOCATED_NOT_PERSISTED_START = 9_071_913
INCIDENT_ALLOCATED_NOT_PERSISTED_END = 9_072_163

DEFAULT_GAPS_PATH = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "quality"
    / "hibachi_collection_gaps_v1.json"
)


def reconcile_closed_generation_persisted_ids(
    *,
    generation_key: str,
    id_start: int,
    id_end: int,
    persisted_count: int,
    min_persisted_id: int,
    max_persisted_id: int,
) -> None:
    """Fail closed unless persisted ids match generation identity.

    ``g_9071913`` is allowed to have 399,749 rows because 251 sequence values
    were allocated during the partition-miss incident and never persisted.
    """

    if persisted_count < 1:
        raise ValueError(f"{generation_key} has no persisted rows")
    if min_persisted_id < id_start or max_persisted_id >= id_end:
        raise ValueError(f"{generation_key} persisted ids outside generation bounds")
    if min_persisted_id > max_persisted_id:
        raise ValueError(f"{generation_key} persisted id bounds inverted")
    span = max_persisted_id - min_persisted_id + 1
    if persisted_count != span:
        raise ValueError(
            f"{generation_key} persisted count {persisted_count} != id span {span}"
        )
    if generation_key == INCIDENT_GENERATION_KEY:
        if persisted_count != INCIDENT_PERSISTED_ROW_COUNT:
            raise ValueError("g_9071913 persisted count must be 399749")
        if min_persisted_id != INCIDENT_MIN_PERSISTED_ID:
            raise ValueError("g_9071913 min persisted id must be 9072164")
        if max_persisted_id != INCIDENT_MAX_PERSISTED_ID:
            raise ValueError("g_9071913 max persisted id must be 9471912")
        hole = INCIDENT_ALLOCATED_NOT_PERSISTED_END - INCIDENT_ALLOCATED_NOT_PERSISTED_START + 1
        if hole != 251:
            raise ValueError("incident hole must remain 251 ids")
        return
    capacity = id_end - id_start
    if persisted_count != capacity:
        raise ValueError(
            f"{generation_key} expected {capacity} persisted rows, got {persisted_count}"
        )
    if min_persisted_id != id_start or max_persisted_id != id_end - 1:
        raise ValueError(f"{generation_key} persisted ids do not fill the generation")


@dataclass(frozen=True, slots=True)
class CollectionGap:
    """One documented production collection hole."""

    gap_id: str
    kind: GapKind
    start_utc: datetime
    # None means the outage is still open (COLLECT has not resumed).
    end_utc: datetime | None
    id_start_inclusive: int | None
    id_end_inclusive: int | None
    classification: str
    synthesize: bool
    bridge_normalization: bool
    notes: str

    def contains_timestamp(self, instant: datetime) -> bool:
        ts = instant.astimezone(UTC)
        if self.end_utc is None:
            # Ongoing outage: mask from start until COLLECT resumes and the
            # registry is closed with an exclusive end timestamp.
            return ts >= self.start_utc
        return self.start_utc <= ts < self.end_utc

    def contains_id(self, raw_id: int) -> bool:
        if self.id_start_inclusive is None or self.id_end_inclusive is None:
            return False
        return self.id_start_inclusive <= raw_id <= self.id_end_inclusive


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"collection gap timestamp must be timezone-aware: {value}")
    return parsed.astimezone(UTC)


def load_collection_gaps(path: Path | None = None) -> tuple[CollectionGap, ...]:
    """Load the committed registry. Missing file fails closed (empty is not assumed)."""

    target = path or DEFAULT_GAPS_PATH
    payload: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
    gaps: list[CollectionGap] = []
    for raw in payload.get("gaps", []):
        kind = str(raw["kind"])
        if kind not in {
            "ALLOCATED_NOT_PERSISTED_DURING_PARTITION_FAILURE",
            "COLLECTION_OUTAGE",
        }:
            raise ValueError(f"unknown collection gap kind {kind}")
        # ``end_utc`` JSON null means COLLECT has not resumed yet.
        end_raw = raw.get("end_utc")
        end_utc = None if end_raw is None else _parse_utc(str(end_raw))
        gaps.append(
            CollectionGap(
                gap_id=str(raw["gap_id"]),
                kind=kind,  # type: ignore[arg-type]
                start_utc=_parse_utc(str(raw["start_utc"])),
                end_utc=end_utc,
                id_start_inclusive=raw.get("id_start_inclusive"),
                id_end_inclusive=raw.get("id_end_inclusive"),
                classification=str(raw["classification"]),
                synthesize=bool(raw.get("synthesize", True)),
                bridge_normalization=bool(raw.get("bridge_normalization", True)),
                notes=str(raw.get("notes", "")),
            )
        )
        if gaps[-1].synthesize or gaps[-1].bridge_normalization:
            raise ValueError(
                f"collection gap {gaps[-1].gap_id} must forbid synthesize/bridge"
            )
    return tuple(gaps)


def timestamp_in_known_outage(
    instant: datetime, gaps: tuple[CollectionGap, ...] | None = None
) -> CollectionGap | None:
    for gap in gaps if gaps is not None else load_collection_gaps():
        if gap.contains_timestamp(instant):
            return gap
    return None


def raw_id_in_known_gap(
    raw_id: int, gaps: tuple[CollectionGap, ...] | None = None
) -> CollectionGap | None:
    for gap in gaps if gaps is not None else load_collection_gaps():
        if gap.contains_id(raw_id):
            return gap
    return None
