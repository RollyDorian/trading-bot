# Edge characterization & data accumulation v1

STATUS: `EDGE_CHARACTERIZATION_READY`  
ML_DECISION: `EDGE_INSUFFICIENT_FOR_CURRENT_HORIZON`  
DATA_READY_FOR_ML: `NO`

## DATA_READINESS

| Metric | Observed | Target |
| --- | ---: | ---: |
| Calendar UTC days (exploratory prior) | 2 | 14 |
| Usable hours | ~21.2 | 252 |
| Verified generations / durable segments | 2 | 3 |
| Valid book % | 100 | ≥95 |

Regime status (exploratory tertiles): vol **sufficient**, spread **sufficient**, trend **sufficient**, activity **insufficient** (many zero-trade seconds).

ACTION: `CONTINUE_COLLECTION`

Reasoning: primary gate is temporal/regime coverage, not RAW row count. Four represented UTC days overall and ~28 usable hours remain far below a conservative 2-week gate.

## NEW_DATA

- Incremental discovery: `NO_NEW_ARCHIVES` after last validated window (`>= 20260810T210000`).
- 31 materialized research windows already known; ACTIVE production generation `g_7871913` is still filling and not yet a verified closed research segment.
- Pipeline is idempotent/resumable; no full-corpus recompute required when new COMPLETED windows appear.

## SIGNALS / EXTREME_SIGNALS (exploratory prior only)

Strongest **signed** extreme-tail gross edges:

| Feature | Horizon | Bucket | n | Gross bps | stderr |
| --- | ---: | --- | ---: | ---: | ---: |
| signed_trade_flow_1s | 15s | abs top 1% | 763 | ~2.31 | ~0.10 |
| signed_trade_flow_1s | 30s | abs top 1% | 763 | ~2.30 | ~0.14 |
| signed_trade_flow_1s | 60s | abs top 2% | 1525 | ~2.22 | ~0.13 |
| ofi_5s | 60s | frontier extreme | 764 | ~1.63 | ~0.16 |

Book microprice/imbalance still show directional IC, but extreme executable signed gross is dominated by sparse trade-flow tails and remains ~2 bps.

## TRADE_FRONTIER

Even the best exploratory thresholds remain `NOT_TRADEABLE` after ~11 bps plausible friction (example: signed_trade_flow 15s ≈ +2.3 gross → ≈ −8.7 net).

## COST_MODEL

| Component | Class | Evidence |
| --- | --- | --- |
| Fee | VERIFIED_CURRENT | Public Hibachi tiers; Tier-1 taker 4.5 bps/side, maker 0; account tier unknown → retain taker range 2.0–4.5 bps |
| Spread | OBSERVED_FROM_DATA | Prior median spread ≈ 0.053 bps |
| Slippage | OBSERVED_FROM_DATA for ≤$1k | Top-of-book capacity typically fits small notionals → extra depth slip 0 |
| Delay/latency | MODELED | 1 bp/side base; 0/1/2s sensitivity only (no sub-second claim on 1s grid) |
| Funding | PLACEHOLDER | ~0.0005 bps over 15s — negligible |

Plausible RT friction (Tier-1 + median spread + modeled latency): **~11.05 bps**.

## BREAK_EVEN

Best exploratory gross ≈ **2.3 bps** ⇒ max tolerable friction ≈ **2.3 bps** ≪ **11.05 bps** ⇒ `NOT_TRADEABLE`.

## OOS

- Current later generation `g_7471913_7871913` is **contaminated** (inspected in full-corpus validation).
- Do not fit thresholds on it.
- Future clean holdout: next newly verified generation / multi-day block after ACTIVE `g_7871913` closes.

## PROTOCOL_DRAFT

Status `DRAFT` / not frozen. See `docs/research_protocol_draft_v1.json`.

## PRODUCTION

Collector remained running/healthy; no restart, DROP, B2 mutation, or hot-PG historical scan.

## ML_DECISION

`EDGE_INSUFFICIENT_FOR_CURRENT_HORIZON`

Extreme short-horizon gross edges are stably below plausible all-in friction by ~5× on exploratory data. Collect more regimes for readiness, but reconsider horizon / maker-style execution / event selection before supervised ML.

## NEXT (not executed)

`reconsider target horizon, maker/taker execution assumptions, or event-selection strategy before introducing ML`
