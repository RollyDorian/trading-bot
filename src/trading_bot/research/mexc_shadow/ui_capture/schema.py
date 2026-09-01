"""Capture snapshot types. Missing values stay null; nothing is inferred."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from trading_bot.research.mexc_shadow.types import Observation

ParseStatus = Literal["ok", "ok_redundant", "missing", "unparsable", "ambiguous"]
CaptureTrigger = Literal["mutation", "interval", "manual", "fixture"]


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
