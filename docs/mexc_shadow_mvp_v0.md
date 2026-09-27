# MEXC Shadow MVP v0

**Milestone:** `MEXC_SHADOW_MVP_V0`

**Status:** `MEXC_SHADOW_MVP_READY`

**Decision:** `STOP_FOR_LEAD_REVIEW`

## Scope and safety

This MVP is a local, public-data-only TAOUSDT shadow process. Extension 1.3.6 is the
smallest patch over 1.3.5 needed to forward already-extracted snapshots to
`http://127.0.0.1:8765/v1/snapshot`. IndexedDB is written first; forwarding is optional,
sequential, bounded to 32 queued snapshots, and never fails capture. The receiver exposes
only one POST endpoint and returns an acknowledgement, not commands. There are no private
APIs, account DOM selectors, credentials, browser drivers, or real/paper orders.
The final-fix receiver rejects every browser `Origin` except the exact
`chrome-extension://<32-character-id>` origin supplied at startup, reflects only that
origin, and emits no wildcard CORS header. Origin-less localhost CLI requests remain
supported.

The runner uses the existing `FeatureEngine`, `CandidateGate`, and `ShadowBook`. It evaluates
exactly two fixed exploratory variants: 1-second executable-mid momentum with
`mid_vs_mark`, and the same momentum with `mid_vs_index`. Both inherit the existing
`author_observed_v0` thresholds and exit parameters. Neither variant is asserted to be the
author's identified formula. Missing, crossed, stale (>2000 ms in continuous mode),
non-monotonic, or session-gap inputs stop candidate evaluation and reset causal feature
history; values are never backfilled. Any affected open virtual position is removed as an
unscored `CLOSE` with reason `ABANDONED_DATA_GAP`; it is excluded from trade, PnL, cost,
drawdown, and risk-state calculations rather than settled on the first post-gap quote.

## Run

```powershell
# Reload the unpacked extensions/mexc_ui_capture directory, then start the receiver.
.\.venv\Scripts\python.exe -m trading_bot.research.mexc_shadow.mvp serve `
  --extension-origin chrome-extension://<extension-id-from-chrome-extensions> `
  --events data\mexc_shadow\live_events.ndjson `
  --summary data\mexc_shadow\live_summary.json

# Deterministic development replay; SHA mismatch fails before evaluation.
.\.venv\Scripts\python.exe -m trading_bot.research.mexc_shadow.mvp replay `
  --raw "D:\программирование\trading-bot\data\mexc_ui_capture\mexc_ui_capture_e41b48eb-852c-4a36-88b6-9fc9a10ced32_2026-09-21T06-04-21-527Z.ndjson" `
  --events data\mexc_shadow\replay_events.ndjson `
  --summary data\mexc_shadow\replay_summary.json
```

`OPEN` and `CLOSE` are append-only NDJSON research events. Long entry/exit use ask/bid;
short entry/exit use bid/ask. The summary includes exit reasons, gross and 0/6/8 bps-per-side
fee overlays, 0/1/2 bps-per-side slippage, drawdown, tail losses, latency, and invalid-data
counters.

## Locked development replay

- Corpus SHA-256: `5c15b9714f804f8df5a327ae81fb2d7fb515ec052aeed0ff1af5df5a8680467c`
- Rows: 76,311 valid snapshots; 10.5999 elapsed hours; 1 >2000 ms event gap; 0 invalid
  snapshots; source timestamp latency is 0 ms by this capture's local timestamp contract.
- The locked replay had no open position at its single gap: abandoned count is 0 for both
  variants, so all previously reported trade and cost statistics remain unchanged.
- Full machine result: `docs/mexc_shadow_mvp_v0_replay.json`.

| Exploratory variant | Trades | Exit reasons G/H/R/T/Trail | Gross sum / mean bps | Net mean 6 / 8 bps-side | Max DD | Worst |
|---|---:|---|---:|---:|---:|---:|
| mid momentum 1s + mid/mark gap | 160 | 81/35/22/19/3 | -23.62 / -0.148 | -12.148 / -16.148 | 202.79 | -44.48 |
| mid momentum 1s + mid/index gap | 357 | 181/77/43/42/14 | -95.34 / -0.267 | -12.267 / -16.267 | 255.50 | -44.48 |

`G/H/R/T/Trail` means gap-hit, hard-stop, rapid-adverse, time-stop, trail-exit. These are
development-sample shadow statistics, not unseen OOS evidence and not validated
profitability. No profile, frozen protocol-v2 document, ML, PAPER, or LIVE behavior changed.

## Final-fix verification

- Two focused regression tests were added: Origin allowlist/rejection and unscored open
  position abandonment across a data gap. The MVP file now contains nine targeted tests.
- With mypy 1.20.2 unchanged, SQLAlchemy 2.1.1 reproduces the same nine CI typing errors;
  replacing only SQLAlchemy with 2.0.54 makes the same three-file mypy command pass.
  Dependency policy is therefore constrained to `sqlalchemy[asyncio]>=2.0.41,<2.1`.
