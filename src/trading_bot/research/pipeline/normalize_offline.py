"""Offline RAW events.parquet → typed normalized Parquet (no PostgreSQL writes)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trading_bot.normalization.parsers import (
    BestQuoteRecord,
    FundingEstimateRecord,
    NormalizationFailure,
    OrderBookRecord,
    ReferencePriceRecord,
    parse_market_event,
)
from trading_bot.research.pipeline import RESEARCH_PIPELINE_VERSION
from trading_bot.research.pipeline.trades import (
    TradeRecord,
    parse_trade_event,
    raw_row_to_market_event,
)


@dataclass(frozen=True, slots=True)
class NormalizeStats:
    input_rows: int
    normalized_rows: int
    trade_rows: int
    error_rows: int
    by_topic: dict[str, int]
    quality_counts: dict[str, int]


def iter_event_rows(events_parquet: Path, *, batch_size: int = 5_000) -> Iterator[dict[str, Any]]:
    """Stream research events.parquet rows without loading the full file."""

    pf = pq.ParquetFile(events_parquet)
    for batch in pf.iter_batches(batch_size=batch_size):
        yield from batch.to_pylist()


def _provenance_dict(record: Any) -> dict[str, Any]:
    p = record.provenance
    return {
        "raw_event_id": p.raw_event_id,
        "received_at": p.received_at,
        "available_at": p.available_at,
        "exchange_at": p.exchange_at,
        "symbol": p.symbol,
        "source": p.source,
        "connection_id": p.connection_id,
        "local_sequence": p.local_sequence,
        "exchange_sequence": p.exchange_sequence,
        "raw_schema_version": p.raw_schema_version,
        "pipeline_version": p.pipeline_version,
        "research_pipeline_version": RESEARCH_PIPELINE_VERSION,
        "data_quality": p.data_quality,
    }


_TS_UTC = pa.timestamp("us", tz="UTC")


def _provenance_schema_fields() -> list[pa.Field]:
    """Nullable UTC timestamps so all-null ``exchange_at`` still writes Parquet."""

    return [
        pa.field("raw_event_id", pa.int64()),
        pa.field("received_at", _TS_UTC),
        pa.field("available_at", _TS_UTC),
        pa.field("exchange_at", _TS_UTC),
        pa.field("symbol", pa.string()),
        pa.field("source", pa.string()),
        pa.field("connection_id", pa.string()),
        pa.field("local_sequence", pa.int64()),
        pa.field("exchange_sequence", pa.int64()),
        pa.field("raw_schema_version", pa.int64()),
        pa.field("pipeline_version", pa.int64()),
        pa.field("research_pipeline_version", pa.int64()),
        pa.field("data_quality", pa.string()),
        pa.field("topic", pa.string()),
    ]


_TOPIC_SCHEMAS: dict[str, pa.Schema] = {
    "ask_bid_price": pa.schema(
        _provenance_schema_fields()
        + [
            pa.field("bid_price", pa.string()),
            pa.field("bid_size", pa.string()),
            pa.field("ask_price", pa.string()),
            pa.field("ask_size", pa.string()),
        ]
    ),
    "mark_price": pa.schema(
        _provenance_schema_fields()
        + [
            pa.field("price_kind", pa.string()),
            pa.field("price", pa.string()),
        ]
    ),
    "spot_price": pa.schema(
        _provenance_schema_fields()
        + [
            pa.field("price_kind", pa.string()),
            pa.field("price", pa.string()),
        ]
    ),
    "funding_rate_estimation": pa.schema(
        _provenance_schema_fields()
        + [
            pa.field("estimated_rate", pa.string()),
            pa.field("next_funding_at", _TS_UTC),
        ]
    ),
    "orderbook": pa.schema(
        _provenance_schema_fields()
        + [
            pa.field("message_type", pa.string()),
            pa.field("depth", pa.int64()),
            pa.field("granularity", pa.string()),
            pa.field("bids_json", pa.string()),
            pa.field("asks_json", pa.string()),
        ]
    ),
    "trades": pa.schema(
        _provenance_schema_fields()
        + [
            pa.field("price", pa.string()),
            pa.field("quantity", pa.string()),
            pa.field("taker_side", pa.string()),
            pa.field("exchange_trade_at", _TS_UTC),
        ]
    ),
}

_ERROR_SCHEMA = pa.schema(
    [
        pa.field("raw_event_id", pa.int64()),
        pa.field("topic", pa.string()),
        pa.field("error", pa.string()),
        pa.field("received_at", pa.string()),
    ]
)


def _record_to_row(topic: str, record: Any) -> dict[str, Any]:
    base = _provenance_dict(record)
    base["topic"] = topic
    if isinstance(record, BestQuoteRecord):
        base.update(
            {
                "bid_price": str(record.bid_price),
                "bid_size": str(record.bid_size),
                "ask_price": str(record.ask_price),
                "ask_size": str(record.ask_size),
            }
        )
    elif isinstance(record, ReferencePriceRecord):
        base.update({"price_kind": record.price_kind, "price": str(record.price)})
    elif isinstance(record, FundingEstimateRecord):
        base.update(
            {
                "estimated_rate": str(record.estimated_rate),
                "next_funding_at": record.next_funding_at,
            }
        )
    elif isinstance(record, OrderBookRecord):
        base.update(
            {
                "message_type": record.message_type,
                "depth": record.depth,
                "granularity": str(record.granularity),
                "bids_json": json.dumps([level.compact() for level in record.bids]),
                "asks_json": json.dumps([level.compact() for level in record.asks]),
            }
        )
    elif isinstance(record, TradeRecord):
        base.update(
            {
                "price": str(record.price),
                "quantity": str(record.quantity),
                "taker_side": record.taker_side,
                "exchange_trade_at": record.exchange_trade_at,
            }
        )
    else:
        raise TypeError(f"unsupported record type {type(record)}")
    return base


def normalize_events_parquet(
    events_parquet: Path,
    output_dir: Path,
    *,
    batch_size: int = 5_000,
) -> NormalizeStats:
    """Normalize a research events.parquet into typed Parquet partitions.

    Large corpora are sorted with PyArrow (columnar) and flushed in batches so
    the full Python row list is never retained.
    """

    output_dir.mkdir(parents=True, exist_ok=True)
    topic_names = (
        "ask_bid_price",
        "mark_price",
        "spot_price",
        "funding_rate_estimation",
        "orderbook",
        "trades",
    )
    buffers: dict[str, list[dict[str, Any]]] = {name: [] for name in topic_names}
    writers: dict[str, pq.ParquetWriter] = {}
    errors: list[dict[str, Any]] = []
    by_topic: dict[str, int] = {}
    quality_counts: dict[str, int] = {}
    flush_every = max(1_000, batch_size)

    def _flush(topic: str, *, force: bool = False) -> None:
        items = buffers[topic]
        if not items:
            return
        if not force and len(items) < flush_every:
            return
        schema = _TOPIC_SCHEMAS[topic]
        table = pa.Table.from_pylist(items, schema=schema)
        writer = writers.get(topic)
        if writer is None:
            writers[topic] = pq.ParquetWriter(
                output_dir / f"{topic}.parquet",
                schema,
                compression="zstd",
            )
            writer = writers[topic]
        writer.write_table(table)
        items.clear()

    # Columnar load + sort avoids materializing 1M+ Python dicts before parsing.
    table = pq.read_table(events_parquet)
    id_column = "raw_event_id" if "raw_event_id" in table.column_names else "id"
    table = table.sort_by(
        [("received_at", "ascending"), (id_column, "ascending")]
    )
    input_rows = table.num_rows

    try:
        for batch in table.to_batches(max_chunksize=batch_size):
            for row in batch.to_pylist():
                topic = str(row.get("topic") or row.get("event_type") or "")
                try:
                    event = raw_row_to_market_event(row)
                    if topic == "trades":
                        record: Any = parse_trade_event(event)
                    else:
                        record = parse_market_event(event)
                    if topic not in buffers:
                        raise NormalizationFailure("unsupported_event_type", topic)
                    buffers[topic].append(_record_to_row(topic, record))
                    by_topic[topic] = by_topic.get(topic, 0) + 1
                    quality = record.provenance.data_quality
                    quality_counts[quality] = quality_counts.get(quality, 0) + 1
                    _flush(topic)
                except (NormalizationFailure, ValueError, KeyError, TypeError) as exc:
                    errors.append(
                        {
                            "raw_event_id": row.get("raw_event_id", row.get("id")),
                            "topic": topic,
                            "error": str(exc)[:200],
                            "received_at": str(row.get("received_at") or ""),
                        }
                    )
        for topic in topic_names:
            _flush(topic, force=True)
    finally:
        for writer in writers.values():
            writer.close()
    if errors:
        try:
            pq.write_table(
                pa.Table.from_pylist(errors, schema=_ERROR_SCHEMA),
                output_dir / "normalization_errors.parquet",
                compression="zstd",
            )
        except (TypeError, ValueError, pa.ArrowInvalid) as exc:
            # Topic files already closed; do not abort a successful normalize
            # because the error sidecar could not be written.
            print(
                json.dumps(
                    {
                        "phase": "normalization_errors_write_failed",
                        "error_type": type(exc).__name__,
                        "n_errors": len(errors),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

    return NormalizeStats(
        input_rows=input_rows,
        normalized_rows=sum(by_topic.values()),
        trade_rows=by_topic.get("trades", 0),
        error_rows=len(errors),
        by_topic=by_topic,
        quality_counts=quality_counts,
    )


def assert_available_at_equals_received_at(rows: list[dict[str, Any]]) -> None:
    """Hard invariant: available_at == received_at unless separately proven later."""

    for row in rows:
        avail = row["available_at"]
        recv = row["received_at"]
        if isinstance(avail, str):
            avail = datetime.fromisoformat(avail.replace("Z", "+00:00"))
        if isinstance(recv, str):
            recv = datetime.fromisoformat(recv.replace("Z", "+00:00"))
        if avail != recv:
            raise AssertionError(
                f"available_at must equal received_at for raw_event_id={row.get('raw_event_id')}"
            )
