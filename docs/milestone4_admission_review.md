# Milestone 4 admission exercise

## Result

Status: **blocked by insufficient and rejected collected data**. PAPER remains disabled.
No threshold was relaxed and no market data was invented to manufacture a passing report.

The local PostgreSQL snapshot contained 966 public `ETH/USDT-P` events from
2026-07-15 through 2026-07-18, but only 2 were `trades`. Five non-overlapping,
chronological slices were exported from the available collection windows. All five
failed the current quality policy after revalidation:

| UTC window | Events | Quality result | Primary reason |
| --- | ---: | --- | --- |
| 2026-07-15 22:59–23:00 | 1 | rejected | exchange timestamp outside manifest range |
| 2026-07-15 23:00–00:00 | 726 | rejected | 58 timestamp ordering violations |
| 2026-07-16 00:00–00:01 | 51 | rejected | 8 timestamp ordering violations |
| 2026-07-18 10:47–10:49 | 127 | rejected | 25 timestamp ordering violations |
| 2026-07-18 12:10–12:11 | 61 | rejected | 12 timestamp ordering violations |

The first finding exposed a validation gap: a 2024 exchange timestamp could previously
pass inside a 2026 manifest window. Quality report schema 3 now rejects timestamps outside
`[start_utc, end_utc)`. The other slices already failed closed because heterogeneous
exchange timestamps were not chronological.

Only the first slice initially reached cost-aware replay, producing one candle and zero
trades. The remaining replay attempts correctly stopped on rejected quality. After the
range-validation fix, no slice is replay-eligible.

The saved local report `data/research/milestone4-admission.json` is intentionally ignored
by Git. It returned exit code 1 with:

- `admitted: false`
- zero admissible OOS datasets and trades
- failed artifact, quality-dataset, OOS-dataset, OOS-trade, positive-net-PnL, and
  UTC-coverage criteria

## Regime coverage review

Trend, ranging, and high-volatility regimes cannot be assigned defensibly from this
snapshot. It has two isolated trade messages, several short collection bursts, and a
multi-day gap. Mark, spot, and orderbook events cannot be relabeled as executed trades,
and synthetic prices are not representative Hibachi observations. Milestone 4 therefore
remains in progress until new COLLECT-only data supplies multiple quality-passing,
trade-bearing windows across independently labeled regimes.

## Cost assumptions

The baseline uses a USD 1,000 notional and conservative taker execution:

| Assumption | Current value | Review |
| --- | ---: | --- |
| Taker fee | 0.045% per fill | Matches Hibachi tier 1; retain. |
| Maker fee | 0.020% | Not used by the current taker calculation; do not rely on it. |
| Funding | 0.010% per 8h, absolute cost | Conservative placeholder; must be replaced or stress-tested against observed signed funding. |
| Slippage | 2 bps per fill | Uncalibrated placeholder; require orderbook-derived distribution and stress cases. |
| Latency penalty | 1 bp per fill | Uncalibrated placeholder; require measured end-to-end latency sensitivity. |
| Execution delay | 1 second | Deterministic placeholder; require sensitivity across plausible delays. |

The tier-1 taker fee is supported by the current
[Hibachi fee schedule](https://docs.hibachi.xyz/hibachi-docs/trading/fees). Hibachi funding
settles every eight hours and is signed according to perp/index divergence, so a constant
absolute charge is deliberately conservative but not a faithful realized-funding model;
see [Hibachi funding](https://docs.hibachi.xyz/hibachi-docs/trading/funding).

## Threshold review

No adjustment is justified by the available snapshot:

- **3 quality-passing datasets:** already a low floor and currently unmet.
- **2 OOS datasets / 2 UTC days:** minimum diversity guard, not regime sufficiency.
- **30 OOS trades:** too small for strong statistical claims, but useful as a fail-closed
  smoke-test floor; do not lower it.
- **Positive OOS net PnL:** necessary after modeled costs but not sufficient for admission
  confidence or future profitability.
- **Maximum drawdown 100:** equals 10% of the baseline USD 1,000 notional, but absolute
  drawdown scales poorly across trade count and dataset count. Before future PAPER work,
  replace or complement it with normalized drawdown and independently approved limits.

Threshold selection must be frozen before final OOS evaluation. The current values remain
research placeholders and were not tuned to these failed periods.

## Robustness

Collected-data checks with `(validation=1, OOS=2)`, `(validation=2, OOS=2)`, and
`(validation=1, OOS=3)` all remained `FAIL` with zero admissible OOS trades and the same
core failed criteria. This is stable failure caused by artifact/data quality, not evidence
of strategy stability.

Synthetic tests separately verify that the default decision remains `PASS` across small
split-boundary shifts when five chronological, quality-passing datasets contain compatible
reports and at least 30 positive OOS trades. Synthetic robustness validates gate mechanics
only; it is not market evidence.

## Required evidence before completing milestone 4

1. Collect longer, continuous public-market windows without starting any execution mode.
2. Resolve timestamp normalization/ordering at collection boundaries, then regenerate
   immutable manifests and quality reports.
3. Predefine objective regime labels from market observables before viewing OOS strategy
   results.
4. Produce at least four chronological datasets so training, validation, and at least two
   OOS partitions all exist; each must contain enough trades for replay.
5. Calibrate and stress funding, slippage, latency, and execution-delay assumptions using
   observed public data.
6. Repeat boundary/regime stability checks without changing thresholds after OOS review.

Even a future passing report requires human review and cannot enable PAPER.
