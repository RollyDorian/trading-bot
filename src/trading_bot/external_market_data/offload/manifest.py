"""Segment manifest for external RAW offload."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trading_bot.external_market_data.offload.segments import (
    SEGMENT_SCHEMA_VERSION,
    SegmentPaths,
    summarize_ndjson,
)


def build_manifest(
    paths: SegmentPaths,
    *,
    archived_bytes: int | None = None,
    archive_format: str = "ndjson.gz",
    content_sha256: str | None = None,
    code_version: str | None = None,
) -> dict[str, Any]:
    summary = summarize_ndjson(paths.sealed_ndjson)
    return {
        "manifest_version": 1,
        "segment_id": paths.segment_id,
        "venue": "binance_usdm",
        "instrument": "ETHUSDT",
        "streams": sorted(summary["by_type"].keys()),
        "schema_version": SEGMENT_SCHEMA_VERSION,
        "utc_bounds": {
            "received_at_min": summary["received_at_min"],
            "received_at_max": summary["received_at_max"],
            "exchange_at_min": summary["exchange_at_min"],
            "exchange_at_max": summary["exchange_at_max"],
        },
        "first_local_sequence": summary["first_local_sequence"],
        "last_local_sequence": summary["last_local_sequence"],
        "connection_ids": summary["connection_ids"],
        "event_counts": {
            "total": summary["event_count"],
            "by_type": summary["by_type"],
        },
        "raw_bytes": summary["raw_bytes"],
        "archived_bytes": archived_bytes,
        "archive_format": archive_format,
        "content_sha256": content_sha256 or summary["content_sha256"],
        "code_version": code_version,
        "note": (
            "bookTicker and aggTrade do not share one exchange sequence; "
            "local_sequence is per connection."
        ),
    }


def write_manifest(paths: SegmentPaths, manifest: dict[str, Any]) -> None:
    tmp = paths.manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(paths.manifest_path)


def read_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest must be a JSON object")
    return payload
