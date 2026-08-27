"""Incremental research corpus discovery from durable B2 archive evidence.

Never scans production PostgreSQL hot buffers. Discovery is metadata-first and
idempotent: already-registered generations are skipped.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CorpusSegment:
    segment_id: str
    kind: str  # prior_continuous | partition_generation | archive_window
    role: str  # exploratory | oos_contaminated | oos_clean_future
    id_start: int | None
    id_end_inclusive: int | None
    source_evidence: dict[str, Any]


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "registry_version": 1,
            "segments": [],
            "updated_at_utc": None,
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"registry must be an object: {path}")
    return payload


def save_registry(path: Path, registry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    registry = dict(registry)
    registry["updated_at_utc"] = datetime.now(UTC).isoformat()
    path.write_text(
        json.dumps(registry, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )


def registered_segment_ids(registry: dict[str, Any]) -> set[str]:
    return {str(item["segment_id"]) for item in registry.get("segments", [])}


def discover_new_completed_windows(
    b2_completed_index: list[dict[str, Any]],
    *,
    known_dataset_ids: set[str],
) -> list[dict[str, Any]]:
    """Return COMPLETED archive windows not yet known to research."""

    discovered: list[dict[str, Any]] = []
    for item in b2_completed_index:
        dataset_id = str(item["dataset_id"])
        if dataset_id in known_dataset_ids:
            continue
        discovered.append(dict(item))
    return sorted(discovered, key=lambda row: str(row["dataset_id"]))


def bootstrap_registry_from_validated_inventory(
    inventory_path: Path,
    *,
    registry_path: Path,
) -> dict[str, Any]:
    """Seed registry from the validated full-corpus inventory (no B2 mutation)."""

    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    segments: list[dict[str, Any]] = []
    for raw in inventory.get("datasets", []):
        dataset_id = str(raw["dataset_id"])
        kind = str(raw.get("kind", "unknown"))
        # Generation inspected in full-corpus validation is contaminated for fitting.
        role = "oos_contaminated" if kind == "partition_generation" else "exploratory"
        segments.append(
            asdict(
                CorpusSegment(
                    segment_id=dataset_id,
                    kind=kind,
                    role=role,
                    id_start=raw.get("id_start"),
                    id_end_inclusive=raw.get("id_end_inclusive"),
                    source_evidence={
                        "inventory": True,
                        "storage_verification": raw.get("storage_verification"),
                        "rows": raw.get("rows"),
                        "trade_rows": raw.get("trade_rows"),
                    },
                )
            )
        )
    registry = {
        "registry_version": 1,
        "segments": segments,
        "notes": {
            "oos_policy": (
                "g_7471913_7871913 was inspected during full-corpus validation "
                "baselines/IC reporting; do not use it for threshold fitting. "
                "Reserve the next newly verified generation/time block as clean OOS."
            )
        },
    }
    save_registry(registry_path, registry)
    return registry


def register_segments(
    registry: dict[str, Any],
    segments: list[CorpusSegment],
) -> tuple[dict[str, Any], list[str]]:
    """Append unknown segments; skip duplicates. Returns (registry, newly_added_ids)."""

    known = registered_segment_ids(registry)
    added: list[str] = []
    items = list(registry.get("segments", []))
    for segment in segments:
        if segment.segment_id in known:
            continue
        items.append(asdict(segment))
        known.add(segment.segment_id)
        added.append(segment.segment_id)
    registry = dict(registry)
    registry["segments"] = items
    return registry, added


def plan_incremental_materialization(
    *,
    registry: dict[str, Any],
    b2_completed_index: list[dict[str, Any]],
    already_materialized_dataset_ids: set[str],
) -> dict[str, Any]:
    """Idempotent plan: which COMPLETED windows are new vs already known."""

    known = registered_segment_ids(registry) | already_materialized_dataset_ids
    new_windows = discover_new_completed_windows(
        b2_completed_index, known_dataset_ids=known
    )
    return {
        "new_windows": [row["dataset_id"] for row in new_windows],
        "new_window_count": len(new_windows),
        "already_known_or_materialized": len(known),
        "action": "MATERIALIZE_INCREMENTAL" if new_windows else "NO_NEW_ARCHIVES",
        "note": "Materialize only new COMPLETED windows; do not recompute prior corpus.",
    }


def reserve_clean_oos_future(
    registry: dict[str, Any],
    *,
    segment_id: str,
    kind: str = "partition_generation",
    id_start: int | None = None,
    id_end_inclusive: int | None = None,
    source_evidence: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Designate a future verified segment as untouched clean OOS.

    Does not inspect payloads. Contaminated segments cannot be re-labeled clean.
    Returns (registry, newly_reserved).
    """

    items = list(registry.get("segments", []))
    for item in items:
        if str(item.get("segment_id")) != segment_id:
            continue
        role = str(item.get("role", ""))
        if role == "oos_contaminated":
            raise ValueError(
                f"refusing to reserve contaminated segment as clean OOS: {segment_id}"
            )
        if role == "oos_clean_future":
            return registry, False
        item["role"] = "oos_clean_future"
        item["source_evidence"] = {
            **dict(item.get("source_evidence") or {}),
            **(source_evidence or {}),
            "reserved_clean_oos": True,
            "inspected_during_selection": False,
        }
        out = dict(registry)
        out["segments"] = items
        return out, True

    segment = CorpusSegment(
        segment_id=segment_id,
        kind=kind,
        role="oos_clean_future",
        id_start=id_start,
        id_end_inclusive=id_end_inclusive,
        source_evidence={
            **(source_evidence or {}),
            "reserved_clean_oos": True,
            "inspected_during_selection": False,
        },
    )
    out, added = register_segments(registry, [segment])
    return out, bool(added)
