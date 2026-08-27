# Execution and horizon reassessment v1

STATUS: `EXECUTION_HORIZON_REASSESSMENT_READY`

DECISION: `STRATEGY_RETHINK_REQUIRED`

ML_STATUS: `BLOCKED`

## DATA_READINESS

- calendar days: 2/14
- usable hours: 21.20/252.0
- clean OOS holdout designated: `True` (placeholder reservation)
- DATA_READY_FOR_ML: `False`
- ACTION: `CONTINUE_COLLECTION`

## MAKER_DATA_SUPPORT

Public depth-20 books, TOB quotes, and trades support only fill upper/lower bounds
via volume-through / trade-through rules. Exact historical maker fills cannot be
claimed without queue priority.

- price-level fill bounds: yes (volume-through / trade-through)
- exact queue position: no
- order IDs / exchange_sequence: not reliable
- do not claim exact historical maker fills

## MAKER_FILL

Join-TOB on `signed_trade_flow_1s` |p99|, notional $1k, max wait 30s.

| scenario | submitted | fills | fill_rate | ttf_median_s | ttf_p90_s | post_fill_15s_bps | unfilled_rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| optimistic | 764 | 12 | 0.0157 | 13.0 | 22.9 | -1.533 | 0.9843 |
| base | 764 | 5 | 0.0065 | 10.0 | 20.0 | -1.406 | 0.9935 |
| conservative | 764 | 173 | 0.2264 | 14.0 | 26.0 | -0.967 | 0.7736 |

Notes:

- Unfilled signals are never counted as trades.
- Post-fill signed mid is negative under all scenarios → adverse selection after fill.
- Conservative uses trade-through (different path than volume-through); higher fill
  count does not imply better economics.
- Exact queue unknown; optimistic/base require 1x/2x opposing notional volume while
  still competitive at the limit.

## EXECUTION_STYLES / BREAK_EVEN (required move, bps)

| style | required_move_bps |
|---|---:|
| TAKER_TAKER | 11.053 |
| MAKER_TAKER_OPTIMISTIC | 292.354 |
| MAKER_TAKER_BASE | 698.012 |
| MAKER_TAKER_CONSERVATIVE | 22.046 |
| MAKER_MAKER_OPTIMISTIC | 572.121 |
| MAKER_MAKER_BASE | 1383.565 |
| MAKER_MAKER_CONSERVATIVE | 32.072 |

TAKER_TAKER ~11.05 bps is the verified fee+spread+latency floor. Maker styles add
non-fill penalties and measured adverse selection; they are not modeled as
zero-fee taker.

## HORIZONS (`signed_trade_flow_1s` extreme ~1%)

| horizon_s | extreme_gross_bps | n |
|---:|---:|---:|
| 5 | 1.896 | 764 |
| 15 | 2.313 | 764 |
| 30 | 2.301 | 764 |
| 60 | 2.225 | 761 |
| 120 | 1.908 | 759 |
| 300 | 1.029 | 757 |
| 600 | 0.782 | 756 |

Longer-horizon p99 baselines vs ~11.05 bps taker friction: all `NOT_TRADEABLE`
(gross peaks ~1.9 bps at 120s and decays further).

## EVENT_SELECTION

- cells evaluated: 140
- TAKER_TAKER clears with ok sample: 0

Best exploratory gross remains ~2.3 bps at 15s on extreme signed trade flow —
still far below all required-move thresholds.

## OOS

- contaminated: `g_7471913_7871913`
- clean future reserved: `RESERVED_NEXT_VERIFIED_GENERATION_AFTER_g_7871913`
  (placeholder until next verified closed generation)
- inspected during selection: `False`

## PRODUCTION

```json
{
  "b2": "read-only",
  "collector_observed": "Up ~9h (healthy) at report time",
  "no_pg_historical_scan": true,
  "note": "Reported only; research did not restart/remediate. No production DROP/B2 mutation/PG historical scan.",
  "postgres_observed": "Up (healthy)"
}
```

## DECISION

`STRATEGY_RETHINK_REQUIRED`

Maker fills show adverse post-fill mid; longer horizons do not grow gross above
friction; no event class clears break-even with adequate sample. Do not use ML to
rescue a structurally negative economic setup.

## NEXT (not executed)

review alternative strategy classes or market/execution assumptions before further
predictive modeling
