"""Compression and Parquet conversion for sealed external segments."""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trading_bot.external_market_data.offload.segments import (
    iter_ndjson_records,
    sha256_file,
)

try:
    import resource as _resource
except ImportError:  # Windows local tests
    _resource = None  # type: ignore[assignment]


# Envelope fields required for durable RAW provenance / round-trip.
# Durable RAW provenance. stream/exchange_at may be null on some rows.
REQUIRED_FIELDS = (
    "schema_version",
    "venue",
    "instrument",
    "event_type",
    "received_at",
    "local_sequence",
    "connection_id",
    "payload",
)


def gzip_ndjson(source: Path, destination: Path, *, compresslevel: int = 4) -> dict[str, Any]:
    # Level 4: stream file→gzip file (1 MiB chunks). Lower than 6 to cut CPU on
    # the 1-vCPU VPS so Hibachi healthchecks are not starved; SoT remains gzip NDJSON.
    started = time.perf_counter()
    rss_before = _rss_bytes()
    with (
        source.open("rb") as src,
        destination.open("wb") as raw_out,
        gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw_out,
            compresslevel=compresslevel,
            mtime=0,
        ) as dst,
    ):
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
    elapsed = time.perf_counter() - started
    return {
        "raw_bytes": source.stat().st_size,
        "gzip_bytes": destination.stat().st_size,
        "ratio": destination.stat().st_size / max(source.stat().st_size, 1),
        "elapsed_seconds": elapsed,
        "peak_rss_delta_bytes": max(0, _rss_bytes() - rss_before),
        "sha256_raw": sha256_file(source),
        "sha256_gzip": sha256_file(destination),
    }


def gunzip_ndjson(source: Path, destination: Path) -> None:
    with gzip.open(source, "rb") as src, destination.open("wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)


def ndjson_to_parquet(source: Path, destination: Path) -> dict[str, Any]:
    """Convert sealed NDJSON envelopes to Parquet with JSON-serialized payload."""

    started = time.perf_counter()
    rss_before = _rss_bytes()
    columns: dict[str, list[Any]] = {
        "schema_version": [],
        "venue": [],
        "instrument": [],
        "stream": [],
        "event_type": [],
        "received_at": [],
        "exchange_at": [],
        "local_sequence": [],
        "connection_id": [],
        "payload_json": [],
        "book_update_id": [],
        "agg_trade_id": [],
        "latency_ms": [],
    }
    count = 0
    for row in iter_ndjson_records(source):
        for field in REQUIRED_FIELDS:
            if field not in row:
                raise ValueError(f"missing required field {field}")
        columns["schema_version"].append(int(row["schema_version"]))
        columns["venue"].append(str(row["venue"]))
        columns["instrument"].append(str(row["instrument"]))
        stream = row.get("stream")
        columns["stream"].append(None if stream is None else str(stream))
        columns["event_type"].append(str(row["event_type"]))
        columns["received_at"].append(str(row["received_at"]))
        columns["exchange_at"].append(
            None if row.get("exchange_at") is None else str(row["exchange_at"])
        )
        columns["local_sequence"].append(int(row["local_sequence"]))
        columns["connection_id"].append(str(row["connection_id"]))
        columns["payload_json"].append(
            json.dumps(row["payload"], separators=(",", ":"), sort_keys=True)
        )
        columns["book_update_id"].append(row.get("book_update_id"))
        columns["agg_trade_id"].append(row.get("agg_trade_id"))
        columns["latency_ms"].append(row.get("latency_ms"))
        count += 1
    table = pa.table(
        {
            "schema_version": pa.array(columns["schema_version"], type=pa.int32()),
            "venue": pa.array(columns["venue"], type=pa.string()),
            "instrument": pa.array(columns["instrument"], type=pa.string()),
            "stream": pa.array(columns["stream"], type=pa.string()),
            "event_type": pa.array(columns["event_type"], type=pa.string()),
            "received_at": pa.array(columns["received_at"], type=pa.string()),
            "exchange_at": pa.array(columns["exchange_at"], type=pa.string()),
            "local_sequence": pa.array(columns["local_sequence"], type=pa.int64()),
            "connection_id": pa.array(columns["connection_id"], type=pa.string()),
            "payload_json": pa.array(columns["payload_json"], type=pa.string()),
            "book_update_id": pa.array(columns["book_update_id"], type=pa.int64()),
            "agg_trade_id": pa.array(columns["agg_trade_id"], type=pa.int64()),
            "latency_ms": pa.array(columns["latency_ms"], type=pa.float64()),
        }
    )
    pq.write_table(table, destination, compression="zstd")
    elapsed = time.perf_counter() - started
    return {
        "raw_bytes": source.stat().st_size,
        "parquet_bytes": destination.stat().st_size,
        "ratio": destination.stat().st_size / max(source.stat().st_size, 1),
        "event_count": count,
        "elapsed_seconds": elapsed,
        "peak_rss_delta_bytes": max(0, _rss_bytes() - rss_before),
        "sha256_raw": sha256_file(source),
        "sha256_parquet": sha256_file(destination),
    }


def parquet_to_canonical_ndjson(source: Path, destination: Path) -> dict[str, Any]:
    """Round-trip Parquet back to canonical NDJSON for equivalence checks."""

    table = pq.read_table(source)
    rows = table.to_pydict()
    count = len(rows["local_sequence"])
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        for i in range(count):
            payload = json.loads(rows["payload_json"][i])
            envelope: dict[str, Any] = {
                "schema_version": rows["schema_version"][i],
                "venue": rows["venue"][i],
                "instrument": rows["instrument"][i],
                "stream": rows["stream"][i],
                "event_type": rows["event_type"][i],
                "received_at": rows["received_at"][i],
                "exchange_at": rows["exchange_at"][i],
                "local_sequence": rows["local_sequence"][i],
                "connection_id": rows["connection_id"][i],
                "payload": payload,
            }
            if rows["book_update_id"][i] is not None:
                envelope["book_update_id"] = rows["book_update_id"][i]
            if rows["agg_trade_id"][i] is not None:
                envelope["agg_trade_id"] = rows["agg_trade_id"][i]
            if rows["latency_ms"][i] is not None:
                envelope["latency_ms"] = rows["latency_ms"][i]
            handle.write(json.dumps(envelope, separators=(",", ":"), sort_keys=True))
            handle.write("\n")
    return {
        "event_count": count,
        "sha256": sha256_file(destination),
        "bytes": destination.stat().st_size,
    }


def canonical_ndjson_hash(source: Path) -> str:
    """Hash of sorted-key canonical NDJSON lines (order-preserving)."""

    import hashlib

    digest = hashlib.sha256()
    for row in iter_ndjson_records(source):
        line = json.dumps(row, separators=(",", ":"), sort_keys=True)
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def prove_round_trip(source_ndjson: Path, work_dir: Path) -> dict[str, Any]:
    """Prove required envelope + payload semantic equivalence via Parquet.

    Durable source-of-truth remains exact NDJSON (optionally gzip). Parquet is
    an analytical projection and may omit optional diagnostic fields.
    """

    work_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = work_dir / "events.parquet"
    back_path = work_dir / "roundtrip.ndjson"
    parquet_stats = ndjson_to_parquet(source_ndjson, parquet_path)
    back_stats = parquet_to_canonical_ndjson(parquet_path, back_path)
    mismatches = 0
    checked = 0
    for original, restored in zip(
        iter_ndjson_records(source_ndjson),
        iter_ndjson_records(back_path),
        strict=True,
    ):
        checked += 1
        for field in REQUIRED_FIELDS:
            if original.get(field) != restored.get(field):
                mismatches += 1
                break
        else:
            # Timestamps must round-trip exactly (no lookahead transforms).
            ts_mismatch = (
                original.get("exchange_at") != restored.get("exchange_at")
                or original.get("received_at") != restored.get("received_at")
            )
            if ts_mismatch:
                mismatches += 1
    return {
        "parquet": parquet_stats,
        "checked_events": checked,
        "field_mismatches": mismatches,
        "roundtrip_equal": mismatches == 0 and checked == parquet_stats["event_count"],
        "event_count_match": parquet_stats["event_count"] == back_stats["event_count"],
        "note": "gzip NDJSON is durable SoT; Parquet proves required-field semantics",
    }


def _rss_bytes() -> int:
    if _resource is None:
        return 0
    getrusage = getattr(_resource, "getrusage", None)
    rusage_self = getattr(_resource, "RUSAGE_SELF", None)
    if getrusage is None or rusage_self is None:
        return 0
    usage = int(getrusage(rusage_self).ru_maxrss)
    # Linux: kilobytes; Windows: bytes. Prefer Linux (VPS) semantics.
    if usage > 10_000_000:
        return usage
    return usage * 1024
