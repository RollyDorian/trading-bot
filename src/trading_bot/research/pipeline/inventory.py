"""Research inventory helpers (metadata-only; no bulk Parquet download)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class DatasetInventoryEntry:
    dataset_id: str
    kind: str
    rows: int | None
    trade_rows: int | None
    id_start: int | None
    id_end_inclusive: int | None
    storage_ok: bool
    research_quality_status: str | None
    extra: dict[str, Any]


def load_inventory(path: Path) -> list[DatasetInventoryEntry]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries: list[DatasetInventoryEntry] = []
    for raw in payload.get("datasets", []):
        storage = raw.get("storage_verification") or {}
        storage_ok = bool(
            storage.get("remote_completed")
            or storage.get("restore_verification") == "PASS"
            or storage.get("windows_completed")
        )
        topics = raw.get("topics")
        trade_rows = raw.get("trade_rows")
        if trade_rows is None and isinstance(topics, dict):
            trade_rows = int(topics.get("trades", 0) or topics.get("trade", 0) or 0)
        entries.append(
            DatasetInventoryEntry(
                dataset_id=str(raw["dataset_id"]),
                kind=str(raw.get("kind", "unknown")),
                rows=raw.get("rows"),
                trade_rows=trade_rows,
                id_start=raw.get("id_start"),
                id_end_inclusive=raw.get("id_end_inclusive"),
                storage_ok=storage_ok,
                research_quality_status=raw.get("research_quality_status"),
                extra=raw,
            )
        )
    return entries


def verified_historical_sources(path: Path) -> list[DatasetInventoryEntry]:
    """Return inventory rows that are durable verified B2 history (not hot buffer)."""

    return [e for e in load_inventory(path) if e.storage_ok]
