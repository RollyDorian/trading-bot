# Binance USD-M external reference feed — official contract record

STATUS: verified for implementation (2026-08-11)

## Sources checked

- Connect / Websocket Market Streams:
  https://developers.binance.com/legacy-docs/derivatives/usds-margined-futures/websocket-market-streams
- Individual Symbol Book Ticker Streams:
  https://developers.binance.com/legacy-docs/derivatives/usds-margined-futures/websocket-market-streams/Individual-Symbol-Book-Ticker-Streams
- Aggregate Trade Streams:
  https://developers.binance.com/legacy-docs/derivatives/usds-margined-futures/websocket-market-streams/Aggregate-Trade-Streams

Live public REST sanity (no auth): `fapi.binance.com` ETHUSDT bookTicker / aggTrades OK.

## Routing (post 2026-03-06 USDM WS upgrade)

| Stream | URL PATH | Example |
|---|---|---|
| `ethusdt@bookTicker` | `/public` | `wss://fstream.binance.com/public/ws/ethusdt@bookTicker` |
| `ethusdt@aggTrade` | `/market` | `wss://fstream.binance.com/market/ws/ethusdt@aggTrade` |

**Implication:** one process, **two** WebSocket connections. Unrouted
`wss://fstream.binance.com/ws/...` does **not** deliver `/market` streams.

Legacy unrouted hosts retire **2026-04-23**.

## Limits / lifetime

- Connection valid ~24 hours then disconnect
- Server ping frame every 3 minutes; pong required within 10 minutes
- ≤10 inbound client messages/sec
- ≤1024 streams per connection
- Stream symbols **lowercase**
- Combined wrapper: `{"stream":"<name>","data":{...}}`

## Identifiers (do not merge)

- bookTicker: `u` = order book updateId → envelope `book_update_id`
- aggTrade: `a` = aggregate trade id → `agg_trade_id`; `f`/`l` trade id range

## Implementation mapping

See `src/trading_bot/external_market_data/contract.py`.
