import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.storage.models import MarketEvent

VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("Export timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def next_version_slug(output_root: Path, timestamp: datetime) -> str:
    highest = 0
    if output_root.is_dir():
        for path in output_root.iterdir():
            match = re.match(r"^v(\d+)_", path.name)
            if path.is_dir() and match:
                highest = max(highest, int(match.group(1)))
    return f"v{highest + 1}_{timestamp.astimezone(UTC):%Y%m%d}"


def validate_version(version: str) -> str:
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("Version must contain only letters, digits, dot, underscore, or dash.")
    return version


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _table(events: list[MarketEvent]) -> pa.Table:
    schema = pa.schema(
        [
            ("id", pa.int64()),
            ("source", pa.string()),
            ("topic", pa.string()),
            ("symbol", pa.string()),
            ("exchange_at", pa.timestamp("us", tz="UTC")),
            ("sequence", pa.int64()),
            ("received_at", pa.timestamp("us", tz="UTC")),
            ("latency_ms", pa.float64()),
            ("payload_json", pa.string()),
        ]
    )
    return pa.Table.from_pylist(
        [
            {
                "id": event.id,
                "source": event.source,
                "topic": event.event_type,
                "symbol": event.symbol,
                "exchange_at": event.exchange_at,
                "sequence": event.sequence,
                "received_at": event.received_at,
                "latency_ms": event.latency_ms,
                "payload_json": json.dumps(
                    event.payload, separators=(",", ":"), sort_keys=True
                ),
            }
            for event in events
        ],
        schema=schema,
    )


def write_versioned_export(
    *,
    events: list[MarketEvent],
    output_root: Path,
    version: str,
    symbol: str,
    start: datetime | None,
    end: datetime | None,
) -> Path:
    version = validate_version(version)
    dataset_dir = output_root / version
    if dataset_dir.exists():
        raise FileExistsError(f"Dataset version already exists: {version}")
    ordered = sorted(events, key=lambda event: (event.received_at, event.id))
    dataset_dir.mkdir(parents=True)
    filename = symbol.replace("/", "-") + ".parquet"
    parquet_path = dataset_dir / filename
    pq.write_table(_table(ordered), parquet_path, compression="zstd", version="2.6")
    actual_start = start or (ordered[0].received_at if ordered else None)
    actual_end = end or (ordered[-1].received_at if ordered else None)
    manifest: dict[str, Any] = {
        "version": version,
        "schema_version": 1,
        "symbol": symbol,
        "exchange": "hibachi",
        "start_utc": actual_start.astimezone(UTC).isoformat() if actual_start else None,
        "end_utc": actual_end.astimezone(UTC).isoformat() if actual_end else None,
        "row_count": len(ordered),
        "parquet_file": filename,
        "sha256": _checksum(parquet_path),
    }
    (dataset_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return dataset_dir


class VersionedDatasetExporter:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def export(
        self,
        *,
        output_root: Path,
        symbol: str,
        version: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Path:
        start = _utc(start)
        end = _utc(end)
        if start is not None and end is not None and start >= end:
            raise ValueError("Export start must be earlier than end.")
        statement = select(MarketEvent).where(MarketEvent.symbol == symbol)
        if start is not None:
            statement = statement.where(MarketEvent.received_at >= start)
        if end is not None:
            statement = statement.where(MarketEvent.received_at < end)
        statement = statement.order_by(MarketEvent.received_at, MarketEvent.id)
        async with self._session_factory() as session:
            events = list((await session.scalars(statement)).all())
        export_version = version or next_version_slug(output_root, end or datetime.now(UTC))
        return write_versioned_export(
            events=events,
            output_root=output_root,
            version=export_version,
            symbol=symbol,
            start=start,
            end=end,
        )
