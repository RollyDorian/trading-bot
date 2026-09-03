"""Stdlib HTML extractor for fixtures and fail-closed selector tests.

Live pages are observed by the extension. Python never drives a browser.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
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
    join_price_tokens,
    locale_from_pathname,
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
    empty_header_diagnostics,
    empty_orderbook_diagnostics,
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


@dataclass(frozen=True, slots=True)
class _PriceHit:
    value: float
    raw_text: str
    tokens: tuple[str, ...]


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


def _following_numeric_text(
    label_node: _Node, *, allow_uncle: bool = False, locale: str = "unknown"
) -> str | None:
    parent = label_node.parent
    if parent is None:
        return None
    start = parent.children.index(label_node)
    for sibling in parent.children[start + 1 :]:
        text = _combined_text(sibling)
        if parse_number(text, locale)[0] is not None:
            return text
    parent_text = _combined_text(parent)
    label = collapse_ws(_combined_text(label_node))
    if parent_text.lower().startswith(label.lower()):
        remainder = parent_text[len(label) :].strip()
        if parse_number(remainder, locale)[0] is not None:
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
            if parse_number(text, locale)[0] is not None:
                return text
    return None


def _decode_field(
    name: str,
    spec: dict[str, Any],
    nodes: list[_Node],
    selector_id: str,
    raws: list[str],
    locale: str = "unknown",
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
            value = parse_size(raw, locale)
        elif kind == "number":
            value, unit = parse_number(raw, locale)
        else:
            value = parse_price(raw, locale)
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
                parser_locale=locale,
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
            parser_locale=locale,
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
        parser_locale=locale,
    )


def _extract_field(
    root: _Node, name: str, spec: dict[str, Any], locale: str = "unknown"
) -> FieldRecord:
    attr_nodes = _attr_matches(root, str(spec["data_attr_value"]))
    if attr_nodes:
        raws = [_combined_text(node) or node.attrs.get("data-value", "") for node in attr_nodes]
        return _decode_field(name, spec, attr_nodes, f"data_attr:{name}", raws, locale)
    label_nodes = _label_matches(root, list(spec.get("labels") or []))
    if name in {"mark", "index", "funding"}:
        # Once a node is inside a recognized market-header item, only the
        # explicit title/value structure may decode it. A generic label fallback
        # would conceal the exact class mismatch this diagnostic is meant to expose.
        label_nodes = [node for node in label_nodes if not _in_market_header_item(node)]
    if label_nodes:
        raws = []
        for node in label_nodes:
            raw = _following_numeric_text(node, allow_uncle=name == "funding", locale=locale)
            raws.append(raw or "")
        if not all(is_missing_text(raw) for raw in raws):
            return _decode_field(name, spec, label_nodes, f"label:{name}", raws, locale)
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
        return _decode_field(name, spec, unique_nodes, f"class:{tokens[0]}", raws, locale)
    if label_nodes:
        return FieldRecord(
            name=name,
            raw_text=raws[0] if label_nodes else None,
            value=None,
            selector_id=f"label:{name}",
            parse_status="missing",
            match_count=len(label_nodes),
            parser_locale=locale,
        )
    return FieldRecord(
        name=name,
        raw_text=None,
        value=None,
        selector_id=None,
        parse_status="missing",
        match_count=0,
        parser_locale=locale,
    )


def _in_market_header_item(node: _Node) -> bool:
    spec = SELECTOR_CATALOG.get("market_header") or {}
    item_token = str(spec.get("item_class_contains") or "commonItem")
    root_token = str(spec.get("root_class_contains") or "contractDetail")
    excluded = list(spec.get("item_class_exclude") or [])
    current: _Node | None = node
    while current is not None:
        classes = current.attrs.get("class", "")
        if (
            item_token in classes
            and root_token in classes
            and not any(token in classes for token in excluded)
        ):
            return True
        current = current.parent
    return False


def _parse_levels(
    root: _Node, side: str, spec: dict[str, Any], locale: str = "unknown"
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
        price = parse_price(node.attrs.get(price_attr) or _combined_text(node), locale)
        size = parse_size(node.attrs.get(size_attr) or node.attrs.get("data-qty"), locale)
        if price is None or size is None:
            continue
        levels.append((price, size))
        if len(levels) >= max_levels:
            break
    return tuple(levels) or None


def _orderbook(
    root: _Node, locale: str = "unknown"
) -> tuple[
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
    bids = _parse_levels(book, "bid", spec, locale) or _parse_levels(
        book, bid_side, spec, locale
    )
    asks = _parse_levels(book, "ask", spec, locale) or _parse_levels(
        book, ask_side, spec, locale
    )
    return bids, asks, "data_attr:orderbook", []


_DOCUMENTISH_TAGS = frozenset({"document", "html", "body"})
_WRAPPER_AMBIGUITY_CODES = frozenset(
    {
        "ambiguous_live_orderbook",
        "crossed_wrapper_bbo",
        "missing_wrapper_bbo",
    }
)


def _own_text(node: _Node) -> str:
    return collapse_ws(node.text)


def _style_hides(style: str) -> bool:
    compact = "".join(style.lower().split())
    return "display:none" in compact or "visibility:hidden" in compact


def _is_visible(node: _Node) -> bool:
    """Python fixture visibility: connected tree + not display:none/visibility:hidden/hidden.

    Live extension also requires a non-zero rendered rect. Fixtures have no layout
    engine, so they encode hidden duplicates with hidden/style rather than zero boxes.
    """

    current: _Node | None = node
    while current is not None and current.tag != "document":
        if "hidden" in current.attrs:
            return False
        if _style_hides(current.attrs.get("style") or ""):
            return False
        current = current.parent
    return True


def _node_contains(ancestor: _Node, node: _Node) -> bool:
    current: _Node | None = node
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


def _depth(node: _Node) -> int:
    n = 0
    current: _Node | None = node
    while current is not None:
        n += 1
        current = current.parent
    return n


def _lca(left: _Node, right: _Node) -> _Node | None:
    seen: set[int] = set()
    current: _Node | None = left
    while current is not None:
        seen.add(id(current))
        current = current.parent
    current = right
    while current is not None:
        if id(current) in seen:
            return current
        current = current.parent
    return None


def _is_documentish(node: _Node | None) -> bool:
    return node is None or node.tag in _DOCUMENTISH_TAGS


def _unique_nested_roots(roots: list[_Node]) -> list[_Node]:
    """Collapse nested duplicates of the same side. Distinct siblings stay distinct."""

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
    return unique


def _coalesce_book_roots(
    roots: list[_Node], problem_code: str = "ambiguous_orderbook_heading"
) -> tuple[_Node | None, list[str]]:
    """One unique panel, or invalid. Nested headings of the same panel are ok."""

    unique = _unique_nested_roots(roots)
    if not unique:
        return None, []
    if len(unique) > 1:
        return None, [problem_code]
    return unique[0], []


def _bounded_text_tokens(node: _Node, max_tokens: int = 12) -> list[str]:
    tokens: list[str] = []
    for child in _walk(node):
        piece = child.text
        if not piece or not piece.strip():
            continue
        tokens.append(piece)
        if len(tokens) >= max_tokens:
            break
    return tokens


def _node_price_hit(node: _Node, locale: str) -> _PriceHit | None:
    """Parse a wrapper/header price node. Keep DOM text; never stringify the float."""

    tokens = _bounded_text_tokens(node)
    joined = join_price_tokens(tokens)
    combined = collapse_ws(_combined_text(node) or _own_text(node))
    if joined is None and len(tokens) > 1:
        return None
    text = joined or combined
    price = parse_price(text, locale)
    if price is None:
        return None
    raw_text = combined if combined else joined
    if not raw_text:
        return None
    return _PriceHit(price, raw_text, tuple(tokens))


def _normalize_header_title(text: str) -> str:
    compact = collapse_ws(text).lower().replace("/", " / ")
    return " ".join(compact.split())


def _header_alias_lookup() -> dict[str, str]:
    spec = SELECTOR_CATALOG.get("market_header") or {}
    aliases = spec.get("field_title_aliases") or {}
    lookup: dict[str, str] = {}
    for field_name, titles in aliases.items():
        for title in titles:
            key = _normalize_header_title(str(title))
            lookup[key] = str(field_name)
    return lookup


def _header_items(root: _Node) -> list[_Node]:
    spec = SELECTOR_CATALOG.get("market_header") or {}
    item_token = str(spec.get("item_class_contains") or "commonItem")
    root_token = str(spec.get("root_class_contains") or "contractDetail")
    excluded = list(spec.get("item_class_exclude") or [])
    items: list[_Node] = []
    for node in _walk(root):
        if _ignored(node) or not _is_visible(node):
            continue
        classes = node.attrs.get("class", "")
        if item_token not in classes or root_token not in classes:
            continue
        if any(token in classes for token in excluded):
            continue
        items.append(node)
    return items


_HEADER_PROBE_MAX_ITEMS = 12
_HEADER_PROBE_MAX_CHILDREN = 8
_HEADER_PROBE_MAX_CLASS_TOKENS = 16
_HEADER_PROBE_MAX_RELEVANT_TOKENS = 24
_HEADER_PROBE_MAX_TEXT_TOKENS = 16
_HEADER_PROBE_MAX_ATTRIBUTE_RECORDS = 8
_HEADER_PROBE_ATTRIBUTE_KEYS = (
    "title",
    "aria-label",
    "aria-labelledby",
    "data-title",
    "data-tooltip",
    "data-original-title",
    "role",
)
_HEADER_RELEVANT_CLASS = re.compile(
    r"title|content|value|label|price|rate|fair|index|fund|item", re.IGNORECASE
)
_HEADER_NUMBER_SHAPE = re.compile(r"[-+]?\d[\d\s.,:%/:-]*")
_HEADER_PRIVATE_HINT = re.compile(
    r"account|balance|wallet|position|\borders?\b|order(?:form|panel|entry|history)|"
    r"margin|asset|equity|available|"
    r"api.?key|secret|credential|email|\buid\b",
    re.IGNORECASE,
)


def _cap_probe_text(value: str, limit: int) -> str:
    return collapse_ws(value)[:limit]


def _probe_class_tokens(node: _Node) -> list[str]:
    return [
        _cap_probe_text(token, 80)
        for token in node.attrs.get("class", "").split()[:_HEADER_PROBE_MAX_CLASS_TOKENS]
    ]


def _probe_attributes(node: _Node) -> dict[str, str]:
    return {
        key: _cap_probe_text(node.attrs[key], 120)
        for key in _HEADER_PROBE_ATTRIBUTE_KEYS
        if node.attrs.get(key)
    }


def _private_probe_node(node: _Node) -> bool:
    values = [node.attrs.get("class", ""), _own_text(node)]
    values.extend(node.attrs.get(key, "") for key in _HEADER_PROBE_ATTRIBUTE_KEYS)
    return bool(_HEADER_PRIVATE_HINT.search(" ".join(values)))


def _under_private_probe_node(node: _Node, root: _Node) -> bool:
    current: _Node | None = node
    while current is not None:
        if _private_probe_node(current):
            return True
        if current is root:
            return False
        current = current.parent
    return False


def _probe_visible_tokens(node: _Node) -> list[str]:
    tokens: list[str] = []
    for child in _walk(node):
        if _under_private_probe_node(child, node):
            continue
        token = _cap_probe_text(child.text, 80)
        if token:
            tokens.append(token)
        if len(tokens) >= _HEADER_PROBE_MAX_TEXT_TOKENS:
            break
    return tokens


def _probe_child(node: _Node, title_token: str, value_token: str) -> dict[str, Any]:
    if _private_probe_node(node):
        return {
            "tag": _cap_probe_text(node.tag, 24),
            "class_string": "",
            "class_tokens": [],
            "visible_text": "",
            "visible_text_tokens": [],
            "attributes": {},
            "current_title_token_matched": False,
            "current_value_token_matched": False,
            "redacted": True,
        }
    classes = node.attrs.get("class", "")
    text_tokens = _probe_visible_tokens(node)
    return {
        "tag": _cap_probe_text(node.tag, 24),
        "class_string": _cap_probe_text(classes, 240),
        "class_tokens": _probe_class_tokens(node),
        "visible_text": _cap_probe_text(" ".join(text_tokens), 240),
        "visible_text_tokens": text_tokens,
        "attributes": _probe_attributes(node),
        "current_title_token_matched": title_token in classes,
        "current_value_token_matched": value_token in classes,
        "redacted": False,
    }


def _probe_item(
    item: _Node, item_index: int, title_token: str, value_token: str
) -> dict[str, Any]:
    if _private_probe_node(item):
        return {
            "item_index": item_index,
            "tag": _cap_probe_text(item.tag, 24),
            "class_string": "",
            "class_tokens": [],
            "direct_children": [],
            "descendant_relevant_class_tokens": [],
            "descendant_attributes": [],
            "visible_text": "",
            "visible_text_tokens": [],
            "attributes": {},
            "current_title_token_matched": False,
            "current_value_token_matched": False,
            "redacted": True,
        }
    descendants = _walk(item)[1:]
    relevant_tokens: list[str] = []
    attribute_records: list[dict[str, Any]] = []
    for node in descendants:
        if _under_private_probe_node(node, item):
            continue
        for token in node.attrs.get("class", "").split():
            if _HEADER_RELEVANT_CLASS.search(token) and token not in relevant_tokens:
                relevant_tokens.append(_cap_probe_text(token, 80))
                if len(relevant_tokens) >= _HEADER_PROBE_MAX_RELEVANT_TOKENS:
                    break
        attrs = _probe_attributes(node)
        if attrs and len(attribute_records) < _HEADER_PROBE_MAX_ATTRIBUTE_RECORDS:
            attribute_records.append({"tag": _cap_probe_text(node.tag, 24), "attributes": attrs})
    text_tokens = _probe_visible_tokens(item)
    classes = item.attrs.get("class", "")
    return {
        "item_index": item_index,
        "tag": _cap_probe_text(item.tag, 24),
        "class_string": _cap_probe_text(classes, 240),
        "class_tokens": _probe_class_tokens(item),
        "direct_children": [
            _probe_child(child, title_token, value_token)
            for child in item.children[:_HEADER_PROBE_MAX_CHILDREN]
            if _is_visible(child) and not _ignored(child)
        ],
        "descendant_relevant_class_tokens": relevant_tokens,
        "descendant_attributes": attribute_records,
        "visible_text": _cap_probe_text(" ".join(text_tokens), 240),
        "visible_text_tokens": text_tokens,
        "attributes": _probe_attributes(item),
        "current_title_token_matched": any(
            title_token in node.attrs.get("class", "")
            for node in descendants
            if not _under_private_probe_node(node, item)
        ),
        "current_value_token_matched": any(
            value_token in node.attrs.get("class", "")
            for node in descendants
            if not _under_private_probe_node(node, item)
        ),
        "redacted": False,
    }


def _probe_signature_shape(value: Any) -> Any:
    if isinstance(value, str):
        return _HEADER_NUMBER_SHAPE.sub("<number>", value.lower())
    if isinstance(value, list):
        return [_probe_signature_shape(item) for item in value]
    if isinstance(value, dict):
        return {key: _probe_signature_shape(item) for key, item in value.items()}
    return value


def _header_probe(items: list[_Node], title_token: str, value_token: str) -> dict[str, Any]:
    bounded = items[:_HEADER_PROBE_MAX_ITEMS]
    summaries = [
        _probe_item(item, index, title_token, value_token)
        for index, item in enumerate(bounded)
    ]
    signature_shape = _probe_signature_shape(summaries)
    encoded = json.dumps(signature_shape, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {
        "probe_version": 1,
        "structural_signature": f"sha256:{hashlib.sha256(encoded).hexdigest()[:16]}",
        "matched_item_count": len(items),
        "items_truncated": len(items) > _HEADER_PROBE_MAX_ITEMS,
        "items": summaries,
    }


def _item_class_text(item: _Node, token: str) -> str:
    texts: list[str] = []
    for node in _walk(item):
        if token not in node.attrs.get("class", ""):
            continue
        text = _combined_text(node) or _own_text(node)
        if text:
            texts.append(text)
    if not texts:
        return ""
    return max(texts, key=len)


def _extract_market_header(
    root: _Node, locale: str
) -> tuple[dict[str, FieldRecord], dict[str, Any]]:
    """Read Fair/Index/Funding from contractDetail items. No page HTML stored."""

    spec = SELECTOR_CATALOG.get("market_header") or {}
    title_token = str(spec.get("title_class_contains") or "itemTitle")
    value_token = str(spec.get("value_class_contains") or "itemContent")
    lookup = _header_alias_lookup()
    grouped: dict[str, list[tuple[_Node, str]]] = {"mark": [], "index": [], "funding": []}
    diag = empty_header_diagnostics()
    diag["ui_locale"] = locale
    diag["parser_mode"] = locale
    items = _header_items(root)
    diag["header_item_count"] = len(items)
    diag["market_header_probe"] = _header_probe(items, title_token, value_token)
    for item in items:
        title = _item_class_text(item, title_token)
        value = _item_class_text(item, value_token)
        field_name = lookup.get(_normalize_header_title(title))
        if field_name not in grouped:
            continue
        grouped[field_name].append((item, value))
    diag["header_title_hits_mark"] = len(grouped["mark"])
    diag["header_title_hits_index"] = len(grouped["index"])
    diag["header_title_hits_funding"] = len(grouped["funding"])
    out: dict[str, FieldRecord] = {}
    for name, rows in grouped.items():
        if not rows:
            continue
        field_spec = SELECTOR_CATALOG["fields"][name]
        nodes = [row[0] for row in rows]
        raws = [row[1] for row in rows]
        out[name] = _decode_field(
            name, field_spec, nodes, f"header_struct:{name}", raws, locale
        )
    return out, diag


def _overlay_header_fields(
    fields: dict[str, FieldRecord], header_fields: dict[str, FieldRecord]
) -> None:
    for name, rec in header_fields.items():
        current = fields.get(name)
        if current is None:
            fields[name] = rec
            continue
        if current.selector_id and str(current.selector_id).startswith("data_attr"):
            continue
        if rec.parse_status in {"ok", "ok_redundant", "ambiguous", "unparsable"} or (
            current.parse_status == "missing"
        ):
            fields[name] = rec


def _wrapper_prices(wrap: _Node, price_token: str, locale: str) -> list[_PriceHit]:
    hits: list[_PriceHit] = []
    for node in _walk(wrap):
        if _ignored(node):
            continue
        if price_token not in node.attrs.get("class", ""):
            continue
        hit = _node_price_hit(node, locale)
        if hit is not None:
            hits.append(hit)
    return hits


def _tree_distance(left: _Node, right: _Node) -> int:
    ancestor = _lca(left, right)
    if ancestor is None:
        return 10**9
    return _depth(left) + _depth(right) - 2 * _depth(ancestor)


def _pair_ask_bid_wrappers(
    asks: list[_Node], bids: list[_Node]
) -> list[tuple[_Node, _Node]]:
    """Pair visible ask/bid wrappers that share a book component.

    Exactly one visible ask + one visible bid is always a pair, even when the
    LCA is body (MEXC often renders the two wrappers as siblings).
    """

    asks_u = _unique_nested_roots(asks)
    bids_u = _unique_nested_roots(bids)
    if not asks_u or not bids_u:
        return []
    if len(asks_u) == 1 and len(bids_u) == 1:
        return [(asks_u[0], bids_u[0])]
    scored: list[tuple[int, int, _Node, _Node]] = []
    for ask_node in asks_u:
        for bid_node in bids_u:
            ancestor = _lca(ask_node, bid_node)
            scored.append(
                (
                    _tree_distance(ask_node, bid_node),
                    0 if ancestor is None else _depth(ancestor),
                    ask_node,
                    bid_node,
                )
            )
    scored.sort(key=lambda row: (row[0], -row[1]))
    used_asks: set[int] = set()
    used_bids: set[int] = set()
    pairs: list[tuple[_Node, _Node]] = []
    for _dist, _lca_depth, ask_node, bid_node in scored:
        if id(ask_node) in used_asks or id(bid_node) in used_bids:
            continue
        pairs.append((ask_node, bid_node))
        used_asks.add(id(ask_node))
        used_bids.add(id(bid_node))
    return pairs


def _pair_nested_in(
    inner: tuple[_Node, _Node], outer: tuple[_Node, _Node]
) -> bool:
    """True when inner sits inside the outer pair's non-document container."""

    container = _lca(outer[0], outer[1])
    if _is_documentish(container) or container is None:
        return False
    return _node_contains(container, inner[0]) and _node_contains(container, inner[1])


def _bbo_from_wrapper_pair(
    ask_wrap: _Node,
    bid_wrap: _Node,
    ask_token: str,
    bid_token: str,
    locale: str,
) -> tuple[_PriceHit | None, _PriceHit | None, str | None]:
    asks = _wrapper_prices(ask_wrap, ask_token, locale)
    bids = _wrapper_prices(bid_wrap, bid_token, locale)
    if not asks or not bids:
        return None, None, "missing_wrapper_bbo"
    best_ask = min(asks, key=lambda hit: hit.value)
    best_bid = max(bids, key=lambda hit: hit.value)
    if best_bid.value >= best_ask.value:
        return None, None, "crossed_wrapper_bbo"
    return best_bid, best_ask, None


def _count_orderbook_presence(root: _Node) -> dict[str, Any]:
    spec = SELECTOR_CATALOG["live_orderbook"]
    headings = _label_matches(root, list(spec.get("heading_labels") or []))
    asks = _class_hits(root, str(spec.get("asks_class_contains") or "asksWrapper"))
    bids = _class_hits(root, str(spec.get("bids_class_contains") or "bidsWrapper"))
    diag = empty_orderbook_diagnostics()
    diag["orderbook_heading_count"] = len(headings)
    diag["visible_orderbook_heading_count"] = sum(1 for node in headings if _is_visible(node))
    diag["asks_wrapper_count"] = len(asks)
    diag["visible_asks_wrapper_count"] = sum(1 for node in asks if _is_visible(node))
    diag["bids_wrapper_count"] = len(bids)
    diag["visible_bids_wrapper_count"] = sum(1 for node in bids if _is_visible(node))
    return diag


def _wrapper_path_available(diag: dict[str, Any]) -> bool:
    return (
        int(diag.get("visible_asks_wrapper_count") or 0) > 0
        and int(diag.get("visible_bids_wrapper_count") or 0) > 0
    )


def _resolve_wrapper_bbo(
    root: _Node,
    locale: str,
) -> tuple[_PriceHit | None, _PriceHit | None, list[str]]:
    """Canonical live BBO from visible asksWrapper/bidsWrapper pairs.

    Sides come from MEXC wrapper/class tokens only. Last is never used to split.
    """

    spec = SELECTOR_CATALOG["live_orderbook"]
    ask_wrap_token = spec.get("asks_class_contains")
    bid_wrap_token = spec.get("bids_class_contains")
    if not ask_wrap_token or not bid_wrap_token:
        return None, None, []
    ask_token = str(spec["ask_price_class_contains"])
    bid_token = str(spec["bid_price_class_contains"])
    visible_asks = [
        node
        for node in _class_hits(root, str(ask_wrap_token))
        if _is_visible(node)
    ]
    visible_bids = [
        node
        for node in _class_hits(root, str(bid_wrap_token))
        if _is_visible(node)
    ]
    if not visible_asks or not visible_bids:
        return None, None, []
    asks_u = _unique_nested_roots(visible_asks)
    bids_u = _unique_nested_roots(visible_bids)
    pairs = _pair_ask_bid_wrappers(visible_asks, visible_bids)
    used = {id(node) for pair in pairs for node in pair}
    leftover_priced = False
    for node in asks_u:
        if id(node) not in used and _wrapper_prices(node, ask_token, locale):
            leftover_priced = True
            break
    if not leftover_priced:
        for node in bids_u:
            if id(node) not in used and _wrapper_prices(node, bid_token, locale):
                leftover_priced = True
                break
    if leftover_priced:
        return None, None, ["ambiguous_live_orderbook"]
    if not pairs:
        return None, None, ["ambiguous_live_orderbook"]
    resolved: list[tuple[tuple[_Node, _Node], _PriceHit, _PriceHit]] = []
    for ask_wrap, bid_wrap in pairs:
        best_bid, best_ask, error = _bbo_from_wrapper_pair(
            ask_wrap, bid_wrap, ask_token, bid_token, locale
        )
        if error:
            return None, None, [error]
        if best_bid is None or best_ask is None:
            return None, None, ["missing_wrapper_bbo"]
        resolved.append(((ask_wrap, bid_wrap), best_bid, best_ask))
    unique_bbos = {(bid.value, ask.value) for _pair, bid, ask in resolved}
    if len(unique_bbos) > 1:
        return None, None, ["ambiguous_live_orderbook"]
    if len(resolved) == 1:
        _pair, best_bid, best_ask = resolved[0]
        return best_bid, best_ask, []
    # Identical BBO on multiple visible pairs: allow only nested containment.
    outers: list[tuple[tuple[_Node, _Node], _PriceHit, _PriceHit]] = []
    for candidate in resolved:
        nested = False
        for other in resolved:
            if candidate[0] is other[0]:
                continue
            if _pair_nested_in(candidate[0], other[0]):
                nested = True
                break
        if not nested:
            outers.append(candidate)
    if len(outers) == 1:
        _pair, best_bid, best_ask = outers[0]
        return best_bid, best_ask, []
    return None, None, ["ambiguous_live_orderbook"]


def _collect_own_price_hits(root: _Node, locale: str) -> list[_PriceHit]:
    hits: list[_PriceHit] = []
    for node in _walk(root):
        if _ignored(node):
            continue
        hit = _node_price_hit(node, locale)
        if hit is not None and _own_text(node):
            hits.append(hit)
    return hits


def _live_orderbook(
    root: _Node, last_value: float | None, locale: str
) -> tuple[_PriceHit | None, _PriceHit | None, list[str]]:
    """Heading fallback only: unique visible Order Book panel, split by last.

    Never Fair/Index, never ticket numbers, never used when wrappers are present.
    """

    spec = SELECTOR_CATALOG["live_orderbook"]
    headings = [
        node
        for node in _label_matches(root, list(spec.get("heading_labels") or []))
        if _is_visible(node)
    ]
    if not headings:
        return None, None, []
    header_labels = [
        "Fair Price",
        "Mark Price",
        "Index Price",
        "Funding Rate / Countdown",
        "Funding Rate",
        "Справедливая цена",
        "Индексная цена",
        "Ставка финансирования/Обратный отсчет",
        "Ставка финансирования",
    ]
    band = float(spec["price_band_frac"])
    min_side = int(spec["min_side_levels"])
    book, problems = _coalesce_book_roots(
        [(heading.parent or heading) for heading in headings]
    )
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
            hit
            for hit in _collect_own_price_hits(node, locale)
            if abs(hit.value - last_value) / last_value <= band
        ]
        asks = [hit for hit in near if hit.value > last_value]
        bids = [hit for hit in near if hit.value < last_value]
        if len(asks) >= min_side and len(bids) >= min_side:
            chosen = node
            break
        node = node.parent
    if chosen is None:
        return None, None, []
    near = [
        hit
        for hit in _collect_own_price_hits(chosen, locale)
        if abs(hit.value - last_value) / last_value <= band
    ]
    asks = [hit for hit in near if hit.value > last_value]
    bids = [hit for hit in near if hit.value < last_value]
    best_ask = min(asks, key=lambda hit: hit.value)
    best_bid = max(bids, key=lambda hit: hit.value)
    if best_bid.value >= best_ask.value:
        return None, None, []
    return best_bid, best_ask, []


def _chosen_bbo_source(fields: dict[str, FieldRecord]) -> str:
    bid = fields.get("bid")
    ask = fields.get("ask")
    if bid is None or ask is None:
        return "none"
    ok = {"ok", "ok_redundant"}
    if bid.parse_status not in ok or ask.parse_status not in ok:
        return "none"
    bid_sel = bid.selector_id or ""
    ask_sel = ask.selector_id or ""
    if "live_asks_bids_wrapper" in {bid_sel, ask_sel}:
        return "live_asks_bids_wrapper"
    if "live_orderbook_split_by_last" in {bid_sel, ask_sel}:
        return "live_orderbook_heading_fallback"
    if bid_sel.startswith("data_attr") or ask_sel.startswith("data_attr"):
        return "data_attr"
    if bid_sel in {"orderbook_max_bid", "orderbook_min_ask"} or ask_sel in {
        "orderbook_max_bid",
        "orderbook_min_ask",
    }:
        return "data_attr:orderbook"
    return bid_sel or ask_sel or "none"


def _field_from_hit(
    name: str, hit: _PriceHit, selector_id: str, locale: str
) -> FieldRecord:
    return FieldRecord(
        name=name,
        raw_text=hit.raw_text,
        value=hit.value,
        selector_id=selector_id,
        parse_status="ok",
        match_count=1,
        parser_locale=locale,
        raw_tokens=hit.tokens,
    )


def _finish_header_diagnostics(
    diag: dict[str, Any], fields: dict[str, FieldRecord], locale: str
) -> dict[str, Any]:
    diag["ui_locale"] = locale
    diag["parser_mode"] = locale
    for name in ("symbol", "last", "mark", "index", "funding"):
        rec = fields.get(name)
        diag[f"{name}_status"] = rec.parse_status if rec is not None else "missing"
        diag[f"{name}_selector_id"] = rec.selector_id if rec is not None else None
    reasons: list[str] = []
    for name in ("mark", "index", "funding"):
        rec = fields.get(name)
        if rec is not None and rec.parse_status == "ambiguous":
            reasons.append(f"ambiguous:{name}")
    diag["ambiguity_reason"] = reasons[0] if reasons else None
    return diag


def _ambiguity_reason(extra_reasons: list[str]) -> str | None:
    for code in extra_reasons:
        if code in _WRAPPER_AMBIGUITY_CODES or code == "ambiguous_orderbook_heading":
            return code
    return None


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
    locale = locale_from_pathname(page_path)
    fields: dict[str, FieldRecord] = {}
    for name, spec in SELECTOR_CATALOG["fields"].items():
        fields[name] = _extract_field(root, name, spec, locale)
    header_fields, header_diag = _extract_market_header(root, locale)
    _overlay_header_fields(fields, header_fields)
    depth_bids, depth_asks, depth_selector, depth_problems = _orderbook(root, locale)
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
            parser_locale=locale,
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
            parser_locale=locale,
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
            parser_locale=locale,
        )
        if fields["bid_size"].parse_status == "missing" and best_bid[1] > 0:
            fields["bid_size"] = FieldRecord(
                name="bid_size",
                raw_text=str(best_bid[1]),
                value=best_bid[1],
                selector_id="orderbook_max_bid",
                parse_status="ok",
                match_count=1,
                parser_locale=locale,
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
            parser_locale=locale,
        )
        if fields["ask_size"].parse_status == "missing" and best_ask[1] > 0:
            fields["ask_size"] = FieldRecord(
                name="ask_size",
                raw_text=str(best_ask[1]),
                value=best_ask[1],
                selector_id="orderbook_min_ask",
                parse_status="ok",
                match_count=1,
                parser_locale=locale,
            )

    last_rec = fields["last"]
    last_raw = last_rec.value
    last_value = (
        float(last_raw)
        if isinstance(last_raw, int | float) and not isinstance(last_raw, bool)
        else None
    )
    diagnostics = _count_orderbook_presence(root)
    wrapper_available = _wrapper_path_available(diagnostics)
    needs_live_bbo = (
        fields["bid"].parse_status == "missing" or fields["ask"].parse_status == "missing"
    )
    if needs_live_bbo and wrapper_available:
        wrap_bid, wrap_ask, wrap_problems = _resolve_wrapper_bbo(root, locale)
        extra_reasons.extend(wrap_problems)
        if wrap_bid is not None and fields["bid"].parse_status == "missing":
            fields["bid"] = _field_from_hit(
                "bid", wrap_bid, "live_asks_bids_wrapper", locale
            )
        if wrap_ask is not None and fields["ask"].parse_status == "missing":
            fields["ask"] = _field_from_hit(
                "ask", wrap_ask, "live_asks_bids_wrapper", locale
            )
    # Heading fallback only when the wrapper path is unavailable. Duplicate
    # headings must not run (or invalidate) when wrappers uniquely resolve.
    if (
        (fields["bid"].parse_status == "missing" or fields["ask"].parse_status == "missing")
        and not wrapper_available
    ):
        live_bid, live_ask, live_problems = _live_orderbook(root, last_value, locale)
        extra_reasons.extend(live_problems)
        if live_bid is not None and fields["bid"].parse_status == "missing":
            fields["bid"] = _field_from_hit(
                "bid", live_bid, "live_orderbook_split_by_last", locale
            )
        if live_ask is not None and fields["ask"].parse_status == "missing":
            fields["ask"] = _field_from_hit(
                "ask", live_ask, "live_orderbook_split_by_last", locale
            )
    diagnostics["chosen_bbo_source"] = _chosen_bbo_source(fields)
    diagnostics["ambiguity_reason"] = _ambiguity_reason(extra_reasons)

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

    probe = header_diag.get("market_header_probe")
    probe_signature = (
        str(probe.get("structural_signature")) if isinstance(probe, dict) else None
    )
    if (
        previous is not None
        and previous.capture_id == capture_id
        and probe_signature
        and previous.header_probe_signature == probe_signature
    ):
        header_diag["market_header_probe"] = None

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
        orderbook_diagnostics=diagnostics,
        ui_locale=locale,
        parser_mode=locale,
        header_diagnostics=_finish_header_diagnostics(header_diag, aged, locale),
        header_probe_signature=probe_signature,
    )
