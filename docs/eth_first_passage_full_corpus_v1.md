# ETH first-passage full-corpus expansion v1

STATUS: `ETH_FIRST_PASSAGE_FULL_CORPUS_EXPANSION_READY`
ML_STATUS: `NOT_STARTED`
DECISION: `STOP_FOR_LEAD_REVIEW`

## Method

Repeats the frozen first-passage protocol from
`docs/eth_first_passage_opportunity_v1.md` without changing horizons,
thresholds, or path semantics. Mid MFE, executable TOB MFE, first
passage, time-to-hit, MAE before first TP, rolling 1s, and four-offset
non-overlap are unchanged. This expansion adds a live B2 COMPLETED
inventory, an explicit discovery / untouched-OOS / future-holdout split,
exact per-offset counts, UTC-day stability, and `movement_episode_v1`.
OOS is not used to choose horizon or TP. No TP×SL grid.

## B2 inventory (read-only)

- listing status: `LIVE_COMPLETED_MARKERS`
- COMPLETED ETH windows: `216`
- quarantined windows: `56`
- quality pass windows: `153`
- UTC dates: `['2026-07-29', '2026-07-30', '2026-07-31', '2026-08-01', '2026-08-05', '2026-08-06', '2026-08-07', '2026-08-09', '2026-08-10', '2026-08-11', '2026-08-12', '2026-08-13', '2026-08-14', '2026-08-17', '2026-08-18', '2026-08-19', '2026-08-20']`
- B2 mutations: `False`
- credential files loaded (names only): `['b2.env']`

Per-UTC-day eligible hours (non-quarantined COMPLETED overlap):

| UTC date | windows | quarantined | pass | eligible hours | full (≥23h) |
|---|---:|---:|---:|---:|---|
| 2026-07-29 | 13 | 0 | 13 | 12.87 | no |
| 2026-07-30 | 24 | 1 | 23 | 23.00 | yes |
| 2026-07-31 | 14 | 0 | 10 | 13.39 | no |
| 2026-08-01 | 7 | 1 | 3 | 5.03 | no |
| 2026-08-05 | 5 | 0 | 5 | 4.85 | no |
| 2026-08-06 | 23 | 0 | 23 | 22.09 | no |
| 2026-08-07 | 10 | 0 | 10 | 9.56 | no |
| 2026-08-09 | 1 | 0 | 1 | 1.00 | no |
| 2026-08-10 | 8 | 0 | 8 | 8.00 | no |
| 2026-08-11 | 14 | 2 | 12 | 12.00 | no |
| 2026-08-12 | 23 | 14 | 9 | 9.00 | no |
| 2026-08-13 | 24 | 10 | 14 | 14.00 | no |
| 2026-08-14 | 9 | 6 | 3 | 3.00 | no |
| 2026-08-17 | 7 | 4 | 3 | 3.00 | no |
| 2026-08-18 | 24 | 18 | 6 | 6.00 | no |
| 2026-08-19 | 6 | 0 | 6 | 6.00 | no |
| 2026-08-20 | 4 | 0 | 4 | 4.00 | no |

## Corpus split (timestamps only; proposed to lead)

- v1 untouched OOS dates: `['2026-08-07', '2026-08-09', '2026-08-10']`
- expanded discovery dates: `['2026-07-29', '2026-07-30', '2026-07-31', '2026-08-01', '2026-08-05', '2026-08-06', '2026-08-11', '2026-08-12', '2026-08-13', '2026-08-14', '2026-08-17', '2026-08-18', '2026-08-19', '2026-08-20']`
- expanded discovery windows / hours est: `141` / `138.23`
- new final holdout dates: `[]`
- new holdout applied: `False`
- thin holdout alternative (not applied): `['2026-08-19', '2026-08-20']`
- lead alerts: `['NEW_FINAL_HOLDOUT_UNAVAILABLE_NO_TWO_FULL_LATER_UTC_DAYS']`

v1 OOS dates stay untouched. Later verified COMPLETED windows are expanded discovery unless a predeclared full-day holdout exists. No price-path statistics entered this decision. Do not use OOS or holdout to choose horizon/TP.

- materialized discovery usable hours: `107.86444444444444`
- discovery rows: `388312`
- first-passage materialized for holdout: `False`

## Cost layer (not mixed into movement stats)

- taker RT reference: `11.0` bps
- taker RT observed: `11.053767126731724` bps
- median spread: `0.05324629339839128` bps

## Mid either-side hit fraction (rolling 1s, dependent)

| H | n | 5bps | 10 | 15 | 20 | 50 | 100 | MFE p50/p95 abs |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 5s | 388149 | 1.56% | 0.38% | 0.21% | 0.14% | 0.05% | 0.04% | 0.03/3.04 |
| 10s | 387989 | 4.22% | 0.88% | 0.42% | 0.27% | 0.08% | 0.06% | 0.32/4.70 |
| 15s | 387829 | 7.40% | 1.53% | 0.64% | 0.41% | 0.12% | 0.07% | 0.80/6.04 |
| 30s | 387376 | 16.84% | 4.14% | 1.58% | 0.82% | 0.20% | 0.11% | 1.87/9.18 |
| 60s | 386476 | 32.84% | 10.42% | 4.23% | 2.02% | 0.33% | 0.17% | 3.33/13.98 |
| 120s | 384676 | 54.95% | 22.61% | 10.37% | 5.41% | 0.63% | 0.23% | 5.57/20.74 |
| 180s | 382876 | 68.35% | 33.74% | 16.38% | 9.14% | 1.02% | 0.29% | 7.29/26.21 |
| 300s | 379276 | 82.28% | 50.00% | 27.83% | 16.46% | 2.12% | 0.45% | 10.00/35.69 |
| 600s | 370376 | 94.04% | 72.74% | 49.42% | 32.10% | 5.64% | 0.99% | 14.86/52.55 |

## Executable TOB either-side (non-overlap): percent plus exact counts

Each offset cell is `hits/n_valid`. Pooled sums are dependent (four
phases of the same path) and are descriptive only. hits/24h is shown
beside the observed offset-0 count, never instead of it.

| H | TP | off0 | off H/4 | off H/2 | off 3H/4 | pooled hits/n (dependent) | hits/24h |
|---|---:|---:|---:|---:|---:|---:|---:|
| 5s | 5 | 632/77640 | 605/77637 | 630/77634 | 611/77624 | 2478/310535 | 137.89 |
| 5s | 10 | 102/77640 | 89/77637 | 93/77634 | 89/77624 | 373/310535 | 20.76 |
| 5s | 15 | 36/77640 | 35/77637 | 35/77634 | 42/77624 | 148/310535 | 8.24 |
| 5s | 20 | 20/77640 | 19/77637 | 17/77634 | 24/77624 | 80/310535 | 4.45 |
| 5s | 25 | 13/77640 | 12/77637 | 10/77634 | 13/77624 | 48/310535 | 2.67 |
| 5s | 30 | 11/77640 | 9/77637 | 7/77634 | 10/77624 | 37/310535 | 2.06 |
| 5s | 40 | 6/77640 | 6/77637 | 4/77634 | 6/77624 | 22/310535 | 1.22 |
| 5s | 50 | 5/77640 | 4/77637 | 4/77634 | 5/77624 | 18/310535 | 1.00 |
| 5s | 75 | 1/77640 | 2/77637 | 3/77634 | 4/77624 | 10/310535 | 0.56 |
| 5s | 100 | 1/77640 | 1/77637 | 2/77634 | 3/77624 | 7/310535 | 0.39 |
| 10s | 5 | 1121/38811 | 1100/38809 | 1120/38797 | 1121/38793 | 4462/155210 | 248.38 |
| 10s | 10 | 168/38811 | 172/38809 | 176/38797 | 175/38793 | 691/155210 | 38.47 |
| 10s | 15 | 66/38811 | 68/38809 | 74/38797 | 68/38793 | 276/155210 | 15.36 |
| 10s | 20 | 34/38811 | 33/38809 | 38/38797 | 35/38793 | 140/155210 | 7.79 |
| 10s | 25 | 21/38811 | 19/38809 | 29/38797 | 20/38793 | 89/155210 | 4.95 |
| 10s | 30 | 11/38811 | 15/38809 | 18/38797 | 15/38793 | 59/155210 | 3.28 |
| 10s | 40 | 9/38811 | 8/38809 | 11/38797 | 10/38793 | 38/155210 | 2.12 |
| 10s | 50 | 9/38811 | 5/38809 | 7/38797 | 7/38793 | 28/155210 | 1.56 |
| 10s | 75 | 4/38811 | 5/38809 | 3/38797 | 5/38793 | 17/155210 | 0.95 |
| 10s | 100 | 3/38811 | 5/38809 | 3/38797 | 4/38793 | 15/155210 | 0.83 |
| 15s | 5 | 1421/25868 | 1445/25862 | 1458/25856 | 1425/25848 | 5749/103434 | 320.15 |
| 15s | 10 | 257/25868 | 249/25862 | 244/25856 | 254/25848 | 1004/103434 | 55.91 |
| 15s | 15 | 85/25868 | 86/25862 | 92/25856 | 91/25848 | 354/103434 | 19.71 |
| 15s | 20 | 43/25868 | 42/25862 | 53/25856 | 47/25848 | 185/103434 | 10.30 |
| 15s | 25 | 31/25868 | 31/25862 | 27/25856 | 33/25848 | 122/103434 | 6.79 |
| 15s | 30 | 23/25868 | 16/25862 | 20/25856 | 27/25848 | 86/103434 | 4.79 |
| 15s | 40 | 16/25868 | 14/25862 | 12/25856 | 16/25848 | 58/103434 | 3.23 |
| 15s | 50 | 9/25868 | 8/25862 | 8/25856 | 7/25848 | 32/103434 | 1.78 |
| 15s | 75 | 4/25868 | 5/25862 | 5/25856 | 6/25848 | 20/103434 | 1.11 |
| 15s | 100 | 3/25868 | 5/25862 | 5/25856 | 6/25848 | 19/103434 | 1.06 |
| 30s | 5 | 1803/12926 | 1826/12920 | 1822/12911 | 1808/12906 | 7259/51663 | 404.66 |
| 30s | 10 | 394/12926 | 405/12920 | 419/12911 | 428/12906 | 1646/51663 | 91.76 |
| 30s | 15 | 130/12926 | 135/12920 | 153/12911 | 150/12906 | 568/51663 | 31.66 |
| 30s | 20 | 58/12926 | 69/12920 | 70/12911 | 65/12906 | 262/51663 | 14.61 |
| 30s | 25 | 36/12926 | 41/12920 | 49/12911 | 37/12906 | 163/51663 | 9.09 |
| 30s | 30 | 25/12926 | 31/12920 | 34/12911 | 26/12906 | 116/51663 | 6.47 |
| 30s | 40 | 20/12926 | 20/12920 | 19/12911 | 17/12906 | 76/51663 | 4.24 |
| 30s | 50 | 13/12926 | 13/12920 | 14/12911 | 12/12906 | 52/51663 | 2.90 |
| 30s | 75 | 7/12926 | 7/12920 | 7/12911 | 7/12906 | 28/51663 | 1.56 |
| 30s | 100 | 5/12926 | 5/12920 | 5/12911 | 4/12906 | 19/51663 | 1.06 |
| 60s | 5 | 1906/6456 | 1906/6446 | 1895/6440 | 1917/6435 | 7624/25777 | 425.91 |
| 60s | 10 | 561/6456 | 600/6446 | 587/6440 | 568/6435 | 2316/25777 | 129.38 |
| 60s | 15 | 215/6456 | 243/6446 | 225/6440 | 225/6435 | 908/25777 | 50.73 |
| 60s | 20 | 96/6456 | 102/6446 | 102/6440 | 99/6435 | 399/25777 | 22.29 |
| 60s | 25 | 52/6456 | 62/6446 | 60/6440 | 54/6435 | 228/25777 | 12.74 |
| 60s | 30 | 35/6456 | 39/6446 | 41/6440 | 38/6435 | 153/25777 | 8.55 |
| 60s | 40 | 22/6456 | 23/6446 | 23/6440 | 17/6435 | 85/25777 | 4.75 |
| 60s | 50 | 14/6456 | 18/6446 | 15/6440 | 12/6435 | 59/25777 | 3.30 |
| 60s | 75 | 6/6456 | 7/6446 | 8/6440 | 6/6435 | 27/25777 | 1.51 |
| 60s | 100 | 4/6456 | 5/6446 | 6/6440 | 6/6435 | 21/25777 | 1.17 |
| 120s | 5 | 1648/3221 | 1687/3211 | 1643/3205 | 1636/3199 | 6614/12836 | 370.99 |
| 120s | 10 | 673/3221 | 664/3211 | 660/3205 | 680/3199 | 2677/12836 | 150.16 |
| 120s | 15 | 300/3221 | 294/3211 | 288/3205 | 303/3199 | 1185/12836 | 66.47 |
| 120s | 20 | 152/3221 | 148/3211 | 157/3205 | 157/3199 | 614/12836 | 34.44 |
| 120s | 25 | 79/3221 | 90/3211 | 93/3205 | 87/3199 | 349/12836 | 19.58 |
| 120s | 30 | 51/3221 | 58/3211 | 59/3205 | 59/3199 | 227/12836 | 12.73 |
| 120s | 40 | 28/3221 | 27/3211 | 25/3205 | 29/3199 | 109/12836 | 6.11 |
| 120s | 50 | 14/3221 | 12/3211 | 15/3205 | 15/3199 | 56/12836 | 3.14 |
| 120s | 75 | 8/3221 | 5/3211 | 7/3205 | 9/3199 | 29/12836 | 1.63 |
| 120s | 100 | 5/3221 | 5/3211 | 5/3205 | 6/3199 | 21/12836 | 1.18 |
| 180s | 5 | 1404/2141 | 1412/2133 | 1379/2127 | 1421/2121 | 5616/8522 | 316.33 |
| 180s | 10 | 677/2141 | 688/2133 | 673/2127 | 669/2121 | 2707/8522 | 152.47 |
| 180s | 15 | 331/2141 | 348/2133 | 317/2127 | 313/2121 | 1309/8522 | 73.72 |
| 180s | 20 | 179/2141 | 194/2133 | 177/2127 | 172/2121 | 722/8522 | 40.66 |
| 180s | 25 | 94/2141 | 106/2133 | 112/2127 | 118/2121 | 430/8522 | 24.23 |
| 180s | 30 | 60/2141 | 66/2133 | 71/2127 | 73/2121 | 270/8522 | 15.21 |
| 180s | 40 | 29/2141 | 29/2133 | 36/2127 | 37/2121 | 131/8522 | 7.38 |
| 180s | 50 | 17/2141 | 13/2133 | 18/2127 | 20/2121 | 68/8522 | 3.83 |
| 180s | 75 | 8/2141 | 7/2133 | 6/2127 | 10/2121 | 31/8522 | 1.75 |
| 180s | 100 | 4/2141 | 5/2133 | 5/2127 | 4/2121 | 18/8522 | 1.01 |
| 300s | 5 | 1031/1281 | 1041/1269 | 1022/1262 | 1018/1258 | 4112/5070 | 233.58 |
| 300s | 10 | 611/1281 | 624/1269 | 609/1262 | 600/1258 | 2444/5070 | 138.83 |
| 300s | 15 | 342/1281 | 338/1269 | 346/1262 | 319/1258 | 1345/5070 | 76.40 |
| 300s | 20 | 194/1281 | 201/1269 | 202/1262 | 186/1258 | 783/5070 | 44.48 |
| 300s | 25 | 120/1281 | 129/1269 | 141/1262 | 123/1258 | 513/5070 | 29.15 |
| 300s | 30 | 87/1281 | 92/1269 | 90/1262 | 89/1258 | 358/5070 | 20.34 |
| 300s | 40 | 44/1281 | 35/1269 | 38/1262 | 47/1258 | 164/5070 | 9.32 |
| 300s | 50 | 20/1281 | 24/1269 | 24/1262 | 27/1258 | 95/5070 | 5.40 |
| 300s | 75 | 4/1281 | 12/1269 | 9/1262 | 10/1258 | 35/5070 | 1.99 |
| 300s | 100 | 3/1281 | 5/1269 | 6/1262 | 5/1258 | 19/5070 | 1.08 |
| 600s | 5 | 592/633 | 585/623 | 577/618 | 570/609 | 2324/2483 | 134.78 |
| 600s | 10 | 453/633 | 448/623 | 432/618 | 445/609 | 1778/2483 | 103.12 |
| 600s | 15 | 307/633 | 293/623 | 299/618 | 303/609 | 1202/2483 | 69.72 |
| 600s | 20 | 191/633 | 199/623 | 196/618 | 198/609 | 784/2483 | 45.48 |
| 600s | 25 | 129/633 | 137/623 | 130/618 | 128/609 | 524/2483 | 30.39 |
| 600s | 30 | 93/633 | 92/623 | 94/618 | 93/609 | 372/2483 | 21.58 |
| 600s | 40 | 49/633 | 48/623 | 51/618 | 51/609 | 199/2483 | 11.55 |
| 600s | 50 | 33/633 | 30/623 | 30/618 | 30/609 | 123/2483 | 7.13 |
| 600s | 75 | 8/633 | 10/623 | 12/618 | 12/609 | 42/2483 | 2.44 |
| 600s | 100 | 4/633 | 5/623 | 4/618 | 7/609 | 20/2483 | 1.16 |

## Time-to-hit (mid, rolling, either-side, seconds)

| H | TP | p25 | p50 | p75 | p90 |
|---|---:|---:|---:|---:|---:|
| 15s | 10 | 6.00 | 9.00 | 13.00 | 14.00 |
| 15s | 20 | 4.00 | 8.00 | 12.00 | 14.00 |
| 15s | 50 | 3.00 | 7.00 | 11.00 | 14.00 |
| 60s | 10 | 22.00 | 36.00 | 48.00 | 56.00 |
| 60s | 20 | 19.00 | 36.00 | 49.00 | 56.00 |
| 60s | 50 | 10.00 | 24.00 | 42.00 | 52.00 |
| 300s | 10 | 70.00 | 133.00 | 203.00 | 258.00 |
| 300s | 20 | 99.00 | 166.00 | 235.00 | 274.00 |
| 300s | 50 | 105.00 | 185.00 | 249.00 | 281.00 |
| 600s | 10 | 97.00 | 195.00 | 343.00 | 476.00 |
| 600s | 20 | 162.00 | 293.00 | 439.00 | 531.00 |
| 600s | 50 | 232.00 | 369.00 | 478.00 | 555.00 |

## MAE before first TP (executable, non-overlap pooled, bps)

| H | TP | long p50/p75/p90 | short p50/p75/p90 |
|---|---:|---|---|
| 15s | 10 | 0.53/4.27/13.74 | 0.32/2.79/15.15 |
| 15s | 20 | 6.07/15.89/28.79 | 6.51/28.71/261.13 |
| 15s | 50 | 12.89/23.55/44.26 | 11.98/48.09/112.79 |
| 60s | 10 | 0.37/2.15/5.45 | 0.37/1.68/4.33 |
| 60s | 20 | 1.71/6.74/21.61 | 0.88/4.93/25.75 |
| 60s | 50 | 7.97/36.23/79.36 | 22.99/245.38/274.83 |
| 300s | 10 | 1.10/4.10/8.01 | 0.80/3.11/7.34 |
| 300s | 20 | 1.53/5.13/10.00 | 1.20/3.97/9.65 |
| 300s | 50 | 3.19/17.39/26.33 | 0.84/4.94/38.65 |
| 600s | 10 | 2.31/5.85/10.90 | 1.80/5.46/10.95 |
| 600s | 20 | 2.38/6.24/11.95 | 2.00/6.62/12.46 |
| 600s | 50 | 2.38/6.11/23.67 | 3.29/8.35/15.53 |

## Day/block stability (executable non-overlap hit fraction)

Predeclared slice: H ∈ {60,120,300,600}s and TP ∈ {10,15,20,25,30} bps.
Question: is 2026-08-06 a typical regime or an anomaly?

### Distribution across UTC days

| cell | n_days | min | median | max |
|---|---:|---:|---:|---:|
| 120s_10bps | 14 | 0.00% | 16.49% | 46.41% |
| 120s_15bps | 14 | 0.00% | 6.10% | 29.16% |
| 120s_20bps | 14 | 0.00% | 2.25% | 22.70% |
| 120s_25bps | 14 | 0.00% | 1.09% | 18.59% |
| 120s_30bps | 14 | 0.00% | 0.39% | 15.07% |
| 300s_10bps | 14 | 0.00% | 45.60% | 81.82% |
| 300s_15bps | 14 | 0.00% | 22.06% | 57.98% |
| 300s_20bps | 14 | 0.00% | 11.13% | 43.53% |
| 300s_25bps | 14 | 0.00% | 5.24% | 38.04% |
| 300s_30bps | 14 | 0.00% | 3.08% | 31.53% |
| 600s_10bps | 14 | 0.00% | 69.98% | 96.83% |
| 600s_15bps | 14 | 0.00% | 46.01% | 86.04% |
| 600s_20bps | 14 | 0.00% | 25.16% | 71.00% |
| 600s_25bps | 14 | 0.00% | 17.14% | 58.80% |
| 600s_30bps | 14 | 0.00% | 9.79% | 50.63% |
| 60s_10bps | 14 | 0.00% | 5.94% | 27.02% |
| 60s_15bps | 14 | 0.00% | 1.92% | 17.88% |
| 60s_20bps | 14 | 0.00% | 0.40% | 13.41% |
| 60s_25bps | 14 | 0.00% | 0.17% | 11.08% |
| 60s_30bps | 14 | 0.00% | 0.07% | 8.75% |

### Per UTC day (executable either-side mean % @ TP 10/15/20/25/30)

| date | hours | segs | 60s@20 | 120s@20 | 300s@20 | 600s@20 | 60s@10 | 600s@30 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-07-29 | 12.86 | 4 | 4.23% | 14.51% | 37.67% | 59.67% | 23.26% | 36.98% |
| 2026-07-30 | 23.00 | 2 | 0.51% | 2.36% | 12.12% | 27.53% | 6.62% | 11.75% |
| 2026-07-31 | 13.38 | 3 | 0.94% | 3.26% | 14.42% | 29.36% | 7.44% | 12.57% |
| 2026-08-01 | 5.01 | 4 | 0.00% | 0.17% | 1.29% | 8.84% | 1.59% | 0.86% |
| 2026-08-05 | 4.85 | 2 | 0.26% | 0.52% | 3.98% | 13.87% | 3.02% | 3.67% |
| 2026-08-06 ← v1 discovery | 22.09 | 3 | 0.34% | 1.86% | 7.05% | 20.43% | 4.42% | 5.59% |
| 2026-08-11 | 9.90 | 2 | 0.46% | 2.03% | 11.12% | 28.56% | 6.59% | 11.25% |
| 2026-08-12 | 4.01 | 4 | 1.05% | 2.99% | 11.15% | 28.89% | 5.16% | 13.18% |
| 2026-08-13 | 1.19 | 2 | 0.00% | 0.00% | 5.95% | 22.14% | 0.36% | 0.00% |
| 2026-08-14 | 0.44 | 2 | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| 2026-08-17 | 0.70 | 1 | 0.00% | 2.50% | 12.95% | 20.83% | 5.49% | 8.33% |
| 2026-08-18 | 2.36 | 1 | 0.18% | 2.14% | 8.20% | 22.80% | 6.40% | 3.85% |
| 2026-08-19 | 4.32 | 3 | 13.41% | 22.70% | 43.53% | 67.10% | 27.02% | 50.63% |
| 2026-08-20 | 3.75 | 1 | 3.35% | 13.46% | 40.34% | 71.00% | 20.20% | 43.02% |

Raw per-day cells for the full stability slice, including exact per-offset
counts, are in the JSON under `day_block_stability.per_utc_day`.
Contiguous 1s blocks are under `per_contiguous_block`.

## movement_episode_v1 (diagnostic, does not replace non-overlap)

One episode is a run of neighboring rolling starts of the same direction that all hit the TP inside the frozen horizon. This approximates distinct excursions better than overlapping window hits, but it is still window-based (not unique first-touch timestamps). No extra cooldown was introduced.

| H | TP | mid long ep | mid short | exec long | exec short | mid long /day |
|---|---:|---:|---:|---:|---:|---:|
| 60s | 10 | 793 | 760 | 721 | 699 | 176.44 |
| 60s | 15 | 380 | 335 | 332 | 304 | 84.55 |
| 60s | 20 | 181 | 203 | 157 | 159 | 40.27 |
| 60s | 25 | 109 | 112 | 90 | 89 | 24.25 |
| 60s | 30 | 67 | 77 | 63 | 57 | 14.91 |
| 120s | 10 | 986 | 932 | 949 | 863 | 219.39 |
| 120s | 15 | 505 | 445 | 458 | 423 | 112.36 |
| 120s | 20 | 285 | 257 | 259 | 247 | 63.41 |
| 120s | 25 | 175 | 176 | 164 | 167 | 38.94 |
| 120s | 30 | 101 | 130 | 105 | 115 | 22.47 |
| 300s | 10 | 981 | 1029 | 944 | 998 | 218.27 |
| 300s | 15 | 666 | 600 | 619 | 588 | 148.19 |
| 300s | 20 | 427 | 388 | 407 | 375 | 95.01 |
| 300s | 25 | 282 | 265 | 275 | 257 | 62.75 |
| 300s | 30 | 199 | 196 | 190 | 188 | 44.28 |
| 600s | 10 | 833 | 908 | 820 | 907 | 185.34 |
| 600s | 15 | 671 | 651 | 652 | 650 | 149.30 |
| 600s | 20 | 513 | 445 | 510 | 445 | 114.14 |
| 600s | 25 | 360 | 338 | 348 | 334 | 80.10 |
| 600s | 30 | 228 | 265 | 245 | 248 | 50.73 |

## Aug-6-only vs expanded discovery

Compare published v1 (Aug 6, 11.64h, OOS preserved) with the fuller Aug-6 restore and with all expanded-discovery days. Stable cells are those whose hit fraction stays near v1 and near the cross-day median. Sample artifacts are cells where v1's small n or a single-day regime drove hits/24h or a rare-TP percent. Grids stay frozen; none of this is a trading rule.

| cell | v1 Aug-6-only (11.64h) | expanded Aug-6 subset | expanded discovery |
|---|---:|---:|---:|
| 60s_20bps | 0.25% (3.62/24h, pooled_hits=None) | 0.34% (4.90/24h, pooled_hits=18) | 1.55% (22.29/24h, pooled_hits=399) |
| 120s_20bps | 2.02% (14.51/24h, pooled_hits=None) | 1.86% (13.37/24h, pooled_hits=49) | 4.78% (34.44/24h, pooled_hits=614) |
| 300s_20bps | 8.35% (24.04/24h, pooled_hits=None) | 7.05% (20.31/24h, pooled_hits=74) | 15.44% (44.48/24h, pooled_hits=783) |
| 600s_20bps | 23.90% (34.42/24h, pooled_hits=None) | 20.43% (29.42/24h, pooled_hits=106) | 31.59% (45.48/24h, pooled_hits=784) |
| 60s_10bps | 5.64% (81.15/24h, pooled_hits=None) | 4.42% (63.70/24h, pooled_hits=234) | 8.98% (129.38/24h, pooled_hits=2316) |
| 600s_30bps | 6.25% (8.99/24h, pooled_hits=None) | 5.59% (8.05/24h, pooled_hits=29) | 14.99% (21.58/24h, pooled_hits=372) |

### What looks stable / changed / sample artifact

- 60s_20bps: expanded discovery hit fraction moved 516% relative to v1 Aug-6-only (0.0025 → 0.0155). Treat the v1 point estimate as a sample artifact until day-level min/median/max agree.
- 60s_20bps: Aug 6 (0.0034) is near the cross-day median (0.0040); not an obvious anomaly.
- 120s_20bps: expanded discovery hit fraction moved 137% relative to v1 Aug-6-only (0.0202 → 0.0478). Treat the v1 point estimate as a sample artifact until day-level min/median/max agree.
- 120s_20bps: Aug 6 (0.0186) is near the cross-day median (0.0225); not an obvious anomaly.
- 300s_20bps: expanded discovery hit fraction moved 85% relative to v1 Aug-6-only (0.0835 → 0.1544). Treat the v1 point estimate as a sample artifact until day-level min/median/max agree.
- 300s_20bps: Aug 6 (0.0705) is near the cross-day median (0.1113); not an obvious anomaly.
- 600s_20bps: expanded discovery hit fraction moved 32% relative to v1 Aug-6-only (0.2390 → 0.3159). Treat the v1 point estimate as a sample artifact until day-level min/median/max agree.
- 600s_20bps: Aug 6 (0.2043) is near the cross-day median (0.2516); not an obvious anomaly.
- 60s_10bps: expanded discovery hit fraction moved 59% relative to v1 Aug-6-only (0.0564 → 0.0898). Treat the v1 point estimate as a sample artifact until day-level min/median/max agree.
- 60s_10bps: Aug 6 (0.0442) is near the cross-day median (0.0594); not an obvious anomaly.
- 600s_30bps: expanded discovery hit fraction moved 140% relative to v1 Aug-6-only (0.0625 → 0.1499). Treat the v1 point estimate as a sample artifact until day-level min/median/max agree.
- 600s_30bps: Aug 6 (0.0559) is near the cross-day median (0.0979); not an obvious anomaly.
- v1 discovery was 11.64h on Aug 6 only; expanded Aug-6 subset is 22.09h and expanded discovery is 107.86h. Larger n can change rare-TP counts even when the regime is similar.
- Aug 6 raw stability row is in day_block_stability.per_utc_day (marked in the Markdown table).

## Frequency vs friction (not predictability)

Predeclared display band unchanged: executable TP ≥ taker RT reference and
non-overlap mean hits/24h ≥ 1.0.
Not a fitted threshold and not a trading signal.

- 5s TP 15.0 bps: ~8.24 non-overlap windows/24h (frequency only).
- 5s TP 20.0 bps: ~4.45 non-overlap windows/24h (frequency only).
- 5s TP 25.0 bps: ~2.67 non-overlap windows/24h (frequency only).
- 5s TP 30.0 bps: ~2.06 non-overlap windows/24h (frequency only).
- 5s TP 40.0 bps: ~1.22 non-overlap windows/24h (frequency only).
- 5s TP 50.0 bps: ~1.00 non-overlap windows/24h (frequency only).
- 10s TP 15.0 bps: ~15.36 non-overlap windows/24h (frequency only).
- 10s TP 20.0 bps: ~7.79 non-overlap windows/24h (frequency only).
- 10s TP 25.0 bps: ~4.95 non-overlap windows/24h (frequency only).
- 10s TP 30.0 bps: ~3.28 non-overlap windows/24h (frequency only).
- 10s TP 40.0 bps: ~2.12 non-overlap windows/24h (frequency only).
- 10s TP 50.0 bps: ~1.56 non-overlap windows/24h (frequency only).
- 15s TP 15.0 bps: ~19.71 non-overlap windows/24h (frequency only).
- 15s TP 20.0 bps: ~10.30 non-overlap windows/24h (frequency only).
- 15s TP 25.0 bps: ~6.79 non-overlap windows/24h (frequency only).
- 15s TP 30.0 bps: ~4.79 non-overlap windows/24h (frequency only).
- 15s TP 40.0 bps: ~3.23 non-overlap windows/24h (frequency only).
- 15s TP 50.0 bps: ~1.78 non-overlap windows/24h (frequency only).
- 15s TP 75.0 bps: ~1.11 non-overlap windows/24h (frequency only).
- 15s TP 100.0 bps: ~1.06 non-overlap windows/24h (frequency only).
- 30s TP 15.0 bps: ~31.66 non-overlap windows/24h (frequency only).
- 30s TP 20.0 bps: ~14.61 non-overlap windows/24h (frequency only).
- 30s TP 25.0 bps: ~9.09 non-overlap windows/24h (frequency only).
- 30s TP 30.0 bps: ~6.47 non-overlap windows/24h (frequency only).
- 30s TP 40.0 bps: ~4.24 non-overlap windows/24h (frequency only).
- 30s TP 50.0 bps: ~2.90 non-overlap windows/24h (frequency only).
- 30s TP 75.0 bps: ~1.56 non-overlap windows/24h (frequency only).
- 30s TP 100.0 bps: ~1.06 non-overlap windows/24h (frequency only).
- 60s TP 15.0 bps: ~50.73 non-overlap windows/24h (frequency only).
- 60s TP 20.0 bps: ~22.29 non-overlap windows/24h (frequency only).
- 60s TP 25.0 bps: ~12.74 non-overlap windows/24h (frequency only).
- 60s TP 30.0 bps: ~8.55 non-overlap windows/24h (frequency only).
- 60s TP 40.0 bps: ~4.75 non-overlap windows/24h (frequency only).
- 60s TP 50.0 bps: ~3.30 non-overlap windows/24h (frequency only).
- 60s TP 75.0 bps: ~1.51 non-overlap windows/24h (frequency only).
- 60s TP 100.0 bps: ~1.17 non-overlap windows/24h (frequency only).
- 120s TP 15.0 bps: ~66.47 non-overlap windows/24h (frequency only).
- 120s TP 20.0 bps: ~34.44 non-overlap windows/24h (frequency only).
- 120s TP 25.0 bps: ~19.58 non-overlap windows/24h (frequency only).
- 120s TP 30.0 bps: ~12.73 non-overlap windows/24h (frequency only).
- 120s TP 40.0 bps: ~6.11 non-overlap windows/24h (frequency only).
- 120s TP 50.0 bps: ~3.14 non-overlap windows/24h (frequency only).
- 120s TP 75.0 bps: ~1.63 non-overlap windows/24h (frequency only).
- 120s TP 100.0 bps: ~1.18 non-overlap windows/24h (frequency only).
- 180s TP 15.0 bps: ~73.72 non-overlap windows/24h (frequency only).
- 180s TP 20.0 bps: ~40.66 non-overlap windows/24h (frequency only).
- 180s TP 25.0 bps: ~24.23 non-overlap windows/24h (frequency only).
- 180s TP 30.0 bps: ~15.21 non-overlap windows/24h (frequency only).
- 180s TP 40.0 bps: ~7.38 non-overlap windows/24h (frequency only).
- 180s TP 50.0 bps: ~3.83 non-overlap windows/24h (frequency only).
- 180s TP 75.0 bps: ~1.75 non-overlap windows/24h (frequency only).
- 180s TP 100.0 bps: ~1.01 non-overlap windows/24h (frequency only).
- 300s TP 15.0 bps: ~76.40 non-overlap windows/24h (frequency only).
- 300s TP 20.0 bps: ~44.48 non-overlap windows/24h (frequency only).
- 300s TP 25.0 bps: ~29.15 non-overlap windows/24h (frequency only).
- 300s TP 30.0 bps: ~20.34 non-overlap windows/24h (frequency only).
- 300s TP 40.0 bps: ~9.32 non-overlap windows/24h (frequency only).
- 300s TP 50.0 bps: ~5.40 non-overlap windows/24h (frequency only).
- 300s TP 75.0 bps: ~1.99 non-overlap windows/24h (frequency only).
- 300s TP 100.0 bps: ~1.08 non-overlap windows/24h (frequency only).
- 600s TP 15.0 bps: ~69.72 non-overlap windows/24h (frequency only).
- 600s TP 20.0 bps: ~45.48 non-overlap windows/24h (frequency only).
- 600s TP 25.0 bps: ~30.39 non-overlap windows/24h (frequency only).
- 600s TP 30.0 bps: ~21.58 non-overlap windows/24h (frequency only).
- 600s TP 40.0 bps: ~11.55 non-overlap windows/24h (frequency only).
- 600s TP 50.0 bps: ~7.13 non-overlap windows/24h (frequency only).
- 600s TP 75.0 bps: ~2.44 non-overlap windows/24h (frequency only).
- 600s TP 100.0 bps: ~1.16 non-overlap windows/24h (frequency only).

## Runtime

- wall seconds: `7730.597`
- peak RSS MiB: `1327.484375`
- tracemalloc peak MiB: `851.114`

## Data-quality blockers

- DROPPED_5_ROWS_IN_DOCUMENTED_COLLECTION_GAPS_OR_INVALID_TOB

## Stop

STOP_FOR_LEAD_REVIEW. No ML, no feature selection, no PAPER, no live trading.
Do not retune first-passage grids from these results.

