"""Read-only operator status for external offload."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from trading_bot.external_market_data.offload.capacity import (
    BacklogAction,
    CapacityPolicy,
    measure_local_external_bytes,
)
from trading_bot.external_market_data.offload.segments import (
    SegmentPaths,
    SegmentState,
    read_state,
)


@dataclass(slots=True)
class ExternalOffloadStatus:
    EXTERNAL: str = "OFF"
    ACTIVE: dict[str, Any] | None = None
    SEALED_UNVERIFIED: dict[str, Any] = field(default_factory=dict)
    UPLOADING: dict[str, Any] = field(default_factory=dict)
    FAILED: dict[str, Any] = field(default_factory=dict)
    VERIFIED_REMOTE: dict[str, Any] = field(default_factory=dict)
    LOCAL_RECLAIMABLE: dict[str, Any] = field(default_factory=dict)
    LOCAL_TOTAL_BYTES: int = 0
    LOCAL_TOTAL_MIB: float = 0.0
    B2: str = "unknown"
    INGEST_RATE: dict[str, float] = field(default_factory=dict)
    OFFLOAD_RATE: dict[str, float] = field(default_factory=dict)
    BACKLOG_TREND: str = "unknown"
    FILESYSTEM: dict[str, Any] = field(default_factory=dict)
    ACTION: str = BacklogAction.NONE.value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def collect_status(
    root: Path,
    *,
    external_mode: str = "OFF",
    b2_health: str = "unknown",
    ingest_msg_per_sec: float | None = None,
    ingest_mib_per_hour: float | None = None,
    offload_mib_per_hour: float | None = None,
    backlog_trend: str = "unknown",
    policy: CapacityPolicy | None = None,
) -> ExternalOffloadStatus:
    policy = policy or CapacityPolicy()
    sealed_count = 0
    sealed_bytes = 0
    reclaim_count = 0
    reclaim_bytes = 0
    uploading_count = 0
    failed_count = 0
    failed_bytes = 0
    verified_count = 0
    active: dict[str, Any] | None = None
    uploading_id: str | None = None
    latest_verified: str | None = None

    if root.exists():
        for child in sorted(p for p in root.iterdir() if p.is_dir()):
            paths = SegmentPaths(root, child.name)
            record = read_state(paths)
            if record is None:
                continue
            if record.state == SegmentState.ACTIVE:
                active = {
                    "segment_id": record.segment_id,
                    "bytes": record.raw_bytes,
                    "age_hint_created_at": record.created_at_utc,
                }
            elif record.state == SegmentState.SEALED_UNVERIFIED:
                sealed_count += 1
                sealed_bytes += record.raw_bytes
            elif record.state == SegmentState.UPLOADING:
                uploading_count += 1
                uploading_id = record.segment_id
            elif record.state == SegmentState.FAILED:
                failed_count += 1
                failed_bytes += measure_dir_bytes(paths.dir)
            elif record.state == SegmentState.VERIFIED_REMOTE:
                verified_count += 1
                latest_verified = record.segment_id
            elif record.state == SegmentState.RECLAIMABLE:
                reclaim_count += 1
                reclaim_bytes += measure_dir_bytes(paths.dir)
                verified_count += 1
                latest_verified = record.segment_id

    local_total = measure_local_external_bytes(root)
    free = shutil.disk_usage(root if root.exists() else Path.cwd()).free
    action = policy.classify(
        local_total_bytes=local_total,
        filesystem_free_bytes=free,
    )
    return ExternalOffloadStatus(
        EXTERNAL=external_mode,
        ACTIVE=active,
        SEALED_UNVERIFIED={"count": sealed_count, "bytes": sealed_bytes},
        UPLOADING={"count": uploading_count, "id": uploading_id},
        FAILED={"count": failed_count, "bytes": failed_bytes},
        VERIFIED_REMOTE={"latest": latest_verified, "count": verified_count},
        LOCAL_RECLAIMABLE={"count": reclaim_count, "bytes": reclaim_bytes},
        LOCAL_TOTAL_BYTES=local_total,
        LOCAL_TOTAL_MIB=round(local_total / (1024 * 1024), 3),
        B2=b2_health,
        INGEST_RATE={
            "msg_per_sec": ingest_msg_per_sec or 0.0,
            "mib_per_hour": ingest_mib_per_hour or 0.0,
        },
        OFFLOAD_RATE={"mib_per_hour": offload_mib_per_hour or 0.0},
        BACKLOG_TREND=backlog_trend,
        FILESYSTEM={
            "free_gib": round(free / (1024**3), 3),
            "floor_gib": round(policy.global_floor_bytes / (1024**3), 3),
            "margin_gib": round((free - policy.global_floor_bytes) / (1024**3), 3),
            "floor_margin_policy_gib": round(policy.floor_margin_bytes / (1024**3), 3),
        },
        ACTION=action.value,
    )


def measure_dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def status_json(root: Path, **kwargs: Any) -> str:
    return json.dumps(collect_status(root, **kwargs).to_dict(), indent=2, sort_keys=True)
