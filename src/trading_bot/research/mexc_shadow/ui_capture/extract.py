"""Stdlib HTML extractor for fixtures and fail-closed selector tests.

Live pages are observed by the extension. Python never drives a browser.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

from trading_bot.research.mexc_shadow.ui_capture.catalog import (
    CATALOG_VERSION,
    DATA_CAPTURE_ATTR,
    IGNORE_ATTR,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    SELECTOR_CATALOG,
)
from trading_bot.research.mexc_shadow.ui_capture.parse import (
    collapse_ws,
    is_missing_text,
    parse_iso_timestamp,
    parse_number,
    parse_price,
    parse_size,
    parse_symbol,
    symbol_from_futures_path,
)
from trading_bot.research.mexc_shadow.ui_capture.schema import (
    CaptureTrigger,
    FieldRecord,
    ParseStatus,
    UiRawSnapshot,
)

_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class _Node:
    __slots__ = ("tag", "attrs", "text", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.text = ""
        self.children: list[_Node] = []
        self.parent: _Node | None = None


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag.lower(), {key.lower(): (value or "") for key, value in attrs})
        parent = self._stack[-1]
        node.parent = parent
        parent.children.append(node)
        if tag.lower() not in _VOID:
            self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == lowered:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].text += data


def _walk(node: _Node) -> list[_Node]:
    out = [node]
    for child in node.children:
        out.extend(_walk(child))
    return out


def _combined_text(node: _Node) -> str:
    parts = [node.text]
    for child in node.children:
        parts.append(_combined_text(child))
    return collapse_ws(" ".join(parts))


def _ignored(node: _Node) -> bool:
    current: _Node | None = node
    while current is not None:
        if current.attrs.get(IGNORE_ATTR):
            return True
        current = current.parent
    return False


def _attr_matches(root: _Node, field_attr: str) -> list[_Node]:
    hits = []
    for node in _walk(root):
        if _ignored(node):
            continue
        if node.attrs.get(DATA_CAPTURE_ATTR) == field_attr:
            hits.append(node)
    return hits


def _label_matches(root: _Node, labels: list[str]) -> list[_Node]:
    if not labels:
        return []
    ordered = sorted(labels, key=len, reverse=True)
    hits: list[_Node] = []
    for node in _walk(root):
        if _ignored(node):
            continue
        text = collapse_ws(_combined_text(node))
        if not text:
            continue
        for label in ordered:
            if text.lower() == label.lower():
                hits.append(node)
                break
    return hits


def _following_numeric_text(label_node: _Node) -> str | None:
    parent = label_node.parent
    if parent is None:
        return None
    start = parent.children.index(label_node)
    for sibling in parent.children[start + 1 :]:
        text = _combined_text(sibling)
        if text:
            return text
    # Fallback: numeric text inside the same parent after the label.
    parent_text = _combined_text(parent)
    label = collapse_ws(_combined_text(label_node))
    if parent_text.lower().startswith(label.lower()):
        remainder = parent_text[len(label) :].strip()
        return remainder or None
    return None


def _decode_field(
    name: str,
    spec: dict[str, Any],
    nodes: list[_Node],
    selector_id: str,
    raws: list[str],
) -> FieldRecord:
    kind = str(spec["kind"])
    parsed_values: list[float | str] = []
    units: list[str | None] = []
    for raw in raws:
        value: float | str | None
        unit: str | None = None
        if kind == "symbol":
            value = parse_symbol(raw)
        elif kind == "timestamp":
            value = parse_iso_timestamp(raw)
        elif kind == "size":
            value = parse_size(raw)
        elif kind == "number":
            value, unit = parse_number(raw)
        else:
            value = parse_price(raw)
        if value is None:
            return FieldRecord(
                name=name,
                raw_text=raws[0] if raws else None,
                value=None,
                selector_id=selector_id,
                parse_status="unparsable",
                match_count=len(nodes),
                age_ms=None,
                unit=unit,
            )
        parsed_values.append(value)
        units.append(unit)
    unique = {value for value in parsed_values}
    if len(unique) > 1:
        return FieldRecord(
            name=name,
            raw_text=raws[0],
            value=None,
            selector_id=selector_id,
            parse_status="ambiguous",
            match_count=len(nodes),
        )
    status: ParseStatus = "ok_redundant" if len(nodes) > 1 else "ok"
    return FieldRecord(
        name=name,
        raw_text=raws[0],
        value=parsed_values[0],
        selector_id=selector_id,
        parse_status=status,
        match_count=len(nodes),
        unit=units[0],
    )


def _extract_field(root: _Node, name: str, spec: dict[str, Any]) -> FieldRecord:
    attr_nodes = _attr_matches(root, str(spec["data_attr_value"]))
    if attr_nodes:
        raws = [_combined_text(node) or node.attrs.get("data-value", "") for node in attr_nodes]
        return _decode_field(name, spec, attr_nodes, f"data_attr:{name}", raws)
    label_nodes = _label_matches(root, list(spec.get("labels") or []))
    if not label_nodes:
        return FieldRecord(
            name=name,
            raw_text=None,
            value=None,
            selector_id=None,
            parse_status="missing",
            match_count=0,
        )
    raws = []
    for node in label_nodes:
        raw = _following_numeric_text(node)
        raws.append(raw or "")
    if all(is_missing_text(raw) for raw in raws):
        return FieldRecord(
            name=name,
            raw_text=raws[0] if raws else None,
            value=None,
            selector_id=f"label:{name}",
            parse_status="missing",
            match_count=len(label_nodes),
        )
    return _decode_field(name, spec, label_nodes, f"label:{name}", raws)


def _parse_levels(
    root: _Node, side: str, spec: dict[str, Any]
) -> tuple[tuple[float, float], ...] | None:
    attr = str(spec["level_attr"])
    price_attr = str(spec["price_attr"])
    size_attr = str(spec["size_attr"])
    max_levels = int(spec["max_levels"])
    levels: list[tuple[float, float]] = []
    for node in _walk(root):
        if _ignored(node):
            continue
        if node.attrs.get(attr) != side and node.attrs.get(DATA_CAPTURE_ATTR) != side:
            continue
        price = parse_price(node.attrs.get(price_attr) or _combined_text(node))
        size = parse_size(node.attrs.get(size_attr) or node.attrs.get("data-qty"))
        if price is None or size is None:
            continue
        levels.append((price, size))
        if len(levels) >= max_levels:
            break
    return tuple(levels) or None


def _orderbook(root: _Node) -> tuple[
    tuple[tuple[float, float], ...] | None,
    tuple[tuple[float, float], ...] | None,
    str | None,
    list[str],
]:
    spec = SELECTOR_CATALOG["orderbook"]
    roots = _attr_matches(root, str(spec["root_attr_value"]))
    if len(roots) > 1:
        return None, None, "data_attr:orderbook", ["ambiguous_orderbook_root"]
    if not roots:
        return None, None, None, []
    book = roots[0]
    bid_side = str(spec["bids_attr_value"])
    ask_side = str(spec["asks_attr_value"])
    bids = _parse_levels(book, "bid", spec) or _parse_levels(book, bid_side, spec)
    asks = _parse_levels(book, "ask", spec) or _parse_levels(book, ask_side, spec)
    return bids, asks, "data_attr:orderbook", []


def _elapsed_ms(previous_iso: str, current_iso: str) -> int:
    previous = datetime.fromisoformat(previous_iso.replace("Z", "+00:00"))
    current = datetime.fromisoformat(current_iso.replace("Z", "+00:00"))
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    delta = (current - previous).total_seconds() * 1000.0
    return max(0, int(delta))


def extract_html(
    html: str,
    *,
    received_at_local: str,
    sequence: int,
    page_host: str = "fixture.local",
    page_path: str = "/futures/TAO_USDT",
    trigger: CaptureTrigger = "fixture",
    sample_interval_ms: int | None = None,
    previous: UiRawSnapshot | None = None,
    monotonic_ms: float | None = None,
    capture_id: str | None = None,
) -> UiRawSnapshot:
    parser = _TreeParser()
    parser.feed(html)
    parser.close()
    root = parser.root
    fields: dict[str, FieldRecord] = {}
    for name, spec in SELECTOR_CATALOG["fields"].items():
        fields[name] = _extract_field(root, name, spec)
    depth_bids, depth_asks, depth_selector, depth_problems = _orderbook(root)
    hint = symbol_from_futures_path(page_path)
    symbol_field = fields["symbol"]
    if symbol_field.parse_status == "missing" and hint:
        fields["symbol"] = FieldRecord(
            name="symbol",
            raw_text=hint,
            value=hint,
            selector_id="page_path",
            parse_status="ok",
            match_count=1,
        )
    if (
        symbol_field.value
        and hint
        and symbol_field.parse_status in {"ok", "ok_redundant"}
        and symbol_field.value != hint
    ):
        fields["symbol"] = FieldRecord(
            name="symbol",
            raw_text=str(symbol_field.raw_text),
            value=None,
            selector_id=symbol_field.selector_id,
            parse_status="ambiguous",
            match_count=symbol_field.match_count,
        )

    # Visible depth may supply BBO only when explicit bid/ask fields are missing.
    bid_field = fields["bid"]
    ask_field = fields["ask"]
    extra_reasons = list(depth_problems)
    if bid_field.parse_status == "missing" and depth_bids:
        best_bid = max(depth_bids, key=lambda pair: pair[0])
        fields["bid"] = FieldRecord(
            name="bid",
            raw_text=str(best_bid[0]),
            value=best_bid[0],
            selector_id="orderbook_max_bid",
            parse_status="ok",
            match_count=1,
        )
        if fields["bid_size"].parse_status == "missing" and best_bid[1] > 0:
            fields["bid_size"] = FieldRecord(
                name="bid_size",
                raw_text=str(best_bid[1]),
                value=best_bid[1],
                selector_id="orderbook_max_bid",
                parse_status="ok",
                match_count=1,
            )
    if ask_field.parse_status == "missing" and depth_asks:
        best_ask = min(depth_asks, key=lambda pair: pair[0])
        fields["ask"] = FieldRecord(
            name="ask",
            raw_text=str(best_ask[0]),
            value=best_ask[0],
            selector_id="orderbook_min_ask",
            parse_status="ok",
            match_count=1,
        )
        if fields["ask_size"].parse_status == "missing" and best_ask[1] > 0:
            fields["ask_size"] = FieldRecord(
                name="ask_size",
                raw_text=str(best_ask[1]),
                value=best_ask[1],
                selector_id="orderbook_min_ask",
                parse_status="ok",
                match_count=1,
            )

    invalid_reasons = list(extra_reasons)
    for name, spec in SELECTOR_CATALOG["fields"].items():
        rec = fields[name]
        if rec.parse_status == "ambiguous":
            invalid_reasons.append(f"ambiguous:{name}")
        if spec["required_for_valid"] and rec.parse_status not in {"ok", "ok_redundant"}:
            invalid_reasons.append(f"missing_required:{name}")
    bid_value = fields["bid"].value
    ask_value = fields["ask"].value
    if isinstance(bid_value, float) and isinstance(ask_value, float) and bid_value >= ask_value:
        invalid_reasons.append("crossed_book")

    changed: list[str] = []
    aged: dict[str, FieldRecord] = {}
    dt_ms = 0
    if previous is not None:
        dt_ms = _elapsed_ms(previous.received_at_local, received_at_local)
    for name, rec in fields.items():
        age: int | None = None
        if rec.parse_status != "missing":
            age = 0
            if previous is not None and name in previous.fields:
                prev = previous.fields[name]
                if prev.value == rec.value and prev.parse_status == rec.parse_status:
                    age = (prev.age_ms or 0) + dt_ms
                else:
                    changed.append(name)
            else:
                changed.append(name)
        aged[name] = FieldRecord(
            name=rec.name,
            raw_text=rec.raw_text,
            value=rec.value,
            selector_id=rec.selector_id,
            parse_status=rec.parse_status,
            match_count=rec.match_count,
            age_ms=age if rec.parse_status != "missing" else None,
            unit=rec.unit,
        )

    exchange_at = None
    exchange_field = aged.get("exchange_display_at")
    if exchange_field is not None and isinstance(exchange_field.value, str):
        exchange_at = exchange_field.value

    return UiRawSnapshot(
        schema=SCHEMA_NAME,
        schema_version=SCHEMA_VERSION,
        sequence=sequence,
        received_at_local=received_at_local,
        observed_at_local=received_at_local,
        monotonic_ms=monotonic_ms,
        trigger=trigger,
        selector_catalog_version=CATALOG_VERSION,
        page_host=page_host,
        page_path=page_path,
        symbol_hint=hint,
        sample_interval_ms=sample_interval_ms,
        observation_valid=not invalid_reasons,
        invalid_reasons=tuple(invalid_reasons),
        changed_fields=tuple(changed),
        fields=aged,
        depth_bids=depth_bids,
        depth_asks=depth_asks,
        depth_selector_id=depth_selector,
        exchange_display_at=exchange_at,
        capture_id=capture_id,
    )
