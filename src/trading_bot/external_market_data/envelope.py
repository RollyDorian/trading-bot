"""Append-only RAW envelope for external reference market data."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

EventType = Literal["book_ticker", "agg_trade"]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ExternalRawEnvelope:
    """Canonical canary/spool record. Never invents exchange sequences."""

    venue: str
    instrument: str
    event_type: EventType
    received_at: datetime
    connection_id: str
    local_sequence: int
    schema_version: int
    payload: dict[str, Any]
    # Diagnostic exchange time when present (ms epoch → UTC).
    exchange_at: datetime | None = None
    # bookTicker order-book updateId (field u) — not a trade id.
    book_update_id: int | None = None
    # aggTrade aggregate trade id (field a).
    agg_trade_id: int | None = None
    # First/last underlying trade ids on aggTrade (f/l).
    first_trade_id: int | None = None
    last_trade_id: int | None = None
    stream: str | None = None
    parse_ok: bool = True
    validation_error: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["received_at"] = self.received_at.isoformat()
        raw["exchange_at"] = (
            self.exchange_at.isoformat() if self.exchange_at is not None else None
        )
        return raw

    def to_ndjson_line(self) -> str:
        return json.dumps(self.to_json_dict(), separators=(",", ":"), ensure_ascii=True)
