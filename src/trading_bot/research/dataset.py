import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.storage.models import MarketEvent

SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = {SCHEMA_VERSION}
DATASET_FILES = {"events.parquet", "candles_1s.parquet", "README.md"}


class DatasetValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    trade_count: int


@dataclass(slots=True)
class _CandleBuilder:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int
    volumes_seen: int


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Dataset timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def generate_dataset_id(
    symbol: str,
    start: datetime,
    end: datetime,
    schema_version: int = SCHEMA_VERSION,
) -> str:
    start = _utc(start)
    end = _utc(end)
    if start >= end:
        raise ValueError("Dataset start must be earlier than end.")
    safe_symbol = symbol.lower().replace("/", "-")
    def compact(value: datetime) -> str:
        return value.strftime("%Y%m%dT%H%M%S%fZ")

    return f"{safe_symbol}_{compact(start)}_{compact(end)}_v{schema_version}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value: Any, *, positive: bool = False) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if positive and number <= 0:
        return None
    return number


def _payload_number(payload: dict[str, Any], names: tuple[str, ...]) -> float | None:
    containers = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        containers.append(data)
    for container in containers:
        for name in names:
            if name in container:
                return _number(container[name], positive=True)
    return None


def aggregate_candles(events: list[MarketEvent]) -> list[Candle]:
    builders: dict[datetime, _CandleBuilder] = {}
    for event in sorted(events, key=lambda item: (item.received_at, item.id)):
        if event.event_type != "trades":
            continue
        price = _payload_number(event.payload, ("price", "tradePrice", "trade_price", "p"))
        if price is None:
            continue
        timestamp = (event.exchange_at or event.received_at).astimezone(UTC).replace(
            microsecond=0
        )
        volume = _payload_number(
            event.payload,
            ("quantity", "size", "amount", "volume", "qty", "q"),
        )
        builder = builders.get(timestamp)
        if builder is None:
            builders[timestamp] = _CandleBuilder(
                timestamp=timestamp,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=volume or 0.0,
                trade_count=1,
                volumes_seen=int(volume is not None),
            )
            continue
        builder.high = max(builder.high, price)
        builder.low = min(builder.low, price)
        builder.close = price
        builder.trade_count += 1
        if volume is not None:
            builder.volume += volume
            builder.volumes_seen += 1
    return [
        Candle(
            timestamp=builder.timestamp,
            open=builder.open,
            high=builder.high,
            low=builder.low,
            close=builder.close,
            volume=(builder.volume if builder.volumes_seen == builder.trade_count else None),
            trade_count=builder.trade_count,
        )
        for builder in sorted(builders.values(), key=lambda item: item.timestamp)
    ]


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _write_events(path: Path, events: list[MarketEvent]) -> None:
    schema = pa.schema(
        [
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
    rows = [
        {
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
    ]
    table = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(table, path, compression="zstd", version="2.6")


def _write_candles(path: Path, candles: list[Candle]) -> None:
    schema = pa.schema(
        [
            ("timestamp", pa.timestamp("us", tz="UTC")),
            ("open", pa.float64()),
            ("high", pa.float64()),
            ("low", pa.float64()),
            ("close", pa.float64()),
            ("volume", pa.float64()),
            ("trade_count", pa.int64()),
        ]
    )
    table = pa.Table.from_pylist([asdict(candle) for candle in candles], schema=schema)
    pq.write_table(table, path, compression="zstd", version="2.6")


def validate_manifest(dataset_dir: Path) -> dict[str, Any]:
    manifest_path = dataset_dir / "manifest.json"
    try:
        manifest = cast(
            dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError) as error:
        raise DatasetValidationError(f"Invalid dataset manifest: {error}") from error
    if manifest.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
        raise DatasetValidationError(
            f"Unsupported dataset schema version: {manifest.get('schema_version')!r}"
        )
    if manifest.get("dataset_id") != dataset_dir.name:
        raise DatasetValidationError("Manifest dataset_id does not match directory name.")
    try:
        expected_id = generate_dataset_id(
            str(manifest["symbol"]),
            datetime.fromisoformat(str(manifest["start_utc"])),
            datetime.fromisoformat(str(manifest["end_utc"])),
            int(manifest["schema_version"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DatasetValidationError(f"Manifest metadata is invalid: {error}") from error
    if expected_id != manifest["dataset_id"]:
        raise DatasetValidationError("Manifest dataset_id is inconsistent with its metadata.")
    row_counts = manifest.get("row_counts")
    if not isinstance(row_counts, dict) or not all(
        isinstance(row_counts.get(name), int) and row_counts[name] >= 0
        for name in ("events", "candles_1s")
    ):
        raise DatasetValidationError("Manifest row counts are invalid.")
    checksums = manifest.get("checksums")
    if not isinstance(checksums, dict) or set(checksums) != DATASET_FILES:
        raise DatasetValidationError("Manifest checksums are missing or unexpected.")
    for name, expected in checksums.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            raise DatasetValidationError("Manifest checksum entry is invalid.")
        path = dataset_dir / name
        if not path.is_file() or sha256_file(path) != expected:
            raise DatasetValidationError(f"Dataset checksum mismatch: {name}")
    return manifest


class DatasetExporter:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def export(
        self,
        *,
        symbol: str,
        start: datetime,
        end: datetime,
        output_root: Path,
    ) -> Path:
        start = _utc(start)
        end = _utc(end)
        dataset_id = generate_dataset_id(symbol, start, end)
        dataset_dir = output_root / dataset_id
        if dataset_dir.exists():
            raise FileExistsError(f"Dataset already exists: {dataset_dir}")
        statement = (
            select(MarketEvent)
            .where(
                MarketEvent.symbol == symbol,
                MarketEvent.received_at >= start,
                MarketEvent.received_at < end,
            )
            .order_by(MarketEvent.received_at, MarketEvent.id)
        )
        async with self._session_factory() as session:
            events = list((await session.scalars(statement)).all())
        return write_dataset(
            events=events,
            symbol=symbol,
            start=start,
            end=end,
            output_root=output_root,
        )


def write_dataset(
    *,
    events: list[MarketEvent],
    symbol: str,
    start: datetime,
    end: datetime,
    output_root: Path,
) -> Path:
    start = _utc(start)
    end = _utc(end)
    dataset_id = generate_dataset_id(symbol, start, end)
    dataset_dir = output_root / dataset_id
    if dataset_dir.exists():
        raise FileExistsError(f"Dataset already exists: {dataset_dir}")
    events = sorted(events, key=lambda item: (item.received_at, item.id))
    candles = aggregate_candles(events)
    dataset_dir.mkdir(parents=True)
    events_path = dataset_dir / "events.parquet"
    candles_path = dataset_dir / "candles_1s.parquet"
    readme_path = dataset_dir / "README.md"
    _write_events(events_path, events)
    _write_candles(candles_path, candles)
    readme_path.write_text(
        "# Research dataset\n\n"
        "Immutable export of public market events. `candles_1s.parquet` uses only "
        "trade events with a parseable positive price. Volume is null for a second "
        "when any contributing trade has no parseable quantity. No values are invented.\n",
        encoding="utf-8",
        newline="\n",
    )
    exported_at = max((event.received_at for event in events), default=end).astimezone(UTC)
    files = (events_path, candles_path, readme_path)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "symbol": symbol,
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
        "row_counts": {"events": len(events), "candles_1s": len(candles)},
        "exported_at_utc": exported_at.isoformat(),
        "software": {"version": "0.1.0", "git_commit": _git_commit()},
        "checksums": {path.name: sha256_file(path) for path in files},
    }
    (dataset_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return dataset_dir
