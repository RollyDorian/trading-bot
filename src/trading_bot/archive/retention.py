import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.archive.manifest import ArchiveManifest
from trading_bot.archive.store import ArchiveStore
from trading_bot.storage.models import MarketEvent

MAX_DELETE_CHUNK = 1000


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    interval_start_utc: str
    interval_end_utc: str
    min_raw_event_id: int
    max_raw_event_id: int
    row_count: int
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    generated_at_utc: str
    hot_raw_days: int
    dry_run: bool
    eligible_rows: int
    candidates: tuple[RetentionCandidate, ...]
    state: str


def plan_retention(
    manifests: list[tuple[ArchiveManifest, str]],
    *,
    now: datetime,
    hot_raw_days: int,
) -> RetentionPlan:
    if now.tzinfo is None or hot_raw_days < 1:
        raise ValueError("retention inputs are invalid")
    cutoff = now.astimezone(UTC) - timedelta(days=hot_raw_days)
    candidates: list[RetentionCandidate] = []
    previous_end: datetime | None = None
    for manifest, digest in sorted(
        manifests,
        key=lambda item: item[0].interval_start_utc,
    ):
        start = datetime.fromisoformat(manifest.interval_start_utc)
        end = datetime.fromisoformat(manifest.interval_end_utc)
        if (
            manifest.verification_status != "verified"
            or manifest.destination != "s3"
            or end > cutoff
            or end - start != timedelta(days=1)
        ):
            continue
        if previous_end is not None and start != previous_end:
            raise RuntimeError("verified archive intervals contain a gap")
        raw_rows = sum(
            item.row_count for item in manifest.objects if item.dataset == "raw"
        )
        if raw_rows != manifest.raw_row_count:
            raise RuntimeError("verified archive coverage is inconsistent")
        candidates.append(
            RetentionCandidate(
                interval_start_utc=manifest.interval_start_utc,
                interval_end_utc=manifest.interval_end_utc,
                min_raw_event_id=manifest.min_raw_event_id,
                max_raw_event_id=manifest.max_raw_event_id,
                row_count=manifest.raw_row_count,
                manifest_sha256=digest,
            )
        )
        previous_end = end
    return RetentionPlan(
        generated_at_utc=now.astimezone(UTC).isoformat(),
        hot_raw_days=hot_raw_days,
        dry_run=True,
        eligible_rows=sum(item.row_count for item in candidates),
        candidates=tuple(candidates),
        state="eligible" if candidates else "nothing_eligible",
    )


class RetentionExecutor:
    """Bounded executor; not exposed by the production CLI in this milestone."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        audit_store: ArchiveStore,
        *,
        test_mode: bool = False,
    ) -> None:
        self._factory = session_factory
        self._store = audit_store
        self._test_mode = test_mode

    async def delete_verified_chunk(
        self,
        candidate: RetentionCandidate,
        *,
        limit: int,
        confirmation: str,
    ) -> int:
        if confirmation != "DELETE_VERIFIED_ARCHIVE":
            raise PermissionError("retention confirmation is invalid")
        if not 1 <= limit <= MAX_DELETE_CHUNK:
            raise ValueError(f"retention chunk must be between 1 and {MAX_DELETE_CHUNK}")
        if self._store.destination_label != "s3" and not self._test_mode:
            raise RuntimeError("production retention requires verified external storage")
        audit_id = str(uuid.uuid4())
        key = f"_retention/{audit_id}.json"
        started = {
            "audit_id": audit_id,
            "status": "started",
            "candidate": asdict(candidate),
            "limit": limit,
            "created_at_utc": datetime.now(UTC).isoformat(),
        }
        self._store.publish_bytes(
            key,
            (json.dumps(started, separators=(",", ":"), sort_keys=True) + "\n").encode(),
        )
        async with self._factory.begin() as session:
            await session.execute(text("SET LOCAL statement_timeout = '5s'"))
            ids = list(
                await session.scalars(
                    select(MarketEvent.id)
                    .where(
                        MarketEvent.id >= candidate.min_raw_event_id,
                        MarketEvent.id <= candidate.max_raw_event_id,
                    )
                    .order_by(MarketEvent.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            if ids:
                await session.execute(delete(MarketEvent).where(MarketEvent.id.in_(ids)))
        completed = {
            **started,
            "status": "completed",
            "deleted_rows": len(ids),
            "completed_at_utc": datetime.now(UTC).isoformat(),
        }
        self._store.publish_bytes(
            key,
            (json.dumps(completed, separators=(",", ":"), sort_keys=True) + "\n").encode(),
        )
        return len(ids)
