"""Capture snapshot types. Missing values stay null; nothing is inferred."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from trading_bot.research.mexc_shadow.types import Observation

ParseStatus = Literal["ok", "ok_redundant", "missing", "unparsable", "ambiguous"]
CaptureTrigger = Literal["mutation", "interval", "manual", "fixture"]

# Bounded live-BBO diagnostics. Never store HTML, tickets, or credentials.
ORDERBOOK_DIAGNOSTIC_INT_KEYS = (
    "orderbook_heading_count",
    "visible_orderbook_heading_count",
    "asks_wrapper_count",
    "visible_asks_wrapper_count",
    "bids_wrapper_count",
    "visible_bids_wrapper_count",
)
_ALLOWED_BBO_SOURCES = frozenset(
    {
        "none",
        "data_attr",
        "data_attr:orderbook",
        "live_asks_bids_wrapper",
        "live_orderbook_heading_fallback",
    }
)


def empty_orderbook_diagnostics() -> dict[str, Any]:
    return {
        "orderbook_heading_count": 0,
        "visible_orderbook_heading_count": 0,
        "asks_wrapper_count": 0,
        "visible_asks_wrapper_count": 0,
        "bids_wrapper_count": 0,
        "visible_bids_wrapper_count": 0,
        "chosen_bbo_source": "none",
        "ambiguity_reason": None,
    }


def sanitize_orderbook_diagnostics(raw: Any) -> dict[str, Any]:
    """Keep only the declared integer/string diagnostic keys. Drop anything else."""

    out = empty_orderbook_diagnostics()
    if not isinstance(raw, Mapping):
        return out
    for key in ORDERBOOK_DIAGNOSTIC_INT_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        try:
            out[key] = int(value)
        except (TypeError, ValueError):
            continue
    source = raw.get("chosen_bbo_source")
    if source is None or source == "":
        out["chosen_bbo_source"] = "none"
    else:
        text = str(source)
        out["chosen_bbo_source"] = text if text in _ALLOWED_BBO_SOURCES else "none"
    reason = raw.get("ambiguity_reason")
    out["ambiguity_reason"] = None if reason in {None, ""} else str(reason)
    return out


@dataclass(frozen=True, slots=True)
class FieldRecord:
    name: str
    raw_text: str | None
    value: float | str | None
    selector_id: str | None
    parse_status: ParseStatus
    match_count: int
    age_ms: int | None = None
    unit: str | None = None
    # Monotonic (or fixture clock) time of the last value change for this field.
    changed_at_monotonic_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "raw_text": self.raw_text,
            "value": self.value,
            "selector_id": self.selector_id,
            "parse_status": self.parse_status,
            "match_count": self.match_count,
            "age_ms": self.age_ms,
            "unit": self.unit,
            "changed_at_monotonic_ms": self.changed_at_monotonic_ms,
        }


@dataclass(frozen=True, slots=True)
class UiRawSnapshot:
    schema: str
    schema_version: int
    sequence: int
    received_at_local: str
    observed_at_local: str
    monotonic_ms: float | None
    trigger: CaptureTrigger
    selector_catalog_version: str
    page_host: str
    page_path: str
    symbol_hint: str | None
    sample_interval_ms: int | None
    observation_valid: bool
    invalid_reasons: tuple[str, ...]
    changed_fields: tuple[str, ...]
    fields: dict[str, FieldRecord]
    depth_bids: tuple[tuple[float, float], ...] | None
    depth_asks: tuple[tuple[float, float], ...] | None
    depth_selector_id: str | None
    exchange_display_at: str | None
    capture_id: str | None = None
    orderbook_diagnostics: dict[str, Any] = field(default_factory=empty_orderbook_diagnostics)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "capture_id": self.capture_id,
            "sequence": self.sequence,
            "received_at_local": self.received_at_local,
            "observed_at_local": self.observed_at_local,
            "monotonic_ms": self.monotonic_ms,
            "exchange_display_at": self.exchange_display_at,
            "trigger": self.trigger,
            "selector_catalog_version": self.selector_catalog_version,
            "page_host": self.page_host,
            "page_path": self.page_path,
            "symbol_hint": self.symbol_hint,
            "sample_interval_ms": self.sample_interval_ms,
            "observation_valid": self.observation_valid,
            "invalid_reasons": list(self.invalid_reasons),
            "changed_fields": list(self.changed_fields),
            "fields": {name: rec.as_dict() for name, rec in self.fields.items()},
            "depth_bids": [list(level) for level in self.depth_bids]
            if self.depth_bids is not None
            else None,
            "depth_asks": [list(level) for level in self.depth_asks]
            if self.depth_asks is not None
            else None,
            "depth_selector_id": self.depth_selector_id,
            "orderbook_diagnostics": sanitize_orderbook_diagnostics(self.orderbook_diagnostics),
        }


@dataclass(frozen=True, slots=True)
class NormalizedCapture:
    snapshot: UiRawSnapshot
    observation: Observation | None
    skipped_reason: str | None = None


@dataclass
class CaptureQualityReport:
    n_raw: int = 0
    n_valid_for_replay: int = 0
    n_invalid: int = 0
    invalid_reasons: dict[str, int] = field(default_factory=dict)
    missingness: dict[str, int] = field(default_factory=dict)
    parse_status_counts: dict[str, int] = field(default_factory=dict)
    interarrival_ms: dict[str, float | int | None] = field(default_factory=dict)
    coexistence_bid_ask_mark_index: int = 0
    replay_determinism_sha256: str | None = None
    notes: tuple[str, ...] = ()
    capture_id: str | None = None
    duration_ms: float | None = None
    trigger_counts: dict[str, int] = field(default_factory=dict)
    field_change_counts: dict[str, int] = field(default_factory=dict)
    field_change_rate: dict[str, float] = field(default_factory=dict)
    field_age_ms: dict[str, dict[str, float | int | None]] = field(default_factory=dict)
    n_bid_ge_ask: int = 0
    n_simultaneous_bid_ask_mark_index: int = 0
    sequence_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    session: dict[str, Any] | None = None
    sessions: list[dict[str, Any]] = field(default_factory=list)
    n_sessions: int = 0
    n_chunks_total: int = 0
    timing_adequacy: str = "UNKNOWN"
