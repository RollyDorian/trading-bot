"""Cheap bounded metrics for the external-ref canary."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class ExternalMetrics:
    started_monotonic: float = field(default_factory=time.monotonic)
    messages_total: int = 0
    book_ticker_count: int = 0
    agg_trade_count: int = 0
    malformed_count: int = 0
    reconnect_count: int = 0
    bytes_observed: int = 0
    latest_received_at: datetime | None = None
    latest_exchange_at: datetime | None = None
    exchange_receive_deltas_ms: list[float] = field(default_factory=list)
    negative_delta_count: int = 0
    stop_reason: str | None = None
    max_local_sequence: dict[str, int] = field(default_factory=dict)

    def note_message(
        self,
        *,
        event_type: str,
        raw_bytes: int,
        received_at: datetime,
        exchange_at: datetime | None,
        connection_id: str,
        local_sequence: int,
    ) -> None:
        self.messages_total += 1
        self.bytes_observed += raw_bytes
        self.latest_received_at = received_at
        self.max_local_sequence[connection_id] = local_sequence
        if event_type == "book_ticker":
            self.book_ticker_count += 1
        elif event_type == "agg_trade":
            self.agg_trade_count += 1
        if exchange_at is not None:
            self.latest_exchange_at = exchange_at
            delta_ms = (received_at - exchange_at).total_seconds() * 1000.0
            # Bound memory: 2k samples is enough for canary percentiles (~16 MiB RSS budget).
            if len(self.exchange_receive_deltas_ms) < 2_048:
                self.exchange_receive_deltas_ms.append(delta_ms)
            if delta_ms < 0:
                self.negative_delta_count += 1

    def elapsed_seconds(self) -> float:
        return max(time.monotonic() - self.started_monotonic, 1e-9)

    def snapshot(self, *, spool_bytes: int, filesystem_free: int) -> dict[str, Any]:
        elapsed = self.elapsed_seconds()
        deltas = sorted(self.exchange_receive_deltas_ms)
        return {
            "elapsed_seconds": elapsed,
            "messages_total": self.messages_total,
            "book_ticker_count": self.book_ticker_count,
            "agg_trade_count": self.agg_trade_count,
            "malformed_count": self.malformed_count,
            "reconnect_count": self.reconnect_count,
            "bytes_observed": self.bytes_observed,
            "messages_per_sec": self.messages_total / elapsed,
            "bytes_per_sec": self.bytes_observed / elapsed,
            "spool_bytes": spool_bytes,
            "bytes_per_event": (
                (spool_bytes / self.messages_total) if self.messages_total else None
            ),
            "projected_mib_per_hour": (
                (spool_bytes / elapsed) * 3600.0 / (1024.0 * 1024.0)
            ),
            "projected_gib_per_day": (
                (spool_bytes / elapsed) * 86400.0 / (1024.0**3)
            ),
            "latest_received_at": (
                self.latest_received_at.isoformat() if self.latest_received_at else None
            ),
            "latest_exchange_at": (
                self.latest_exchange_at.isoformat() if self.latest_exchange_at else None
            ),
            "exchange_receive_delta_ms": _percentile_summary(deltas),
            "negative_delta_count": self.negative_delta_count,
            "max_local_sequence_by_connection": dict(self.max_local_sequence),
            "filesystem_free_bytes": filesystem_free,
            "stop_reason": self.stop_reason,
        }


def _percentile_summary(sorted_vals: list[float]) -> dict[str, float | int | None]:
    if not sorted_vals:
        return {
            "n": 0,
            "min": None,
            "max": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
        }

    def pct(q: float) -> float:
        pos = (len(sorted_vals) - 1) * q
        lo = math.floor(pos)
        hi = math.ceil(pos)
        if lo == hi:
            return sorted_vals[lo]
        weight = pos - lo
        return sorted_vals[lo] * (1.0 - weight) + sorted_vals[hi] * weight

    return {
        "n": len(sorted_vals),
        "min": sorted_vals[0],
        "max": sorted_vals[-1],
        "p50": pct(0.50),
        "p90": pct(0.90),
        "p95": pct(0.95),
        "p99": pct(0.99),
    }
