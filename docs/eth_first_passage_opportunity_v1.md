# ETH first-passage opportunity v1

STATUS: `ETH_FIRST_PASSAGE_OPPORTUNITY_REASSESSMENT_READY`
ML_STATUS: `NOT_STARTED`

## Method

Previous opportunity analysis (`opportunity_base_rate.py`) used **endpoint**
`mid[t+h] / mid[t]` only. A path that reached +20 bps and returned to 0 at
the horizon was recorded as no move. This protocol measures **intrawindow
maximum excursion** and **threshold first passage**. Mid numbers are not
tradeable PnL. Executable TOB and the ~11 bps taker RT reference are
separate layers. Horizons and thresholds were frozen before results.

## Corpus

- discovery UTC dates: `['2026-08-06']`
- discovery usable hours: `11.640833333333333`
- discovery usable days (hours/24): `0.485`
- untouched OOS UTC dates: `['2026-08-07', '2026-08-09', '2026-08-10']`
- untouched OOS rows: `58580`
- untouched OOS hours: `16.272222222222222`
- lead alerts: `['DISCOVERY_THIN_OOS_PRESERVED']`

## Cost layer (not mixed into movement stats)

- taker RT reference: `11.0` bps
- taker RT observed: `11.05300091058396` bps
- median spread: `0.052480077250625985` bps

## Mid either-side hit fraction (rolling 1s, dependent)

| H | n | 5bps | 10 | 15 | 20 | 50 | 100 | MFE p50/p95 abs |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 5s | 41897 | 0.65% | 0.04% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00/2.56 |
| 10s | 41887 | 2.30% | 0.15% | 0.01% | 0.00% | 0.00% | 0.00% | 0.16/3.87 |
| 15s | 41877 | 4.56% | 0.36% | 0.04% | 0.01% | 0.00% | 0.00% | 0.58/4.87 |
| 30s | 41847 | 12.18% | 1.78% | 0.28% | 0.04% | 0.00% | 0.00% | 1.60/7.15 |
| 60s | 41787 | 27.19% | 6.13% | 1.20% | 0.36% | 0.00% | 0.00% | 2.93/10.67 |
| 120s | 41667 | 48.07% | 16.57% | 5.23% | 2.05% | 0.00% | 0.00% | 4.78/15.19 |
| 180s | 41547 | 61.14% | 27.05% | 9.86% | 4.22% | 0.00% | 0.00% | 6.34/18.46 |
| 300s | 41307 | 77.28% | 42.35% | 19.53% | 9.35% | 0.11% | 0.00% | 8.81/23.94 |
| 600s | 40707 | 91.62% | 63.66% | 38.95% | 22.92% | 1.11% | 0.00% | 12.58/33.39 |

## Executable TOB either-side hit fraction (non-overlap mean of 4 offsets)

| H | 5bps | 10 | 15 | 20 | 50 | 100 | hits/24h @20bps |
|---|---:|---:|---:|---:|---:|---:|---:|
| 5s | 0.47% | 0.04% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00 |
| 10s | 2.02% | 0.14% | 0.01% | 0.00% | 0.00% | 0.00% | 0.00 |
| 15s | 4.08% | 0.36% | 0.03% | 0.00% | 0.00% | 0.00% | 0.00 |
| 30s | 11.09% | 1.49% | 0.27% | 0.02% | 0.00% | 0.00% | 0.52 |
| 60s | 25.48% | 5.64% | 1.08% | 0.25% | 0.00% | 0.00% | 3.62 |
| 120s | 46.51% | 16.05% | 4.68% | 2.02% | 0.00% | 0.00% | 14.51 |
| 180s | 61.04% | 27.27% | 9.64% | 4.11% | 0.00% | 0.00% | 19.75 |
| 300s | 75.14% | 42.29% | 20.14% | 8.35% | 0.18% | 0.00% | 24.04 |
| 600s | 91.92% | 63.98% | 40.78% | 23.90% | 1.46% | 0.00% | 34.42 |

## Time-to-hit (mid, rolling, either-side, seconds)

| H | TP | p25 | p50 | p75 | p90 |
|---|---:|---:|---:|---:|---:|
| 15s | 10 | 8.00 | 11.00 | 14.00 | 15.00 |
| 15s | 20 | 13.50 | 14.00 | 14.50 | 14.80 |
| 15s | 50 | — | — | — | — |
| 60s | 10 | 28.00 | 41.00 | 51.00 | 57.00 |
| 60s | 20 | 36.00 | 43.00 | 55.00 | 58.00 |
| 60s | 50 | — | — | — | — |
| 300s | 10 | 87.00 | 148.00 | 211.00 | 264.00 |
| 300s | 20 | 129.00 | 193.50 | 253.00 | 281.00 |
| 300s | 50 | 259.75 | 270.50 | 278.25 | 293.30 |
| 600s | 10 | 117.25 | 214.00 | 363.75 | 489.00 |
| 600s | 20 | 220.00 | 351.00 | 456.00 | 543.00 |
| 600s | 50 | 389.25 | 470.00 | 544.00 | 580.10 |

## MAE before first TP (executable, non-overlap pooled, bps)

| H | TP | long p50/p75/p90 | short p50/p75/p90 |
|---|---:|---|---|
| 15s | 10 | 0.05/0.87/1.24 | 0.05/0.05/0.16 |
| 15s | 20 | —/—/— | —/—/— |
| 15s | 50 | —/—/— | —/—/— |
| 60s | 10 | 0.05/0.38/1.45 | 0.05/0.32/1.16 |
| 60s | 20 | 0.05/0.18/0.48 | —/—/— |
| 60s | 50 | —/—/— | —/—/— |
| 300s | 10 | 0.73/3.20/5.92 | 0.18/1.93/4.63 |
| 300s | 20 | 0.74/2.39/5.93 | 0.31/2.26/5.07 |
| 300s | 50 | 0.16/0.16/0.16 | —/—/— |
| 600s | 10 | 2.36/5.25/9.69 | 0.76/3.68/6.73 |
| 600s | 20 | 1.94/6.84/11.18 | 0.55/2.06/4.61 |
| 600s | 50 | 0.45/3.35/8.05 | —/—/— |

## Rolling vs non-overlap

Rolling hit counts are overlapping 1s candidates. Do not convert them to
trades/day. Non-overlap uses stride H with offsets 0, H/4, H/2, 3H/4;
tables above use the mean hit fraction across offsets (min/max in JSON).
hits/24 usable hours extrapolate the observed sample rate.

## Frequency vs friction (not predictability)

Predeclared display band: executable TP ≥ taker RT reference and
non-overlap mean hits/24h ≥ 1.0.
This is not a fitted threshold and not a trading signal.

- 15s TP 15.0 bps: ~1.55 non-overlap windows/24h (frequency only).
- 30s TP 15.0 bps: ~7.74 non-overlap windows/24h (frequency only).
- 60s TP 15.0 bps: ~15.51 non-overlap windows/24h (frequency only).
- 60s TP 20.0 bps: ~3.62 non-overlap windows/24h (frequency only).
- 120s TP 15.0 bps: ~33.69 non-overlap windows/24h (frequency only).
- 120s TP 20.0 bps: ~14.51 non-overlap windows/24h (frequency only).
- 120s TP 25.0 bps: ~2.59 non-overlap windows/24h (frequency only).
- 120s TP 30.0 bps: ~1.04 non-overlap windows/24h (frequency only).
- 180s TP 15.0 bps: ~46.26 non-overlap windows/24h (frequency only).
- 180s TP 20.0 bps: ~19.75 non-overlap windows/24h (frequency only).
- 180s TP 25.0 bps: ~7.29 non-overlap windows/24h (frequency only).
- 180s TP 30.0 bps: ~3.12 non-overlap windows/24h (frequency only).
- 300s TP 15.0 bps: ~58.02 non-overlap windows/24h (frequency only).
- 300s TP 20.0 bps: ~24.04 non-overlap windows/24h (frequency only).
- 300s TP 25.0 bps: ~12.55 non-overlap windows/24h (frequency only).
- 300s TP 30.0 bps: ~6.28 non-overlap windows/24h (frequency only).
- 300s TP 40.0 bps: ~1.56 non-overlap windows/24h (frequency only).
- 600s TP 15.0 bps: ~58.72 non-overlap windows/24h (frequency only).
- 600s TP 20.0 bps: ~34.42 non-overlap windows/24h (frequency only).
- 600s TP 25.0 bps: ~18.51 non-overlap windows/24h (frequency only).
- 600s TP 30.0 bps: ~8.99 non-overlap windows/24h (frequency only).
- 600s TP 40.0 bps: ~2.10 non-overlap windows/24h (frequency only).
- 600s TP 50.0 bps: ~2.10 non-overlap windows/24h (frequency only).

## Runtime

- wall seconds: `359.056`
- peak RSS MiB: `275.87109375`
- tracemalloc peak MiB: `91.98`

## Data-quality blockers

- LIVE_B2_INVENTORY_UNAVAILABLE: LIVE_LISTING_FAILED (ValueError). Analysis uses locally restored verified market_state_1s from the existing offline pipeline, not a fresh B2 listing.

## Stop

No ML, no signal selection, no Binance lead-lag, no PAPER. Report only.

