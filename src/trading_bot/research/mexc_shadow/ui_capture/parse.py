"""Parse displayed text only. Never invent a number that was not in the text.

Locale is derived from the futures pathname. Comma is not stripped unconditionally:
ru-RU uses a decimal comma, en-US a decimal point, and unknown/default refuses
ambiguous punctuation instead of guessing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

ParserLocale = Literal["ru-RU", "en-US", "unknown"]

_MISSING_TEXT = frozenset({"", "--", "—", "-", "n/a", "na", "null"})
_LOCALE_PREFIX = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_BIDI_TABLE = str.maketrans({ord(ch): None for ch in "\u200e\u200f\u202a\u202b\u202c\u202d\u202e"})
_SPACE_GROUPING = str.maketrans(
    {
        "\xa0": " ",
        "\u202f": " ",
        "\u2007": " ",
        "\u2009": " ",
        "\u2008": " ",
        "\u200a": " ",
    }
)
_DIGIT_RUN = re.compile(r"\d")
# First numeric token: sign, digits, grouping/decimal punctuation, optional exponent.
_NUMERIC_TOKEN = re.compile(r"[-+]?(?:\d[\d\s.,]*)(?:[eE][-+]?\d+)?")


def collapse_ws(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").replace("\u200e", "").split())


def is_missing_text(text: str | None) -> bool:
    if text is None:
        return True
    return collapse_ws(text).lower() in _MISSING_TEXT


def _path_parts(path: str) -> list[str]:
    return [item.split("?")[0] for item in path.split("/") if item]


def locale_from_pathname(path: str) -> ParserLocale:
    """Map a futures pathname to an explicit parser mode.

    /ru-RU/futures/... → ru-RU
    /en-US/futures/... → en-US
    /futures/... and any other locale prefix → unknown (fail closed on commas)
    """

    parts = _path_parts(path)
    if not parts:
        return "unknown"
    if _LOCALE_PREFIX.fullmatch(parts[0]) and len(parts) >= 2 and parts[1] == "futures":
        prefix = parts[0]
        if prefix == "ru-RU":
            return "ru-RU"
        if prefix == "en-US":
            return "en-US"
        return "unknown"
    return "unknown"


def symbol_from_futures_path(path: str) -> str | None:
    """Recover TAOUSDT from /futures/TAO_USDT and /xx-XX/futures/TAO_USDT only."""

    parts = _path_parts(path)
    if not parts:
        return None
    if parts[0] == "futures" and len(parts) >= 2:
        return parse_symbol(parts[1])
    if (
        _LOCALE_PREFIX.fullmatch(parts[0])
        and len(parts) >= 3
        and parts[1] == "futures"
    ):
        return parse_symbol(parts[2])
    return None


def _strip_bidi(text: str) -> str:
    return text.translate(_BIDI_TABLE)


def _compact_grouping_spaces(text: str) -> str:
    translated = text.translate(_SPACE_GROUPING)
    # Spaces between digits are grouping, not decimals. Other spaces stay as token edges.
    return re.sub(r"(?<=\d)[ ]+(?=\d)", "", translated)


def _split_grouped(body: str, group_char: str) -> list[str] | None:
    parts = body.split(group_char)
    if not parts or any(not part.isdigit() for part in parts):
        return None
    if not parts[0] or len(parts[0]) > 3:
        return None
    for part in parts[1:]:
        if len(part) != 3:
            return None
    return parts


def _interpret_en_us(body: str) -> float | None:
    if "," in body:
        if body.count(".") > 1:
            return None
        if "." in body:
            left, right = body.rsplit(".", 1)
            grouped = _split_grouped(left, ",")
            if grouped is None or not right.isdigit() or not right:
                return None
            return float(f"{''.join(grouped)}.{right}")
        grouped = _split_grouped(body, ",")
        if grouped is None:
            return None
        return float("".join(grouped))
    if body.count(".") > 1:
        return None
    if "." in body:
        left, right = body.split(".", 1)
        if not left.isdigit() or not right.isdigit() or not right:
            return None
        return float(f"{left}.{right}")
    if not body.isdigit():
        return None
    return float(body)


def _interpret_ru_ru(body: str) -> float | None:
    # Decimal comma. Period is thousands grouping only (groups of 3).
    if body.count(",") > 1:
        return None
    if "," in body:
        left, right = body.split(",", 1)
        if not right.isdigit() or not right:
            return None
        if "." in left:
            grouped = _split_grouped(left, ".")
            if grouped is None:
                return None
            return float(f"{''.join(grouped)}.{right}")
        if not left.isdigit():
            return None
        return float(f"{left}.{right}")
    if "." in body:
        grouped = _split_grouped(body, ".")
        if grouped is None:
            # "218.11" on a ru-RU page is not a thousands group. Fail closed.
            return None
        return float("".join(grouped))
    if not body.isdigit():
        return None
    return float(body)


def _interpret_unknown(body: str) -> float | None:
    # Default /futures/ mode: period may be decimal; comma is always ambiguous.
    if "," in body:
        return None
    if body.count(".") > 1:
        return None
    if "." in body:
        left, right = body.split(".", 1)
        if not left.isdigit() or not right.isdigit() or not right:
            return None
        # "1.234" could be 1.234 or 1234. Fail rather than guess grouping.
        if len(right) == 3 and 1 <= len(left) <= 3:
            return None
        return float(f"{left}.{right}")
    if not body.isdigit():
        return None
    return float(body)


def _interpret_body(body: str, locale: ParserLocale) -> float | None:
    if locale == "ru-RU":
        return _interpret_ru_ru(body)
    if locale == "en-US":
        return _interpret_en_us(body)
    return _interpret_unknown(body)


def _coerce_locale(locale: str | None) -> ParserLocale:
    if locale == "ru-RU":
        return "ru-RU"
    if locale == "en-US":
        return "en-US"
    return "unknown"


def parse_number(
    text: str | None, locale: str | None = "unknown"
) -> tuple[float | None, str | None]:
    """Return (value, unit). unit is 'percent' if the text contains '%'."""

    if text is None or is_missing_text(text):
        return None, None
    mode = _coerce_locale(locale)
    compact = collapse_ws(_strip_bidi(text))
    unit = "percent" if "%" in compact else None
    match = _NUMERIC_TOKEN.search(compact)
    if match is None:
        return None, None
    token = match.group(0).strip()
    sign = 1.0
    if token[:1] in "+-":
        sign = -1.0 if token[0] == "-" else 1.0
        token = token[1:]
    exponent = 0
    exp_match = re.search(r"[eE]([+-]?\d+)$", token)
    if exp_match:
        exponent = int(exp_match.group(1))
        token = token[: exp_match.start()]
    body = _compact_grouping_spaces(token).replace(" ", "")
    if not body or not _DIGIT_RUN.search(body):
        return None, None
    number = _interpret_body(body, mode)
    if number is None:
        return None, None
    value = sign * number
    if exponent:
        value *= 10.0**exponent
    if value != value:
        return None, None
    return value, unit


def parse_price(text: str | None, locale: str | None = "unknown") -> float | None:
    number, unit = parse_number(text, locale)
    # Prices are never percents; funding uses parse_number directly.
    if number is None or unit == "percent" or number <= 0:
        return None
    return number


def parse_size(text: str | None, locale: str | None = "unknown") -> float | None:
    number, unit = parse_number(text, locale)
    if number is None or unit == "percent" or number <= 0:
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


def join_price_tokens(tokens: list[str] | tuple[str, ...]) -> str | None:
    """Join nested-span price tokens. Adjacent digit runs without a separator fail closed."""

    pieces: list[str] = []
    for raw in tokens:
        piece = _strip_bidi(raw).strip()
        if not piece:
            continue
        pieces.append(piece)
    if not pieces:
        return None
    digitish = re.compile(r"^[+-]?\d+$")
    for left, right in zip(pieces, pieces[1:], strict=False):
        if digitish.fullmatch(left) and digitish.fullmatch(right):
            return None
    return "".join(pieces)
