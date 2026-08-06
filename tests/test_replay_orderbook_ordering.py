import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from tests.test_normalization_parsers import payload, raw
from trading_bot.normalization.orderbook import OrderBookReconstructor
from trading_bot.normalization.parsers import OrderBookRecord, parse_market_event
from trading_bot.research.replay import (
    order_events_for_replay,
    orderbook_replay_rows,
    replay_parquet,
)
from trading_bot.storage.models import MarketEvent

START = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)


def _legacy_exchange_first_order_key(row: dict[str, Any]) -> tuple[Any, Any, int]:
    received_at = row["received_at"]
    row_id = row.get("raw_event_id")
    if row_id is None:
        row_id = row.get("id")
    assert row_id is not None
    return (
        row["exchange_at"] or received_at,
        received_at,
        int(row_id),
    )


def _parquet_row(
    *,
    raw_event_id: int,
    received_at: datetime,
    exchange_at: datetime | None,
    exchange_sequence: int | None = None,
    sequence: int | None = None,
    topic: str = "orderbook",
    payload_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = payload_body or payload("orderbook_update")
    return {
        "raw_event_id": raw_event_id,
        "id": raw_event_id,
        "source": "hibachi_ws",
        "topic": topic,
        "symbol": "ETH/USDT-P",
        "exchange_at": exchange_at,
        "sequence": sequence,
        "connection_id": "00000000-0000-0000-0000-000000000001",
        "local_sequence": raw_event_id,
        "exchange_sequence": exchange_sequence,
        "raw_schema_version": 2,
        "received_at": received_at,
        "latency_ms": 0.0,
        "payload_json": json.dumps(body, separators=(",", ":"), sort_keys=True),
    }


def _write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    schema = pa.schema(
        [
            ("raw_event_id", pa.int64()),
            ("id", pa.int64()),
            ("source", pa.string()),
            ("topic", pa.string()),
            ("symbol", pa.string()),
            ("exchange_at", pa.timestamp("us", tz="UTC")),
            ("sequence", pa.int64()),
            ("connection_id", pa.string()),
            ("local_sequence", pa.int64()),
            ("exchange_sequence", pa.int64()),
            ("raw_schema_version", pa.int16()),
            ("received_at", pa.timestamp("us", tz="UTC")),
            ("latency_ms", pa.float64()),
            ("payload_json", pa.string()),
        ]
    )
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


def _remove_best_bid_update(raw_id: int) -> MarketEvent:
    body = payload("orderbook_update")
    body = json.loads(json.dumps(body))
    body["data"]["bid"]["levels"] = [{"price": "1999.50", "quantity": "0"}]
    event = raw("orderbook_update", raw_id=raw_id)
    event.payload = body
    event.local_sequence = raw_id
    event.exchange_sequence = None
    return event


def _row_from_event(event: MarketEvent) -> dict[str, Any]:
    return {
        "raw_event_id": event.id,
        "id": event.id,
        "source": event.source,
        "topic": event.event_type,
        "symbol": event.symbol,
        "exchange_at": event.exchange_at,
        "sequence": event.sequence,
        "connection_id": event.connection_id,
        "local_sequence": event.local_sequence,
        "exchange_sequence": event.exchange_sequence,
        "raw_schema_version": event.schema_version or 1,
        "received_at": event.received_at,
        "latency_ms": event.latency_ms,
        "payload_json": json.dumps(
            event.payload, separators=(",", ":"), sort_keys=True
        ),
    }


def _apply_rows(
    rows: list[dict[str, Any]],
    *,
    preserve_local_sequence: bool = False,
) -> tuple[Decimal | None, Decimal | None]:
    reconstructor = OrderBookReconstructor()
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    for row in rows:
        local_sequence = row.get("local_sequence") if preserve_local_sequence else None
        event = MarketEvent(
            id=row["raw_event_id"],
            received_at=row["received_at"],
            exchange_at=row["exchange_at"],
            source=row["source"],
            event_type=row["topic"],
            symbol=row["symbol"],
            sequence=row.get("sequence"),
            connection_id=row.get("connection_id"),
            local_sequence=local_sequence,
            exchange_sequence=row.get("exchange_sequence"),
            schema_version=row.get("raw_schema_version", 2),
            latency_ms=row.get("latency_ms"),
            payload=json.loads(row["payload_json"]),
        )
        parsed = parse_market_event(event)
        assert isinstance(parsed, OrderBookRecord)
        result = reconstructor.apply(parsed)
        best_bid = result.best_bid
        best_ask = result.best_ask
    return best_bid, best_ask


def test_known_pair_preserves_receipt_order_when_sequence_absent() -> None:
    first_received = START
    second_received = START + timedelta(milliseconds=310)
    first_exchange = START
    second_exchange = START - timedelta(milliseconds=310)
    rows = [
        _parquet_row(
            raw_event_id=1_126_466,
            received_at=first_received,
            exchange_at=first_exchange,
        ),
        _parquet_row(
            raw_event_id=1_126_478,
            received_at=second_received,
            exchange_at=second_exchange,
        ),
    ]
    ordered = order_events_for_replay(rows)
    assert [row["raw_event_id"] for row in ordered] == [1_126_466, 1_126_478]

    legacy = sorted(rows, key=_legacy_exchange_first_order_key)
    assert [row["raw_event_id"] for row in legacy] == [1_126_478, 1_126_466]


def test_replay_parquet_uses_receipt_order_for_known_pair(tmp_path: Path) -> None:
    rows = [
        _parquet_row(
            raw_event_id=1_126_466,
            received_at=START,
            exchange_at=START,
        ),
        _parquet_row(
            raw_event_id=1_126_478,
            received_at=START + timedelta(milliseconds=310),
            exchange_at=START - timedelta(milliseconds=310),
        ),
    ]
    path = tmp_path / "events.parquet"
    _write_parquet(path, rows)
    seen: list[int] = []
    replay_parquet(path, lambda row: seen.append(int(row["raw_event_id"])) or None)
    assert seen == [1_126_466, 1_126_478]


def test_orderbook_replay_rows_use_receipt_order_for_reconstruction() -> None:
    snapshot = _row_from_event(raw("orderbook_snapshot", raw_id=1))
    first_update = _row_from_event(raw("orderbook_update", raw_id=2))
    second_update = _row_from_event(_remove_best_bid_update(raw_id=3))
    first_update["received_at"] = START + timedelta(seconds=1)
    first_update["exchange_at"] = START + timedelta(seconds=2)
    second_update["received_at"] = START + timedelta(seconds=2)
    second_update["exchange_at"] = START + timedelta(seconds=1)
    rows = [snapshot, first_update, second_update]

    receipt_best_bid, receipt_best_ask = _apply_rows(orderbook_replay_rows(rows))
    exchange_swapped = sorted(
        [first_update, second_update],
        key=_legacy_exchange_first_order_key,
    )
    exchange_best_bid, exchange_best_ask = _apply_rows(
        [snapshot, *exchange_swapped]
    )

    assert receipt_best_bid == Decimal("1999.00")
    assert receipt_best_ask == Decimal("2000.50")
    assert exchange_best_bid == Decimal("1999.50")
    assert exchange_best_ask == Decimal("2000.50")
    assert (receipt_best_bid, receipt_best_ask) != (exchange_best_bid, exchange_best_ask)


def test_orderbook_replay_rows_order_by_exchange_sequence_when_present() -> None:
    rows = [
        _parquet_row(
            raw_event_id=20,
            received_at=START + timedelta(seconds=2),
            exchange_at=START,
            exchange_sequence=2,
        ),
        _parquet_row(
            raw_event_id=10,
            received_at=START + timedelta(seconds=1),
            exchange_at=START,
            exchange_sequence=1,
        ),
    ]
    ordered = orderbook_replay_rows(rows)
    assert [row["raw_event_id"] for row in ordered] == [10, 20]


def test_orderbook_replay_rows_fail_closed_on_mixed_sequence_metadata() -> None:
    rows = [
        _parquet_row(
            raw_event_id=1,
            received_at=START,
            exchange_at=START,
            exchange_sequence=1,
        ),
        _parquet_row(
            raw_event_id=2,
            received_at=START + timedelta(seconds=1),
            exchange_at=START,
            exchange_sequence=None,
        ),
    ]
    try:
        orderbook_replay_rows(rows)
    except ValueError as exc:
        assert "mix present and absent" in str(exc)
    else:
        raise AssertionError("Expected mixed sequence metadata to fail closed")
