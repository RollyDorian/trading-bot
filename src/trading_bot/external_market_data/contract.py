"""Official Binance USD-M Futures public WebSocket contract (verified).

Source pages (Binance Open Platform / legacy-docs mirror of current contract):
- Connect / Websocket Market Streams
- Individual Symbol Book Ticker Streams (URL PATH /public)
- Aggregate Trade Streams (URL PATH /market)

Verified locally against live public REST on 2026-08-11 (ETHUSDT bookTicker /
aggTrades reachable on fapi.binance.com). No private/account APIs.
"""

from __future__ import annotations

from typing import Final

CONTRACT_NAME: Final = "binance_usdm_futures_websocket_market_streams"
CONTRACT_VERIFIED_AT_UTC: Final = "2026-08-11T20:00:00+00:00"
CONTRACT_DOCS: Final = {
    "connect": (
        "https://developers.binance.com/legacy-docs/derivatives/"
        "usds-margined-futures/websocket-market-streams"
    ),
    "book_ticker": (
        "https://developers.binance.com/legacy-docs/derivatives/"
        "usds-margined-futures/websocket-market-streams/"
        "Individual-Symbol-Book-Ticker-Streams"
    ),
    "agg_trade": (
        "https://developers.binance.com/legacy-docs/derivatives/"
        "usds-margined-futures/websocket-market-streams/"
        "Aggregate-Trade-Streams"
    ),
}

# Routed bases after 2026-03-06 USDM WS upgrade; legacy unrouted hosts retire 2026-04-23.
BASE_HOST: Final = "wss://fstream.binance.com"
PUBLIC_BASE: Final = f"{BASE_HOST}/public"
MARKET_BASE: Final = f"{BASE_HOST}/market"

VENUE: Final = "binance_usdm"
INSTRUMENT: Final = "ETHUSDT"
STREAM_SYMBOL: Final = "ethusdt"  # official: all stream symbols lowercase

BOOK_TICKER_STREAM: Final = f"{STREAM_SYMBOL}@bookTicker"
AGG_TRADE_STREAM: Final = f"{STREAM_SYMBOL}@aggTrade"

# Raw single-stream URLs (ws mode). Two connections required: different URL PATH.
BOOK_TICKER_WS_URL: Final = f"{PUBLIC_BASE}/ws/{BOOK_TICKER_STREAM}"
AGG_TRADE_WS_URL: Final = f"{MARKET_BASE}/ws/{AGG_TRADE_STREAM}"

# Official connection lifetime / keepalive (Connect page).
CONNECTION_MAX_HOURS: Final = 24
SERVER_PING_INTERVAL_MINUTES: Final = 3
SERVER_PONG_TIMEOUT_MINUTES: Final = 10
CLIENT_INCOMING_MSG_LIMIT_PER_SEC: Final = 10
MAX_STREAMS_PER_CONNECTION: Final = 1024

ENVELOPE_SCHEMA_VERSION: Final = 1

# bookTicker identifiers (official response example).
BOOK_TICKER_REQUIRED_FIELDS: Final = frozenset(
    {"e", "u", "s", "E", "T", "b", "B", "a", "A"}
)
# aggTrade identifiers (official response example; nq/st optional after CM migration).
AGG_TRADE_REQUIRED_FIELDS: Final = frozenset(
    {"e", "E", "s", "a", "p", "q", "f", "l", "T", "m"}
)
