import asyncio
import hashlib
import json
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.archive.manifest import (
    MANIFEST_SCHEMA_VERSION,
    ArchiveCheckpoint,
    ArchiveManifest,
    ArchiveObject,
    raw_id_digest,
    sha256_bytes,
    utc_iso,
)
from trading_bot.archive.schemas import SCHEMAS
from trading_bot.archive.store import ArchiveStore
from trading_bot.normalization.parsers import (
    PIPELINE_VERSION,
    BestQuoteRecord,
    FundingEstimateRecord,
    NormalizationFailure,
    OrderBookRecord,
    ReferencePriceRecord,
    parse_market_event,
)
from trading_bot.normalization.resources import (
    ResourceLimits,
    ResourceProbe,
    SystemResourceProbe,
    evaluate_resources,
)
from trading_bot.storage.models import MarketEvent

DEFAULT_ARCHIVE_BATCH_SIZE = 5000
MAX_ARCHIVE_BATCH_SIZE = 10000
ArchiveBatchReader = Callable[
    ["ArchiveRequest", int],
    Awaitable[list[MarketEvent]],
]
SUPPORTED_EVENT_TYPES = frozenset(
    {
        "ask_bid_price",
        "mark_price",
        "spot_price",
        "funding_rate_estimation",
        "orderbook",
    }
)


@dataclass(frozen=True, slots=True)
class ArchiveRequest:
    start: datetime
    end: datetime
    symbol: str
    work_dir: Path
    capacity_path: Path
    batch_size: int = DEFAULT_ARCHIVE_BATCH_SIZE
    initial_raw_event_id: int = 0
    inter_batch_delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("archive interval must be timezone-aware")
        start = self.start.astimezone(UTC)
        end = self.end.astimezone(UTC)
        if start >= end or end - start != timedelta(days=1):
            raise ValueError("archive interval must be exactly one UTC day")
        if start.time() != datetime.min.time():
            raise ValueError("archive interval must start at UTC midnight")
        if not self.symbol:
            raise ValueError("archive symbol is required")
        if not 1 <= self.batch_size <= MAX_ARCHIVE_BATCH_SIZE:
            raise ValueError(f"archive batch size must be between 1 and {MAX_ARCHIVE_BATCH_SIZE}")
        if self.initial_raw_event_id < 0:
            raise ValueError("initial RAW event ID must be non-negative")
        if not 0 <= self.inter_batch_delay_seconds <= 10:
            raise ValueError("archive inter-batch delay must be between 0 and 10 seconds")


def _symbol_slug(symbol: str) -> str:
    return symbol.replace("/", "-").replace(":", "-")


def _partition_prefix(request: ArchiveRequest) -> str:
    day = request.start.astimezone(UTC).date().isoformat()
    return f"date={day}/symbol={_symbol_slug(request.symbol)}"


def _checkpoint_key(request: ArchiveRequest) -> str:
    return f"_state/checkpoints/{_partition_prefix(request)}.json"


def _manifest_key(request: ArchiveRequest) -> str:
    return f"_manifests/{_partition_prefix(request)}.json"


def _schema_digest(schema: pa.Schema) -> str:
    return sha256_bytes(str(schema.remove_metadata()).encode("utf-8"))


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _provenance(record: Any) -> dict[str, Any]:
    return asdict(record.provenance)


def _rows(events: list[MarketEvent]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {name: [] for name in SCHEMAS}
    for event in events:
        result["raw"].append(
            {
                "raw_event_id": event.id,
                "received_at": event.received_at,
                "exchange_at": event.exchange_at,
                "source": event.source,
                "event_type": event.event_type,
                "symbol": event.symbol,
                "connection_id": event.connection_id,
                "local_sequence": event.local_sequence,
                "exchange_sequence": event.exchange_sequence,
                "raw_schema_version": event.schema_version or 1,
                "legacy_sequence": event.sequence,
                "latency_ms": event.latency_ms,
                "payload_json": json.dumps(
                    event.payload,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            }
        )
        if event.event_type not in SUPPORTED_EVENT_TYPES:
            continue
        try:
            record = parse_market_event(event)
        except NormalizationFailure as error:
            result["normalization_errors"].append(
                {
                    "raw_event_id": event.id,
                    "received_at": event.received_at,
                    "event_type": event.event_type,
                    "pipeline_version": PIPELINE_VERSION,
                    "error_code": error.code[:64],
                    "error_detail": error.detail[:160],
                }
            )
        else:
            values = _provenance(record)
            if isinstance(record, BestQuoteRecord):
                values.update(
                    bid_price=record.bid_price,
                    bid_size=record.bid_size,
                    ask_price=record.ask_price,
                    ask_size=record.ask_size,
                )
                result["best_quotes"].append(values)
            elif isinstance(record, ReferencePriceRecord):
                values.update(price_kind=record.price_kind, price=record.price)
                result["reference_prices"].append(values)
            elif isinstance(record, FundingEstimateRecord):
                values.update(
                    estimated_rate=record.estimated_rate,
                    next_funding_at=record.next_funding_at,
                )
                result["funding_estimates"].append(values)
            elif isinstance(record, OrderBookRecord):
                values.update(
                    message_type=record.message_type,
                    depth=record.depth,
                    granularity=record.granularity,
                    bids=[asdict(level) for level in record.bids],
                    asks=[asdict(level) for level in record.asks],
                    changed_level_count=len(record.bids) + len(record.asks),
                )
                result["orderbook_events"].append(values)
    return result


def _write_chunk(
    *,
    dataset: str,
    rows: list[dict[str, Any]],
    directory: Path,
    first_id: int,
    last_id: int,
) -> tuple[Path, ArchiveObject]:
    schema = SCHEMAS[dataset]
    table = pa.Table.from_pylist(rows, schema=schema)
    path = directory / f"{dataset}-{first_id}-{last_id}.parquet"
    pq.write_table(
        table,
        path,
        compression="zstd",
        compression_level=6,
        row_group_size=min(len(rows), 10000),
        write_statistics=True,
        version="2.6",
    )
    parquet_schema = pq.read_schema(path)
    ids = table.column("raw_event_id").to_pylist()
    return path, ArchiveObject(
        dataset=dataset,
        key="",
        row_count=table.num_rows,
        size_bytes=path.stat().st_size,
        sha256=_file_digest(path),
        min_raw_event_id=min(ids),
        max_raw_event_id=max(ids),
        raw_id_sha256=raw_id_digest(ids),
        parquet_schema_sha256=_schema_digest(parquet_schema),
    )


class ArchiveExporter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
        store: ArchiveStore,
        *,
        batch_reader: ArchiveBatchReader | None = None,
        resource_probe: ResourceProbe | None = None,
        resource_limits: ResourceLimits | None = None,
    ) -> None:
        if session_factory is None and batch_reader is None:
            raise ValueError("archive source is required")
        self._factory = session_factory
        self._batch_reader = batch_reader
        self._store = store
        self._probe = resource_probe or SystemResourceProbe()
        self._limits = resource_limits or ResourceLimits(
            estimated_output_bytes_per_raw=2048
        )

    async def export_day(self, request: ArchiveRequest) -> ArchiveManifest:
        manifest_key = _manifest_key(request)
        if self._store.exists(manifest_key):
            manifest = ArchiveManifest.from_bytes(self._store.read_bytes(manifest_key))
            self.verify(manifest, request.work_dir)
            return manifest
        checkpoint = self._load_checkpoint(request)
        objects = list(checkpoint.objects) if checkpoint else []
        last_id = (
            checkpoint.last_raw_event_id
            if checkpoint
            else request.initial_raw_event_id
        )
        raw_count = checkpoint.row_count if checkpoint else 0
        request.work_dir.mkdir(parents=True, exist_ok=True)
        while True:
            decision = evaluate_resources(
                probe=self._probe,
                path=request.capacity_path,
                limits=self._limits,
                batch_size=request.batch_size,
            )
            if decision.state != "run":
                raise RuntimeError(f"archive resource gate: {decision.reason}")
            events = await self._read_batch(request, last_id)
            if not events:
                break
            with tempfile.TemporaryDirectory(dir=request.work_dir) as temporary:
                batch_objects = self._publish_batch(
                    request,
                    events,
                    Path(temporary),
                )
            objects.extend(batch_objects)
            last_id = events[-1].id
            raw_count += len(events)
            checkpoint = ArchiveCheckpoint(
                interval_start_utc=utc_iso(request.start),
                interval_end_utc=utc_iso(request.end),
                symbol=request.symbol,
                last_raw_event_id=last_id,
                row_count=raw_count,
                objects=tuple(objects),
            )
            self._store.publish_bytes(_checkpoint_key(request), checkpoint.to_bytes())
            if request.inter_batch_delay_seconds:
                await asyncio.sleep(request.inter_batch_delay_seconds)
        raw_objects = [item for item in objects if item.dataset == "raw"]
        if not raw_objects:
            raise RuntimeError("archive interval is empty")
        raw_id_hash = sha256_bytes(
            "".join(item.raw_id_sha256 for item in raw_objects).encode("ascii")
        )
        manifest = ArchiveManifest(
            dataset_group="raw_and_normalized",
            interval_start_utc=utc_iso(request.start),
            interval_end_utc=utc_iso(request.end),
            symbol=request.symbol,
            min_raw_event_id=min(item.min_raw_event_id for item in raw_objects),
            max_raw_event_id=max(item.max_raw_event_id for item in raw_objects),
            raw_row_count=sum(item.row_count for item in raw_objects),
            raw_id_sha256=raw_id_hash,
            pipeline_version=PIPELINE_VERSION,
            schema_version=MANIFEST_SCHEMA_VERSION,
            created_at_utc=datetime.now(UTC).isoformat(),
            destination=self._store.destination_label,
            verification_status="verified",
            objects=tuple(objects),
        )
        self.verify(manifest, request.work_dir)
        self._store.publish_bytes(manifest_key, manifest.to_bytes())
        return manifest

    def _load_checkpoint(self, request: ArchiveRequest) -> ArchiveCheckpoint | None:
        key = _checkpoint_key(request)
        if not self._store.exists(key):
            return None
        checkpoint = ArchiveCheckpoint.from_bytes(self._store.read_bytes(key))
        if (
            checkpoint.interval_start_utc != utc_iso(request.start)
            or checkpoint.interval_end_utc != utc_iso(request.end)
            or checkpoint.symbol != request.symbol
        ):
            raise RuntimeError("archive checkpoint is incompatible")
        return checkpoint

    async def _read_batch(
        self,
        request: ArchiveRequest,
        last_id: int,
    ) -> list[MarketEvent]:
        if self._batch_reader is not None:
            return await self._batch_reader(request, last_id)
        if self._factory is None:
            raise RuntimeError("archive source is unavailable")
        async with self._factory() as session, session.begin():
            await session.execute(text("SET TRANSACTION READ ONLY"))
            await session.execute(text("SET LOCAL statement_timeout = '5s'"))
            return list(
                await session.scalars(
                    select(MarketEvent)
                    .where(
                        MarketEvent.id > last_id,
                        MarketEvent.received_at >= request.start,
                        MarketEvent.received_at < request.end,
                        MarketEvent.symbol == request.symbol,
                    )
                    .order_by(MarketEvent.id)
                    .limit(request.batch_size)
                )
            )

    def _publish_batch(
        self,
        request: ArchiveRequest,
        events: list[MarketEvent],
        directory: Path,
    ) -> list[ArchiveObject]:
        first_id = events[0].id
        last_id = events[-1].id
        prefix = _partition_prefix(request)
        published: list[ArchiveObject] = []
        for dataset, rows in _rows(events).items():
            if not rows:
                continue
            path, partial = _write_chunk(
                dataset=dataset,
                rows=rows,
                directory=directory,
                first_id=first_id,
                last_id=last_id,
            )
            key = f"{dataset}/{prefix}/part-{first_id}-{last_id}.parquet"
            complete = ArchiveObject(**{**asdict(partial), "key": key})
            self._store.publish_file(key, path)
            published.append(complete)
        return published

    def verify(self, manifest: ArchiveManifest, work_dir: Path) -> None:
        work_dir.mkdir(parents=True, exist_ok=True)
        raw_objects = [item for item in manifest.objects if item.dataset == "raw"]
        if sum(item.row_count for item in raw_objects) != manifest.raw_row_count:
            raise RuntimeError("archive manifest row count mismatch")
        raw_hash = sha256_bytes(
            "".join(item.raw_id_sha256 for item in raw_objects).encode("ascii")
        )
        if raw_hash != manifest.raw_id_sha256:
            raise RuntimeError("archive manifest RAW identity mismatch")
        with tempfile.TemporaryDirectory(dir=work_dir) as temporary:
            for index, item in enumerate(manifest.objects):
                path = Path(temporary) / f"{index}.parquet"
                self._store.download_file(item.key, path)
                if _file_digest(path) != item.sha256 or path.stat().st_size != item.size_bytes:
                    raise RuntimeError("archive object checksum mismatch")
                with path.open("rb") as handle:
                    parquet = pq.ParquetFile(handle)
                    if parquet.metadata.num_rows != item.row_count:
                        raise RuntimeError("archive object row count mismatch")
                    expected_schema = SCHEMAS.get(item.dataset)
                    if expected_schema is None or not parquet.schema_arrow.equals(
                        expected_schema,
                        check_metadata=False,
                    ):
                        raise RuntimeError("archive object schema is unsupported")
                    if _schema_digest(parquet.schema_arrow) != item.parquet_schema_sha256:
                        raise RuntimeError("archive object schema mismatch")
                    ids = parquet.read(columns=["raw_event_id"]).column(0).to_pylist()
                if (
                    not ids
                    or min(ids) != item.min_raw_event_id
                    or max(ids) != item.max_raw_event_id
                    or raw_id_digest(ids) != item.raw_id_sha256
                ):
                    raise RuntimeError("archive object RAW identity mismatch")
