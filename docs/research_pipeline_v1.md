# Offline research pipeline v1

STATUS: implemented locally as `trading_bot.research.pipeline`.

## Source of truth

Historical research uses **verified B2 RAW** (restored `events.parquet` windows /
generation archives). Production PostgreSQL is a bounded hot buffer only and must
not be the primary historical research sink.

## Inventory

`data/research/inventory/production_verified_inventory.json` (gitignored under
`/data/research/`; keep a reviewed copy in operator notes) lists:

| Dataset | Ids | Rows | Trades | Storage |
|---|---|---|---|---|
| prior continuous | `6207906..7471912` | 1,264,007 | 3,979 | B2 22/22 COMPLETED + restore PASS |
| `g_7471913_7871913` | `7471913..7871912` | 400,000 | 1,073 | B2 verified; local DROPPED |

## Pipeline

```text
events.parquet (verified RAW bundle)
  → normalized_events/*.parquet
  → market_state_1s/market_state_1s.parquet
  → features/features_v1.parquet
  → labels/labels_v1.parquet
  → reports/baseline_{momentum,mean_reversion,imbalance}.json
```

Hard invariant: `available_at = received_at`. Features use only
`available_at <= decision_time`. Labels may look ahead; they never enter features.

## Cost model (v1)

Same conservative taker model as paper-admission baselines:

* taker fee 4.5 bps/side;
* slippage 2 bps;
* latency penalty 1 bp;
* funding placeholder 1 bp / 8h absolute;
* execution delay 1s;
* fills at ask (buy) / bid (sell), never frictionless mid.

## Splits

Chronological only. Prefer prior continuous for exploration; keep a later
verified generation for final OOS. Apply purge/embargo around label horizons.

## CLI entry

```powershell
.\.venv\Scripts\python.exe -c "from pathlib import Path; from trading_bot.research.pipeline.run import run_research_pipeline_v1; print(run_research_pipeline_v1(events_parquet=Path('events.parquet'), workspace=Path('data/research/runs/demo'), source_dataset_id='demo'))"
```
