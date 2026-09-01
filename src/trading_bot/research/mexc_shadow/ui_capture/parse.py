"""Parse displayed text only. Never invent a number that was not in the text."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

_MISSING_TEXT = frozenset({"", "--", "—", "-", "n/a", "na", "null"})
_FIRST_NUMBER = re.compile(
    r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?"
)


def collapse_ws(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").replace("\u200e", "").split())


def is_missing_text(text: str | None) -> bool:
    if text is None:
        return True
    return collapse_ws(text).lower() in _MISSING_TEXT


def parse_number(text: str | None) -> tuple[float | None, str | None]:
    """Return (value, unit). unit is 'percent' if the text contains '%'."""

    if text is None or is_missing_text(text):
        return None, None
    compact = collapse_ws(text)
    stripped = compact.replace(",", "")
    match = _FIRST_NUMBER.search(stripped)
    if match is None:
        return None, None
    number = float(match.group(0))
    unit = "percent" if "%" in compact else None
    return number, unit


def parse_price(text: str | None) -> float | None:
    number, _unit = parse_number(text)
    if number is None or number <= 0:
        return None
    return number


def parse_size(text: str | None) -> float | None:
    number, _unit = parse_number(text)
    if number is None or number <= 0:
        return None
    return number


def parse_symbol(text: str | None) -> str | None:
    if text is None or is_missing_text(text):
        return None
    compact = collapse_ws(text).upper().replace("-", "").replace("/", "")
    compact = compact.replace(" ", "")
    if "_" in compact:
        compact = compact.replace("_", "")
    if not compact.isalnum() or len(compact) < 6:
        return None
    return compact


def symbol_from_futures_path(path: str) -> str | None:
    # /futures/TAO_USDT → TAOUSDT. Path is displayed routing, not a manufactured price.
    parts = [item for item in path.split("/") if item]
    if len(parts) >= 2 and parts[0] == "futures":
        return parse_symbol(parts[1])
    return None


def parse_iso_timestamp(text: str | None) -> str | None:
    if text is None or is_missing_text(text):
        return None
    compact = collapse_ws(text).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(compact)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def json_ready(value: Any) -> Any:
    if isinstance(value, tuple):
        return [json_ready(item) for item in value]
    return value
