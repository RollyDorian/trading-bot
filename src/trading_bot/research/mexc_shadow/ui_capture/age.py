"""Per-field age from last observed value change.

Age is wall-independent: ``age_ms = now_monotonic - last_change_monotonic``.
Mutation bursts that reprint the same value must not add the sample interval.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from trading_bot.research.mexc_shadow.ui_capture.schema import FieldRecord, UiRawSnapshot

_VALUE_STATUSES = frozenset({"ok", "ok_redundant"})


def clock_ms(monotonic_ms: float | None, received_at_local: str) -> float:
    """Prefer the caller-supplied monotonic clock; fixtures may use received_at."""

    if monotonic_ms is not None:
        return float(monotonic_ms)
    parsed = datetime.fromisoformat(received_at_local.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp() * 1000.0


def page_age_key(page_host: str, page_path: str, symbol: str | None) -> str:
    return f"{page_host}|{page_path}|{symbol or ''}"


def _symbol_value(fields: dict[str, FieldRecord]) -> str | None:
    rec = fields.get("symbol")
    if rec is None or rec.parse_status not in _VALUE_STATUSES:
        return None
    return str(rec.value) if rec.value is not None else None


def _has_stable_value(rec: FieldRecord) -> bool:
    return rec.parse_status in _VALUE_STATUSES and rec.value is not None


def _clocks_compatible(previous: UiRawSnapshot, now_has_monotonic: bool) -> bool:
    prev_has = previous.monotonic_ms is not None
    return prev_has == now_has_monotonic


def apply_field_ages(
    fields: dict[str, FieldRecord],
    *,
    now_ms: float,
    page_host: str,
    page_path: str,
    capture_id: str | None,
    previous: UiRawSnapshot | None,
    now_has_monotonic: bool,
) -> tuple[dict[str, FieldRecord], tuple[str, ...]]:
    """Stamp ``age_ms`` / ``changed_at_monotonic_ms`` and list value-change names.

    The age clock resets when the page/symbol changes, the capture id changes,
    the monotonic/wall clock family changes, or a field goes missing then valid.
    """

    reset = previous is None
    if previous is not None:
        prev_key = page_age_key(
            previous.page_host, previous.page_path, _symbol_value(previous.fields)
        )
        now_key = page_age_key(page_host, page_path, _symbol_value(fields))
        if (
            prev_key != now_key
            or previous.capture_id != capture_id
            or not _clocks_compatible(previous, now_has_monotonic)
        ):
            reset = True

    changed: list[str] = []
    aged: dict[str, FieldRecord] = {}
    for name, rec in fields.items():
        if not _has_stable_value(rec):
            aged[name] = replace(rec, age_ms=None, changed_at_monotonic_ms=None)
            continue
        prev = None if reset else previous.fields.get(name) if previous is not None else None
        if prev is None or not _has_stable_value(prev) or prev.value != rec.value:
            changed.append(name)
            aged[name] = replace(rec, age_ms=0, changed_at_monotonic_ms=now_ms)
            continue
        changed_at = prev.changed_at_monotonic_ms
        if changed_at is None:
            changed.append(name)
            aged[name] = replace(rec, age_ms=0, changed_at_monotonic_ms=now_ms)
            continue
        age = max(0, int(round(now_ms - changed_at)))
        aged[name] = replace(rec, age_ms=age, changed_at_monotonic_ms=changed_at)
    return aged, tuple(changed)
