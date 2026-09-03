"""Versioned selector catalog. Logical field IDs only — not strategy parameters."""

from __future__ import annotations

from typing import Any

CATALOG_VERSION = "v1.1"
SCHEMA_NAME = "mexc_ui_raw_snapshot"
SCHEMA_VERSION = 1
DATA_CAPTURE_ATTR = "data-mexc-capture"
IGNORE_ATTR = "data-mexc-capture-ignore"
ALLOWED_SAMPLE_INTERVALS_MS = (250, 500, 1000)
DEFAULT_SAMPLE_INTERVAL_MS = 500

# MEXC header labels confirmed on the public TAO futures page (values may be "--"
# until the SPA hydrates). Do not map order-ticket Buy/Sell/Open controls.
SELECTOR_CATALOG: dict[str, Any] = {
    "catalog_version": CATALOG_VERSION,
    "data_capture_attr": DATA_CAPTURE_ATTR,
    "ignore_attr": IGNORE_ATTR,
    "sample_interval_ms_allowed": list(ALLOWED_SAMPLE_INTERVALS_MS),
    "ignore_ancestor_labels": [],
    "fields": {
        "symbol": {
            "kind": "symbol",
            "required_for_valid": True,
            "data_attr_value": "symbol",
            "labels": [],
        },
        "bid": {
            "kind": "price",
            "required_for_valid": True,
            "data_attr_value": "bid",
            "labels": [],
        },
        "ask": {
            "kind": "price",
            "required_for_valid": True,
            "data_attr_value": "ask",
            "labels": [],
        },
        "bid_size": {
            "kind": "size",
            "required_for_valid": False,
            "data_attr_value": "bid_size",
            "labels": [],
        },
        "ask_size": {
            "kind": "size",
            "required_for_valid": False,
            "data_attr_value": "ask_size",
            "labels": [],
        },
        "last": {
            "kind": "price",
            "required_for_valid": False,
            "data_attr_value": "last",
            # Do not treat the order-book "Last Price" dropdown as last.
            # Live header last uses a unique lastPrice class token.
            "labels": ["Last Price"],
            "class_contains": ["lastPrice"],
            "class_exclude": ["lastPriceWrapper", "scrollTo-last"],
        },
        "mark": {
            "kind": "price",
            "required_for_valid": False,
            "data_attr_value": "mark",
            # English + ru-RU aliases verified on the live TAOUSDT header.
            "labels": ["Fair Price", "Mark Price", "Справедливая цена"],
        },
        "index": {
            "kind": "price",
            "required_for_valid": False,
            "data_attr_value": "index",
            "labels": ["Index Price", "Индексная цена"],
        },
        "funding": {
            "kind": "number",
            "required_for_valid": False,
            "data_attr_value": "funding",
            "labels": [
                "Funding Rate / Countdown",
                "Funding Rate/Countdown",
                "Funding Rate",
                "Ставка финансирования/Обратный отсчет",
                "Ставка финансирования",
            ],
        },
        "exchange_display_at": {
            "kind": "timestamp",
            "required_for_valid": False,
            "data_attr_value": "exchange_display_at",
            "labels": [],
        },
    },
    "orderbook": {
        "root_attr_value": "orderbook",
        "bids_attr_value": "bids",
        "asks_attr_value": "asks",
        "level_attr": "data-mexc-capture-level",
        "price_attr": "data-price",
        "size_attr": "data-size",
        "max_levels": 20,
    },
    # Live MEXC has no data-mexc-capture attrs. Canonical BBO is asksWrapper +
    # sell / bidsWrapper + buy on a uniquely resolvable visible pair. "Order Book"
    # headings are diagnostic and heading-fallback only; last is never used to
    # assign wrapper sides. Duplicate headings must not invalidate a wrapper BBO.
    "live_orderbook": {
        "heading_labels": ["Order Book", "Книга ордеров"],
        "split_field": "last",
        "price_band_frac": 0.10,
        "min_side_levels": 1,
        "asks_class_contains": "asksWrapper",
        "bids_class_contains": "bidsWrapper",
        "ask_price_class_contains": "sell",
        "bid_price_class_contains": "buy",
    },
    # Bounded futures ticker (contractDetail commonItem). Prefer this over
    # document-wide label walks so chart Fair Price duplicates are ignored.
    "market_header": {
        "root_class_contains": "contractDetail",
        "item_class_contains": "commonItem",
        "item_class_exclude": ["lastPriceWrapper", "rateItem"],
        "title_class_contains": "itemTitle",
        "value_class_contains": "itemContent",
        "field_title_aliases": {
            "mark": ["Fair Price", "Mark Price", "Справедливая цена"],
            "index": ["Index Price", "Индексная цена"],
            "funding": [
                "Funding Rate / Countdown",
                "Funding Rate/Countdown",
                "Funding Rate",
                "Ставка финансирования/Обратный отсчет",
                "Ставка финансирования",
            ],
        },
    },
}


def catalog_payload() -> dict[str, Any]:
    return dict(SELECTOR_CATALOG)
