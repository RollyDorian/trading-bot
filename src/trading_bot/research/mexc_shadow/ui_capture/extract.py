"""Stdlib HTML extractor for fixtures and fail-closed selector tests.

Live pages are observed by the extension. Python never drives a browser.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

from trading_bot.research.mexc_shadow.ui_capture.age import apply_field_ages, clock_ms
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
        text = _own_text(node)
        if not text:
            continue
        for label in ordered:
            if text.lower() == label.lower():
                hits.append(node)
                break
    return hits


def _class_hits(root: _Node, token: str, exclude: list[str] | None = None) -> list[_Node]:
    banned = list(exclude or [])
    hits = []
    for node in _walk(root):
        if _ignored(node):
            continue
        classes = node.attrs.get("class", "")
        if token not in classes:
            continue
        if any(item in classes for item in banned):
            continue
        hits.append(node)
    return hits


def _following_numeric_text(label_node: _Node, *, allow_uncle: bool = False) -> str | None:
    parent = label_node.parent
    if parent is None:
        return None
    start = parent.children.index(label_node)
    for sibling in parent.children[start + 1 :]:
        text = _combined_text(sibling)
        if parse_number(text)[0] is not None:
            return text
    parent_text = _combined_text(parent)
    label = collapse_ws(_combined_text(label_node))
    if parent_text.lower().startswith(label.lower()):
        remainder = parent_text[len(label) :].strip()
        if parse_number(remainder)[0] is not None:
            return remainder
    # Uncle walk is for Funding Rate only. Last Price is a dropdown on live MEXC;
    # using an uncle number would steal nearby prices.
    if not allow_uncle:
        return None
    grand = parent.parent
    if grand is not None:
        gstart = grand.children.index(parent)
        for uncle in grand.children[gstart + 1 :]:
            text = _combined_text(uncle)
            if parse_number(text)[0] is not None:
                return text
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
    if label_nodes:
        raws = []
        for node in label_nodes:
            raw = _following_numeric_text(node, allow_uncle=name == "funding")
            raws.append(raw or "")
        if not all(is_missing_text(raw) for raw in raws):
            return _decode_field(name, spec, label_nodes, f"label:{name}", raws)
    tokens = list(spec.get("class_contains") or [])
    exclude = list(spec.get("class_exclude") or [])
    class_nodes: list[_Node] = []
    for token in tokens:
        class_nodes.extend(_class_hits(root, token, exclude))
    unique_nodes: list[_Node] = []
    seen: set[int] = set()
    for node in class_nodes:
        ident = id(node)
        if ident in seen:
            continue
        seen.add(ident)
        unique_nodes.append(node)
    if unique_nodes:
        raws = [_own_text(node) or _combined_text(node) for node in unique_nodes]
        return _decode_field(name, spec, unique_nodes, f"class:{tokens[0]}", raws)
    if label_nodes:
        return FieldRecord(
            name=name,
            raw_text=raws[0] if label_nodes else None,
            value=None,
            selector_id=f"label:{name}",
            parse_status="missing",
            match_count=len(label_nodes),
        )
    return FieldRecord(
        name=name,
        raw_text=None,
        value=None,
        selector_id=None,
        parse_status="missing",
        match_count=0,
    )


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


def _node_contains(ancestor: _Node, node: _Node) -> bool:
    current: _Node | None = node
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


def _coalesce_book_roots(
    roots: list[_Node], problem_code: str = "ambiguous_orderbook_heading"
) -> tuple[_Node | None, list[str]]:
    """One unique panel, or invalid. Nested headings of the same panel are ok."""

    unique: list[_Node] = []
    for node in roots:
        absorbed = False
        for index, existing in enumerate(unique):
            if existing is node or _node_contains(existing, node):
                absorbed = True
                break
            if _node_contains(node, existing):
                unique[index] = node
                absorbed = True
                break
        if not absorbed:
            unique.append(node)
    if not unique:
        return None, []
    if len(unique) > 1:
        return None, [problem_code]
    return unique[0], []


def _wrapper_side_prices(
    root: _Node, wrap_token: str, price_token: str, problem_code: str
) -> tuple[list[float], list[str]]:
    hits = _class_hits(root, wrap_token)
    if not hits:
        return [], []
    book, problems = _coalesce_book_roots(hits, problem_code)
    if problems or book is None:
        return [], problems
    prices: list[float] = []
    for node in _walk(book):
        if _ignored(node):
            continue
        if price_token not in node.attrs.get("class", ""):
            continue
        price = parse_price(_own_text(node) or _combined_text(node))
        if price is not None:
            prices.append(price)
    return prices, []


def _wrapper_bbo(root: _Node) -> tuple[float | None, float | None, list[str]]:
    """BBO from unique asksWrapper/bidsWrapper. Ambiguous wrappers are invalid."""

    spec = SELECTOR_CATALOG["live_orderbook"]
    ask_wrap = spec.get("asks_class_contains")
    bid_wrap = spec.get("bids_class_contains")
    if not ask_wrap or not bid_wrap:
        return None, None, []
    asks, ask_problems = _wrapper_side_prices(
        root, str(ask_wrap), str(spec["ask_price_class_contains"]), "ambiguous_asks_wrapper"
    )
    bids, bid_problems = _wrapper_side_prices(
        root, str(bid_wrap), str(spec["bid_price_class_contains"]), "ambiguous_bids_wrapper"
    )
    problems = ask_problems + bid_problems
    if problems:
        return None, None, problems
    if not asks or not bids:
        return None, None, []
    best_ask = min(asks)
    best_bid = max(bids)
    if best_bid >= best_ask:
        return None, None, []
    return best_bid, best_ask, []


def _own_text(node: _Node) -> str:
    return collapse_ws(node.text)


def _collect_own_prices(root: _Node) -> list[float]:
    prices: list[float] = []
    for node in _walk(root):
        if _ignored(node):
            continue
        price = parse_price(_own_text(node))
        if price is not None:
            prices.append(price)
    return prices


def _live_orderbook(
    root: _Node, last_value: float | None
) -> tuple[float | None, float | None, list[str]]:
    """BBO from a unique Order Book panel, split by Last Price only — never mark/index."""

    spec = SELECTOR_CATALOG["live_orderbook"]
    headings = _label_matches(root, list(spec.get("heading_labels") or []))
    if not headings:
        return None, None, []
    header_labels = [
        "Fair Price",
        "Mark Price",
        "Index Price",
        "Funding Rate / Countdown",
        "Funding Rate",
    ]
    band = float(spec["price_band_frac"])
    min_side = int(spec["min_side_levels"])
    # Coalesce heading parents first. Distinct panels are invalid, never an arbitrary pick.
    book, problems = _coalesce_book_roots([(heading.parent or heading) for heading in headings])
    if problems or book is None:
        return None, None, problems
    if last_value is None or last_value <= 0:
        return None, None, []
    node: _Node | None = book
    chosen: _Node | None = None
    while node is not None and node.tag not in {"body", "html", "document"}:
        if node is not book and _label_matches(node, header_labels):
            break
        near = [
            price
            for price in _collect_own_prices(node)
            if abs(price - last_value) / last_value <= band
        ]
        asks = [price for price in near if price > last_value]
        bids = [price for price in near if price < last_value]
        if len(asks) >= min_side and len(bids) >= min_side:
            chosen = node
            break
        node = node.parent
    if chosen is None:
        return None, None, []
    near = [
        price
        for price in _collect_own_prices(chosen)
        if abs(price - last_value) / last_value <= band
    ]
    asks = [price for price in near if price > last_value]
    bids = [price for price in near if price < last_value]
    best_ask = min(asks)
    best_bid = max(bids)
    if best_bid >= best_ask:
        return None, None, []
    return best_bid, best_ask, []


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

    last_rec = fields["last"]
    last_raw = last_rec.value
    last_value = (
        float(last_raw)
        if isinstance(last_raw, int | float) and not isinstance(last_raw, bool)
        else None
    )
    if fields["bid"].parse_status == "missing" or fields["ask"].parse_status == "missing":
        wrap_bid, wrap_ask, wrap_problems = _wrapper_bbo(root)
        extra_reasons.extend(wrap_problems)
        if wrap_bid is not None and fields["bid"].parse_status == "missing":
            fields["bid"] = FieldRecord(
                name="bid",
                raw_text=str(wrap_bid),
                value=wrap_bid,
                selector_id="live_asks_bids_wrapper",
                parse_status="ok",
                match_count=1,
            )
        if wrap_ask is not None and fields["ask"].parse_status == "missing":
            fields["ask"] = FieldRecord(
                name="ask",
                raw_text=str(wrap_ask),
                value=wrap_ask,
                selector_id="live_asks_bids_wrapper",
                parse_status="ok",
                match_count=1,
            )
    if fields["bid"].parse_status == "missing" or fields["ask"].parse_status == "missing":
        live_bid, live_ask, live_problems = _live_orderbook(root, last_value)
        extra_reasons.extend(live_problems)
        if live_bid is not None and fields["bid"].parse_status == "missing":
            fields["bid"] = FieldRecord(
                name="bid",
                raw_text=str(live_bid),
                value=live_bid,
                selector_id="live_orderbook_split_by_last",
                parse_status="ok",
                match_count=1,
            )
        if live_ask is not None and fields["ask"].parse_status == "missing":
            fields["ask"] = FieldRecord(
                name="ask",
                raw_text=str(live_ask),
                value=live_ask,
                selector_id="live_orderbook_split_by_last",
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

    aged, changed = apply_field_ages(
        fields,
        now_ms=clock_ms(monotonic_ms, received_at_local),
        page_host=page_host,
        page_path=page_path,
        capture_id=capture_id,
        previous=previous,
        now_has_monotonic=monotonic_ms is not None,
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
