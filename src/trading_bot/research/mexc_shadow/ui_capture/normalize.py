"""Normalize a raw UI snapshot into a replay Observation. No inferred prices."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from trading_bot.research.mexc_shadow.safety import assert_no_credential_keys
from trading_bot.research.mexc_shadow.types import Observation
from trading_bot.research.mexc_shadow.ui_capture.catalog import SCHEMA_NAME, SCHEMA_VERSION
from trading_bot.research.mexc_shadow.ui_capture.parse import parse_iso_timestamp
from trading_bot.research.mexc_shadow.ui_capture.schema import (
    CaptureTrigger,
    FieldRecord,
    NormalizedCapture,
    ParseStatus,
    UiRawSnapshot,
)

_PARSE_STATUS: dict[str, ParseStatus] = {
    "ok": "ok",
    "ok_redundant": "ok_redundant",
    "missing": "missing",
    "unparsable": "unparsable",
    "ambiguous": "ambiguous",
}
_TRIGGER: dict[str, CaptureTrigger] = {
    "mutation": "mutation",
    "interval": "interval",
    "manual": "manual",
    "fixture": "fixture",
}


def _parse_status(raw: Any) -> ParseStatus:
    return _PARSE_STATUS.get(str(raw or "missing"), "unparsable")


def _trigger(raw: Any) -> CaptureTrigger:
    text = str(raw or "fixture")
    try:
        return _TRIGGER[text]
    except KeyError as exc:
        raise ValueError(f"unknown capture trigger {text!r}") from exc


def _field_from_mapping(name: str, raw: Mapping[str, Any]) -> FieldRecord:
    status = _parse_status(raw.get("parse_status"))
    return FieldRecord(
        name=name,
        raw_text=None if raw.get("raw_text") is None else str(raw.get("raw_text")),
        value=raw.get("value"),
        selector_id=None if raw.get("selector_id") is None else str(raw.get("selector_id")),
        parse_status=status,
        match_count=int(raw.get("match_count") or 0),
        age_ms=None if raw.get("age_ms") is None else int(raw["age_ms"]),
        unit=None if raw.get("unit") is None else str(raw["unit"]),
        changed_at_monotonic_ms=None
        if raw.get("changed_at_monotonic_ms") is None
        else float(raw["changed_at_monotonic_ms"]),
    )


def snapshot_from_mapping(payload: Mapping[str, Any]) -> UiRawSnapshot:
    assert_no_credential_keys(dict(payload))
    if str(payload.get("schema") or "") != SCHEMA_NAME:
        raise ValueError("not a mexc_ui_raw_snapshot")
    if int(payload.get("schema_version") or 0) != SCHEMA_VERSION:
        raise ValueError("unsupported capture schema_version")
    raw_fields = dict(payload.get("fields") or {})
    fields = {
        name: _field_from_mapping(name, mapping)
        for name, mapping in raw_fields.items()
        if isinstance(mapping, Mapping)
    }
    trigger = _trigger(payload.get("trigger"))
    depth_bids = _levels(payload.get("depth_bids"))
    depth_asks = _levels(payload.get("depth_asks"))
    return UiRawSnapshot(
        schema=SCHEMA_NAME,
        schema_version=SCHEMA_VERSION,
        sequence=int(payload["sequence"]),
        received_at_local=str(payload["received_at_local"]),
        observed_at_local=str(payload.get("observed_at_local") or payload["received_at_local"]),
        monotonic_ms=None
        if payload.get("monotonic_ms") is None
        else float(payload["monotonic_ms"]),
        trigger=trigger,
        selector_catalog_version=str(payload.get("selector_catalog_version") or ""),
        page_host=str(payload.get("page_host") or ""),
        page_path=str(payload.get("page_path") or ""),
        symbol_hint=None if payload.get("symbol_hint") is None else str(payload["symbol_hint"]),
        sample_interval_ms=None
        if payload.get("sample_interval_ms") is None
        else int(payload["sample_interval_ms"]),
        observation_valid=bool(payload.get("observation_valid")),
        invalid_reasons=tuple(str(item) for item in (payload.get("invalid_reasons") or ())),
        changed_fields=tuple(str(item) for item in (payload.get("changed_fields") or ())),
        fields=fields,
        depth_bids=depth_bids,
        depth_asks=depth_asks,
        depth_selector_id=None
        if payload.get("depth_selector_id") is None
        else str(payload["depth_selector_id"]),
        exchange_display_at=None
        if payload.get("exchange_display_at") is None
        else str(payload["exchange_display_at"]),
        capture_id=None if payload.get("capture_id") is None else str(payload["capture_id"]),
    )


def _levels(raw: Any) -> tuple[tuple[float, float], ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list | tuple):
        raise ValueError("depth levels must be a list")
    out: list[tuple[float, float]] = []
    for item in raw:
        if not isinstance(item, list | tuple) or len(item) < 2:
            continue
        price = float(item[0])
        size = float(item[1])
        if price > 0 and size > 0:
            out.append((price, size))
    return tuple(out) or None


def _ok_number(record: FieldRecord | None) -> float | None:
    if record is None or record.parse_status not in {"ok", "ok_redundant"}:
        return None
    if isinstance(record.value, int | float) and not isinstance(record.value, bool):
        number = float(record.value)
        return number if number > 0 else None
    return None


def observation_from_snapshot(snapshot: UiRawSnapshot) -> NormalizedCapture:
    """Map a snapshot to Observation only when executable bid/ask are valid.

    Mid is left None unless the UI supplied a mid field (it does not in v1).
    Shadow PnL must use bid/ask, not a synthesized mid.
    """

    if not snapshot.observation_valid:
        return NormalizedCapture(snapshot, None, "observation_invalid")
    symbol_rec = snapshot.fields.get("symbol")
    symbol = symbol_rec.value if symbol_rec is not None else snapshot.symbol_hint
    if not isinstance(symbol, str) or not symbol:
        return NormalizedCapture(snapshot, None, "missing_symbol")
    bid = _ok_number(snapshot.fields.get("bid"))
    ask = _ok_number(snapshot.fields.get("ask"))
    if bid is None or ask is None:
        return NormalizedCapture(snapshot, None, "missing_bbo")
    if bid >= ask:
        return NormalizedCapture(snapshot, None, "crossed_book")
    received = parse_iso_timestamp(snapshot.received_at_local)
    observed = parse_iso_timestamp(snapshot.observed_at_local or snapshot.exchange_display_at)
    if received is None:
        return NormalizedCapture(snapshot, None, "bad_received_at")
    received_dt = datetime.fromisoformat(received)
    observed_dt = datetime.fromisoformat(observed) if observed else received_dt
    last = _ok_number(snapshot.fields.get("last"))
    mark = _ok_number(snapshot.fields.get("mark"))
    index = _ok_number(snapshot.fields.get("index"))
    bid_size = _ok_number(snapshot.fields.get("bid_size"))
    ask_size = _ok_number(snapshot.fields.get("ask_size"))
    observation = Observation(
        observed_at=observed_dt,
        received_at=received_dt,
        symbol=symbol,
        bid=bid,
        ask=ask,
        source="mexc_ui_capture_v1",
        mid=None,
        last=last,
        mark=mark,
        index=index,
        bid_size=bid_size,
        ask_size=ask_size,
        orderbook_bids=snapshot.depth_bids,
        orderbook_asks=snapshot.depth_asks,
    )
    return NormalizedCapture(snapshot, observation, None)


def is_capture_payload(payload: Mapping[str, Any]) -> bool:
    return str(payload.get("schema") or "") == SCHEMA_NAME
