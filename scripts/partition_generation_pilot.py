"""Local disposable PostgreSQL pilot for RAW generation sizing and DROP reclaim.

Measures heap/index bytes, approximate insert rate, and DELETE vs DROP
filesystem/relation reclamation. Never targets production.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from trading_bot.storage.partitions import (
    DEFAULT_GENERATION_ROW_SPAN,
    DROP_GENERATION_CONFIRMATION_TOKEN,
    GenerationArchiveEvidence,
    GenerationState,
    drop_eligible_generation,
    mark_drop_eligible,
    mark_generation_state,
    measure_relation_size,
)

# Representative RAW envelope size (~production heap≈382 B/row including JSONB).
_PILOT_PAYLOAD = {
    "symbol": "ETH/USDT-P",
    "topic": "ask_bid_price",
    "bid": "3456.12",
    "ask": "3456.34",
    "bidSize": "1.234",
    "askSize": "2.345",
    "timestamp": 1_720_000_000_000,
    "padding": "x" * 180,
}


@dataclass(frozen=True, slots=True)
class SpanPilotResult:
    row_span: int
    inserted: int
    elapsed_seconds: float
    rows_per_second: float
    heap_bytes: int
    index_bytes: int
    total_bytes: int
    bytes_per_row: float
    wal_bytes_approx: int | None


async def _reset_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS market_events CASCADE"))
        await conn.execute(text("DROP TABLE IF EXISTS market_event_generations CASCADE"))
        await conn.execute(text("DROP SEQUENCE IF EXISTS market_events_id_seq CASCADE"))
        await conn.execute(text("CREATE SEQUENCE market_events_id_seq"))
        await conn.execute(
            text(
                """
                CREATE TABLE market_events (
                    id BIGINT NOT NULL DEFAULT nextval('market_events_id_seq'),
                    received_at TIMESTAMPTZ DEFAULT now() NOT NULL,
                    exchange_at TIMESTAMPTZ,
                    source VARCHAR(32) NOT NULL,
                    event_type VARCHAR(64) NOT NULL,
                    symbol VARCHAR(32) NOT NULL,
                    sequence BIGINT,
                    connection_id VARCHAR(36),
                    local_sequence BIGINT,
                    exchange_sequence BIGINT,
                    schema_version SMALLINT DEFAULT 1 NOT NULL,
                    latency_ms DOUBLE PRECISION,
                    payload JSONB NOT NULL,
                    PRIMARY KEY (id)
                ) PARTITION BY RANGE (id)
                """
            )
        )
        await conn.execute(
            text("ALTER SEQUENCE market_events_id_seq OWNED BY market_events.id")
        )
        await conn.execute(
            text(
                """
                CREATE INDEX ix_market_events_source_sequence
                    ON market_events (source, sequence)
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE INDEX ix_market_events_symbol_exchange_at
                    ON market_events (symbol, exchange_at)
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE INDEX ix_market_events_type_received_at
                    ON market_events (event_type, received_at)
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE market_event_generations (
                    generation_key TEXT PRIMARY KEY,
                    partition_name TEXT NOT NULL UNIQUE,
                    id_start BIGINT NOT NULL,
                    id_end BIGINT NOT NULL,
                    state TEXT NOT NULL,
                    row_span BIGINT NOT NULL,
                    physical_bytes_at_close BIGINT,
                    archive_evidence_sha256 TEXT,
                    closed_at TIMESTAMPTZ,
                    verified_at TIMESTAMPTZ,
                    drop_eligible_at TIMESTAMPTZ,
                    dropped_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )


async def _create_generation(
    engine: AsyncEngine,
    *,
    id_start: int,
    row_span: int,
) -> str:
    id_end = id_start + row_span
    partition_name = f"market_events_g_{id_start}"
    generation_key = f"g_{id_start}_{id_end}"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"""
                CREATE TABLE {partition_name}
                PARTITION OF market_events
                FOR VALUES FROM ({id_start}) TO ({id_end})
                """
            )
        )
        await conn.execute(
            text(
                """
                INSERT INTO market_event_generations (
                    generation_key, partition_name, id_start, id_end, state, row_span
                ) VALUES (
                    :generation_key, :partition_name, :id_start, :id_end, 'ACTIVE', :row_span
                )
                """
            ),
            {
                "generation_key": generation_key,
                "partition_name": partition_name,
                "id_start": id_start,
                "id_end": id_end,
                "row_span": row_span,
            },
        )
    return partition_name


async def _insert_rows(engine: AsyncEngine, count: int, batch: int = 2000) -> float:
    payload = json.dumps(_PILOT_PAYLOAD)
    started = time.perf_counter()
    remaining = count
    async with engine.begin() as conn:
        while remaining > 0:
            n = min(batch, remaining)
            await conn.execute(
                text(
                    """
                    INSERT INTO market_events (
                        source, event_type, symbol, sequence, connection_id,
                        local_sequence, exchange_sequence, schema_version,
                        latency_ms, payload
                    )
                    SELECT
                        'pilot', 'ask_bid_price', 'ETH/USDT-P', g,
                        '00000000-0000-4000-8000-000000000001', g, g, 2,
                        1.5, CAST(:payload AS jsonb)
                    FROM generate_series(1, :n) AS g
                    """
                ),
                {"payload": payload, "n": n},
            )
            remaining -= n
    return time.perf_counter() - started


async def _wal_bytes(engine: AsyncEngine) -> int | None:
    async with engine.connect() as conn:
        try:
            value = await conn.scalar(text("SELECT pg_current_wal_lsn() - '0/0'"))
            return int(value) if value is not None else None
        except Exception:
            return None


async def run_span_pilot(engine: AsyncEngine, row_span: int) -> SpanPilotResult:
    await _reset_schema(engine)
    await _create_generation(engine, id_start=1, row_span=row_span)
    wal_before = await _wal_bytes(engine)
    elapsed = await _insert_rows(engine, row_span)
    wal_after = await _wal_bytes(engine)
    async with engine.connect() as conn:
        size = await measure_relation_size(conn, "market_events_g_1")
    wal_delta = None
    if wal_before is not None and wal_after is not None and wal_after >= wal_before:
        wal_delta = wal_after - wal_before
    return SpanPilotResult(
        row_span=row_span,
        inserted=row_span,
        elapsed_seconds=elapsed,
        rows_per_second=row_span / elapsed if elapsed > 0 else 0.0,
        heap_bytes=size.heap_bytes,
        index_bytes=size.index_bytes,
        total_bytes=size.total_bytes,
        bytes_per_row=size.total_bytes / row_span if row_span else 0.0,
        wal_bytes_approx=wal_delta,
    )


async def run_delete_vs_drop(engine: AsyncEngine, row_span: int = 20_000) -> dict[str, Any]:
    """Compare ordinary DELETE vs DROP partition relation reclamation."""

    await _reset_schema(engine)
    # DELETE path on a dedicated partition.
    await _create_generation(engine, id_start=1, row_span=row_span)
    await _insert_rows(engine, row_span)
    async with engine.connect() as conn:
        before_delete = await measure_relation_size(conn, "market_events_g_1")
        parent_before_delete = await measure_relation_size(conn, "market_events")
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM market_events"))
    async with engine.connect() as conn:
        after_delete = await measure_relation_size(conn, "market_events_g_1")
        parent_after_delete = await measure_relation_size(conn, "market_events")
        row_count = await conn.scalar(text("SELECT COUNT(*) FROM market_events"))

    # DROP path on a fresh partition.
    await _reset_schema(engine)
    partition_name = await _create_generation(engine, id_start=1, row_span=row_span)
    await _insert_rows(engine, row_span)
    async with engine.begin() as conn:
        key = f"g_1_{1 + row_span}"
        await mark_generation_state(conn, key, GenerationState.CLOSED_UNARCHIVED)
        await mark_generation_state(conn, key, GenerationState.VERIFIED)
        evidence = GenerationArchiveEvidence(
            generation_key=key,
            min_raw_event_id=1,
            max_raw_event_id=row_span,
            expected_row_count=row_span,
            observed_row_count=row_span,
            checksums_pass=True,
            manifest_pass=True,
            remote_completed=True,
            download_verification_pass=True,
            storage_reconciliation_pass=True,
            id_coverage_contiguous=True,
            evidence_sha256="pilot",
        )
        await mark_drop_eligible(conn, evidence.generation_key, evidence)
        before_drop = await measure_relation_size(conn, partition_name)
        parent_before_drop = await measure_relation_size(conn, "market_events")
        await drop_eligible_generation(
            conn,
            evidence.generation_key,
            confirmation_token=DROP_GENERATION_CONFIRMATION_TOKEN,
            operator_approved=True,
        )
        parent_after_drop = await measure_relation_size(conn, "market_events")
        partition_regclass = await conn.scalar(
            text("SELECT to_regclass(:rel)"),
            {"rel": f"public.{partition_name}"},
        )

    return {
        "row_span": row_span,
        "delete": {
            "rows_after": int(row_count or 0),
            "partition_total_before": before_delete.total_bytes,
            "partition_total_after": after_delete.total_bytes,
            "parent_total_before": parent_before_delete.total_bytes,
            "parent_total_after": parent_after_delete.total_bytes,
            "reclaimed_bytes_estimate": max(
                0, before_delete.total_bytes - after_delete.total_bytes
            ),
            "notes": (
                "Ordinary DELETE typically leaves most relation file allocation; "
                "pages become reusable but filesystem free space often unchanged."
            ),
        },
        "drop": {
            "partition_total_before": before_drop.total_bytes,
            "partition_regclass_after": partition_regclass,
            "parent_total_before": parent_before_drop.total_bytes,
            "parent_total_after": parent_after_drop.total_bytes,
            "reclaimed_bytes_estimate": max(
                0, parent_before_drop.total_bytes - parent_after_drop.total_bytes
            ),
            "notes": "DROP removes heap/index files for the partition relation.",
        },
        "measured_at_utc": datetime.now(UTC).isoformat(),
    }


async def _async_main(database_url: str, spans: list[int]) -> dict[str, Any]:
    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    try:
        span_results = [asdict(await run_span_pilot(engine, span)) for span in spans]
        delete_vs_drop = await run_delete_vs_drop(engine)
        # Calibrate recommended span from observed bytes/row toward ~250 MiB.
        bytes_per_row = span_results[-1]["bytes_per_row"] if span_results else 533.0
        if bytes_per_row:
            recommended = int((250 * 1024 * 1024) / bytes_per_row)
        else:
            recommended = DEFAULT_GENERATION_ROW_SPAN
        # Snap to nearest 50k for operator clarity.
        recommended = max(50_000, int(round(recommended / 50_000) * 50_000))
        return {
            "production_calibration": {
                "rows": 1_264_007,
                "total_relation_mib_observed": [612, 642],
                "approx_bytes_per_row": 533,
            },
            "span_pilots": span_results,
            "delete_vs_drop": delete_vs_drop,
            "recommended_generation_row_span": recommended,
            "default_in_code": DEFAULT_GENERATION_ROW_SPAN,
        }
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        required=True,
        help="Disposable PostgreSQL URL (must be a test database).",
    )
    parser.add_argument(
        "--spans",
        default="250000,400000,500000",
        help="Comma-separated generation row spans to measure.",
    )
    args = parser.parse_args()
    if "test" not in args.database_url.replace("-", "_").split("/")[-1].split("?")[0]:
        raise SystemExit("refusing non-test database name in --database-url")
    spans = [int(part.strip()) for part in args.spans.split(",") if part.strip()]
    report = asyncio.run(_async_main(args.database_url, spans))
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
