# MEXC zero-fee scalping bot

Local, public-data-only research stack for ultra-short (≤ a few seconds)
positions on MEXC USD-M futures (primary instrument `TAOUSDT`), targeting the
zero-fee treatment that MEXC grants to Web-UI traders.

The **Web UI is the trading surface**: MEXC zero fees do not apply to the bot
API, so market data (and eventually orders) must come through the browser.
The Chrome MV3 extension in `extensions/mexc_ui_capture/` captures rendered
futures data; the pure-stdlib shadow engine in
`src/trading_bot/research/mexc_shadow/` turns those snapshots into virtual
positions and cost-aware reports.

## Current state

The engine is **shadow-only**: virtual positions, append-only NDJSON research
events, no real orders. Real UI order placement is a later milestone and must
be added explicitly.

## Repository layout

| Path | What it is |
|---|---|
| `extensions/mexc_ui_capture/` | Chrome MV3 extension (v1.3.7): read-only capture of rendered MEXC futures market data (bid/ask/last/mark/index), durable IndexedDB chunks, optional loopback forwarding to `http://127.0.0.1:8765/v1/snapshot` |
| `src/trading_bot/research/mexc_shadow/` | Shadow engine, pure stdlib: `MarketDataSource` → `FeatureEngine` (pluggable) → `CandidateGate` → `ShadowBook` → cost overlay. `mvp.py` provides the loopback receiver (`serve`) and deterministic replay (`replay`). `ui_capture/` normalizes and validates captured UI exports |
| `configs/mexc_shadow/` | Frozen profiles: `author_observed_v0`, `conservative_v0` |
| `data/mexc_ui_capture/`, `data/mexc_shadow/` | Captured corpora and shadow events (git-ignored) |
| `docs/mexc_*.md` | MEXC milestone history and frozen protocol-v2 gates |
| `tests/test_mexc_*.py` | Test suite |

## Requirements

- Python 3.13+
- Chrome (or any Chromium) for the extension
- No runtime Python dependencies: the MEXC stack is pure stdlib

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe src
```

## Load the extension

1. Open `chrome://extensions`, enable **Developer mode**.
2. **Load unpacked** → select `extensions/mexc_ui_capture/`.
3. Open a MEXC futures page (e.g. `https://www.mexc.com/futures/TAOUSDT`) and
   start capture from the extension popup.

Capture is read-only: the extension writes durable IndexedDB chunks first and
never fails capture; loopback forwarding is optional and bounded.

## Shadow runner

### Live capture (loopback receiver)

```powershell
.\.venv\Scripts\mexc-shadow.exe serve `
  --extension-origin chrome-extension://<extension-id-from-chrome-extensions> `
  --events data\mexc_shadow\live_events.ndjson `
  --summary data\mexc_shadow\live_summary.json
```

The receiver accepts only the exact `chrome-extension://<32-character-id>`
Origin configured at startup; origin-less localhost CLI requests remain
supported. `OPEN` and `CLOSE` are append-only NDJSON research events; the
summary includes exit reasons, fee and slippage overlays, drawdown, tail
losses, latency, and invalid-data counters.

### Deterministic replay

```powershell
.\.venv\Scripts\mexc-shadow.exe replay `
  --raw <corpus.ndjson> `
  --events data\mexc_shadow\replay_events.ndjson `
  --summary data\mexc_shadow\replay_summary.json
```

Replay validates the corpus SHA before evaluation; a mismatch fails.

## Safety invariants

- Extension is read-only: no clicks, no DOM writes.
- Shadow engine: no order placement, no private endpoints, no trading
  credentials, no browser driver.
- Loopback receiver: exact-Origin allowlist, no wildcard CORS.
- Captured corpora are immutable evidence: SHA-locked files are never
  rewritten or rescaled.
- Frozen profiles and protocol-v2 timing gates are unchanged unless
  explicitly re-approved.

## Locked development replay

- Corpus SHA-256: `5c15b9714f804f8df5a327ae81fb2d7fb515ec052aeed0ff1af5df5a8680467c`
- 10.6 h TAOUSDT, 76,311 valid snapshots.
- Both exploratory variants (1-second executable-mid momentum with
  `mid_vs_mark` / `mid_vs_index`) are **negative in aggregate gross** —
  development sample, not OOS evidence.

Full machine result: [`docs/mexc_shadow_mvp_v0_replay.json`](docs/mexc_shadow_mvp_v0_replay.json).

## Key documents

- [`docs/mexc_shadow_mvp_v0.md`](docs/mexc_shadow_mvp_v0.md) — shadow MVP: scope, safety, run, locked replay
- [`docs/mexc_mom_gap_hypothesis_protocol_design_v1.md`](docs/mexc_mom_gap_hypothesis_protocol_design_v1.md) — protocol design
- [`docs/mexc_mom_gap_hypothesis_protocol_v2_data_contract_amendment.md`](docs/mexc_mom_gap_hypothesis_protocol_v2_data_contract_amendment.md) — frozen protocol-v2 data contract
- [`docs/mexc_zero_fee_signal_recon_and_engine_v1.md`](docs/mexc_zero_fee_signal_recon_and_engine_v1.md) — zero-fee signal reconnaissance and engine
