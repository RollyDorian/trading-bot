# Strategy space rethink v1

STATUS: `STRATEGY_SPACE_RETHINK_READY`

DECISION: `DESIGN_EXTERNAL_FEED_PILOT`

ML_STATUS: `BLOCKED`

RECOMMENDED_HYPOTHESIS: `EXTERNAL_RELATIVE_VALUE_LEAD_LAG`

## CURRENT_HYPOTHESIS (rejected)

REJECTED: short-horizon directional microstructure prediction → direct ETH perp
trade. Best exploratory gross ~2.3 bps vs ~11.05 bps TAKER_TAKER; maker fills
adversely selected; longer horizons decay. Do not start ML on this target.

## OPPORTUNITY_BASE_RATE

Executable TOB mid absolute moves. Prefer **non-overlapping stride** (not 1s
overlapping rows).

| horizon | n | p50 bps | p95 bps | p99 bps | frac≥10bps | frac≥15bps |
|---|---:|---:|---:|---:|---:|---:|
| 15s | 5087 | 0.37 | 4.24 | 7.42 | 0.2% | 0.0% |
| 30s | 2543 | 1.10 | 5.88 | 10.20 | 1.1% | 0.1% |
| 60s | 1271 | 2.05 | 8.45 | 12.30 | 3.1% | 0.7% |
| 120s | 635 | 3.10 | 11.70 | 20.09 | 8.7% | 2.5% |
| 300s | 254 | 5.23 | 18.05 | 29.81 | 24.4% | 11.0% |
| 600s | 127 | 6.27 | 24.96 | 36.05 | 37.0% | 20.5% |
| 1800s | 42 | 9.87 | 32.62 | 48.30 | 47.6% | 28.6% |
| 3600s | 21 | 12.95 | 41.73 | 56.65 | 57.1% | 42.9% |

Interpretation: at ≤60s, moves large enough to pay ~11 bps friction are rare.
Larger absolute moves appear mainly as holding time grows (base-rate ceiling),
not as evidence of a short-horizon predictive edge.

## BASIS_DISLOCATION

- |basis_mark| p99 ≈ **2.0 bps** (small vs friction)
- Extreme-basis executable fade mean @300s ≈ **−0.79 bps** (wrong sign / no edge)
- Mark/spot are references, not executable quotes
- Decision: `REJECT_FOR_NOW`

## FUNDING_CARRY

- median |funding_rate| ≈ **6e-6** → expected 8h |carry| ≈ **0.06 bps**
- Does not cover taker RT fees alone
- Hedged carry needs an external spot/perp leg (unavailable in Hibachi-only RAW)
- Decision: `REJECT_FOR_NOW`

## LIQUIDITY_EVENTS

Sparse causal extremes (60s cooldown). Best 60s **mean** abs move ≈ **4.1 bps**
(spread widen) vs ~11 bps break-even. p95 can exceed costs, but mean does not.
Post-event signed reversion proxies are not supportive.
Decision: `WATCH` (not prioritize)

## VOLATILITY / OPPORTUNITY TARGET

- Non-overlap Stage-1 `P(|move|≥~11bps)`: ~2.0% @60s, ~20.9% @300s
- `rv_60s` precursor lift vs 60s opportunity is high, but absolute hit rate remains tiny
- Useful as a framing for longer holds / selectivity; not yet an executable strategy
- Decision: `WATCH`

## RELATIVE_VALUE_EXTERNAL

Hibachi-only directional/maker/horizon paths failed structurally relative to
friction. A lead-lag / relative-value mechanism is a **materially different**
economic hypothesis that existing Hibachi RAW cannot falsify.

Design-only requirements (not deployed):

- public market data only (trades + top-of-book candidate)
- separate failure domain from Hibachi collector
- RAW provenance: source, received_at, source timestamp, sequence when available
- external feed failure must never kill Hibachi collector
- estimated ~0.5 GiB/day at illustrative 15 evt/s × 400 B (order-of-magnitude)
- falsify first with synchronized lead-lag residual vs Hibachi executable mid

`deploy_in_this_milestone`: **false**

Decision: `PRIORITIZE` (design pilot approval next)

## SCORECARD (ranked)

| rank | class | decision | existing data | new feed |
|---:|---|---|---|---|
| 1 | EXTERNAL_RELATIVE_VALUE_LEAD_LAG | PRIORITIZE | NO | yes |
| 2 | CROSS_VENUE_BASIS_OR_CARRY | WATCH | NO | yes |
| 3 | RARE_LIQUIDITY_DISLOCATION_EVENTS | WATCH | FULL | no |
| 4 | VOLATILITY_REGIME_OR_OPPORTUNITY_TARGET | WATCH | FULL | no |
| 5 | BASIS_DISLOCATION_MEAN_REVERSION | REJECT_FOR_NOW | PARTIAL | no |
| 6 | FUNDING_CARRY | REJECT_FOR_NOW | PARTIAL | yes_for_hedge |
| 7 | SHORT_HORIZON_DIRECTIONAL_MICROSTRUCTURE | REJECT_FOR_NOW | FULL | no |

Machine-readable: `docs/strategy_space_scorecard_v1.json`

## NEW_DATA_REQUIRED

Public external liquid ETH reference feed (trades + TOB/quotes), isolated from
the Hibachi collector. No private/account APIs. Not deployed in this milestone.

## OOS

- contaminated: `g_7471913_7871913`
- clean future reserved: `RESERVED_NEXT_VERIFIED_GENERATION_AFTER_g_7871913`
- not used during screening
- when a new family protocol is frozen, designate a fresh future clean OOS

## PRODUCTION

```json
{
  "collector_observed": "Up ~9h (healthy) at report time",
  "postgres_observed": "Up (healthy)",
  "b2": "read-only",
  "no_pg_historical_scan": true
}
```

## ML_STATUS

`BLOCKED` — requires both temporal/regime coverage **and** a strategy/target with
plausible gross economics.

## NEXT (not executed)

review and approve a bounded isolated public external-market-data collector
before implementation
