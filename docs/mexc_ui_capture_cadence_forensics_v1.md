# MEXC UI capture cadence forensics v1

STATUS: `MEXC_UI_CAPTURE_CADENCE_FORENSICS_READY`

DECISION: `STOP_FOR_LEAD_REVIEW`

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false**

MOM/GAP: **not inspected** (0 of 21 cells)

Capture implementation: **unchanged**

Protocol v2.0.0: **unchanged**

Long capture: **not started**

## Purpose

Explain, as far as persisted RAW allows, why the v2-contract-gate sample
kept **297 interval** snapshots in a ~432.4 s session configured for 500 ms
(about **865** unthrottled scheduled ticks). This is forensics only.

## Capture lock

- sha256: `1f83d307cc42324f803b26c01f6a6afde5eb1dc65b938909cf50eaeb09b56f2b`
- path: `data/mexc_ui_capture/mexc_ui_capture_3c6e0474-5270-4cf0-88b0-5b0207a62006_2026-09-07T14-01-05-500Z.ndjson`
- snapshots: 994
- first/last: `2026-09-07T13:53:51.026Z` / `2026-09-07T14:01:03.449Z`
- snapshot-span duration_ms / mono_ms: 432423.0 / 432422.90000000596
- session: `2026-09-07T13:53:50.930Z` → `2026-09-07T14:01:03.458Z` (span_ms=432528.0, interval_ms=500, chunks=4, status=stopped, storage_error=None)
- trigger_counts: `{'manual': 1, 'mutation': 696, 'interval': 297}`
- expected scheduled ticks snapshot-span / session-span: 864.846 / 865.056
- observed interval snapshots: 297
- missing interval est snapshot-span / session-span: 567.846 / 568.056

## Interval-to-interval deltas

Consecutive **interval** snapshots only (mutations between them are ignored).

- wall: n=296 min=162.000 p50=512.000 p90=1157.000 p95=11226.000 p99=14982.000 max=24004.000 mean=1457.615 >1000=45 >2000=23
- monotonic: n=296 min=162.700 p50=512.000 p90=1157.300 p95=11225.600 p99=14981.700 max=24004.400 mean=1457.614 >1000=45 >2000=23

## All-trigger consecutive deltas

- wall: n=993 min=68.000 p50=113.000 p90=665.000 p95=1353.000 p99=7510.000 max=14098.000 mean=435.471 >1000=68 >2000=41
- monotonic: n=993 min=67.700 p50=112.800 p90=665.200 p95=1352.600 p99=7509.800 max=14098.600 mean=435.471 >1000=68 >2000=41

## Long gaps (consecutive snapshots)

- n >1000 ms: 68
- n >2000 ms: 41
- identical five-field values among >1000 ms: 30
- any five-field change among >1000 ms: 38
- of those changes, last-only: 38
- >1000 ms clusters: 30
- >2000 ms clusters: 27

### Clusters >2000 ms

| start_seq | end_seq | n_gaps | span_ms | snapshots |
| --- | --- | --- | --- | --- |
| 237 | 238 | 1 | 4012.0 | 2 |
| 242 | 243 | 1 | 8986.0 | 2 |
| 246 | 248 | 2 | 7342.0 | 3 |
| 249 | 252 | 3 | 12024.0 | 4 |
| 254 | 256 | 2 | 9890.0 | 3 |
| 292 | 293 | 1 | 9001.0 | 2 |
| 296 | 297 | 1 | 5995.0 | 2 |
| 302 | 304 | 2 | 6281.0 | 3 |
| 305 | 306 | 1 | 5561.0 | 2 |
| 308 | 309 | 1 | 8965.0 | 2 |
| 312 | 314 | 2 | 11855.0 | 3 |
| 318 | 321 | 3 | 13157.0 | 4 |
| 322 | 323 | 1 | 10372.0 | 2 |
| 326 | 327 | 1 | 7439.0 | 2 |
| 328 | 329 | 1 | 2780.0 | 2 |
| 330 | 331 | 1 | 10572.0 | 2 |
| 333 | 334 | 1 | 14098.0 | 2 |
| 335 | 336 | 1 | 6527.0 | 2 |
| 337 | 338 | 1 | 3510.0 | 2 |
| 339 | 340 | 1 | 2997.0 | 2 |
| 341 | 343 | 2 | 11783.0 | 3 |
| 344 | 345 | 1 | 6060.0 | 2 |
| 347 | 348 | 1 | 3196.0 | 2 |
| 351 | 352 | 1 | 10928.0 | 2 |
| 354 | 356 | 2 | 12005.0 | 3 |
| 357 | 359 | 2 | 10862.0 | 3 |
| 818 | 822 | 4 | 18920.0 | 5 |

### Every >2000 ms consecutive gap

| seq | wall_ms | mono_ms | trig_before | trig_after | chunk | fill | identical_five | clock |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 237→238 | 4012.0 | 4011.8 | interval | mutation | 0 | 237 | True | elapsed_both |
| 242→243 | 8986.0 | 8986.3 | mutation | mutation | 0 | 242 | False | elapsed_both |
| 246→247 | 3404.0 | 3404.6 | mutation | mutation | 0 | 246 | False | elapsed_both |
| 247→248 | 3938.0 | 3938.2 | mutation | mutation | 0 | 247 | True | elapsed_both |
| 249→250 | 6876.0 | 6876.7 | interval | mutation | 0 | 249 | False | elapsed_both |
| 250→251 | 2097.0 | 2096.0 | mutation | mutation | 0 | 250 | False | elapsed_both |
| 251→252 | 3051.0 | 3051.1 | mutation | interval | 1 | 1 | True | elapsed_both |
| 254→255 | 2824.0 | 2823.4 | mutation | mutation | 1 | 4 | False | elapsed_both |
| 255→256 | 7066.0 | 7066.0 | mutation | mutation | 1 | 5 | False | elapsed_both |
| 292→293 | 9001.0 | 9001.3 | interval | mutation | 1 | 42 | True | elapsed_both |
| 296→297 | 5995.0 | 5995.3 | mutation | mutation | 1 | 46 | False | elapsed_both |
| 302→303 | 2105.0 | 2105.0 | mutation | mutation | 1 | 52 | False | elapsed_both |
| 303→304 | 4176.0 | 4176.2 | mutation | mutation | 1 | 53 | False | elapsed_both |
| 305→306 | 5561.0 | 5560.9 | mutation | mutation | 1 | 55 | False | elapsed_both |
| 308→309 | 8965.0 | 8965.2 | interval | mutation | 1 | 58 | False | elapsed_both |
| 312→313 | 7510.0 | 7509.8 | mutation | mutation | 1 | 62 | False | elapsed_both |
| 313→314 | 4345.0 | 4344.9 | mutation | mutation | 1 | 63 | True | elapsed_both |
| 318→319 | 4808.0 | 4807.9 | interval | mutation | 1 | 68 | False | elapsed_both |
| 319→320 | 5480.0 | 5480.4 | mutation | mutation | 1 | 69 | False | elapsed_both |
| 320→321 | 2869.0 | 2868.1 | mutation | mutation | 1 | 70 | True | elapsed_both |
| 322→323 | 10372.0 | 10372.3 | interval | mutation | 1 | 72 | False | elapsed_both |
| 326→327 | 7439.0 | 7439.9 | mutation | mutation | 1 | 76 | False | elapsed_both |
| 328→329 | 2780.0 | 2779.2 | mutation | mutation | 1 | 78 | False | elapsed_both |
| 330→331 | 10572.0 | 10571.9 | interval | mutation | 1 | 80 | False | elapsed_both |
| 333→334 | 14098.0 | 14098.6 | interval | interval | 1 | 83 | True | elapsed_both |
| 335→336 | 6527.0 | 6526.9 | mutation | mutation | 1 | 85 | False | elapsed_both |
| 337→338 | 3510.0 | 3510.1 | mutation | mutation | 1 | 87 | False | elapsed_both |
| 339→340 | 2997.0 | 2997.1 | mutation | interval | 1 | 89 | True | elapsed_both |
| 341→342 | 8491.0 | 8491.7 | mutation | mutation | 1 | 91 | False | elapsed_both |
| 342→343 | 3292.0 | 3291.6 | mutation | mutation | 1 | 92 | True | elapsed_both |
| 344→345 | 6060.0 | 6060.5 | interval | mutation | 1 | 94 | False | elapsed_both |
| 347→348 | 3196.0 | 3196.3 | mutation | mutation | 1 | 97 | True | elapsed_both |
| 351→352 | 10928.0 | 10928.6 | mutation | mutation | 1 | 101 | True | elapsed_both |
| 354→355 | 2920.0 | 2920.1 | interval | mutation | 1 | 104 | False | elapsed_both |
| 355→356 | 9085.0 | 9084.9 | mutation | interval | 1 | 105 | True | elapsed_both |
| 357→358 | 4557.0 | 4557.6 | mutation | mutation | 1 | 107 | False | elapsed_both |
| 358→359 | 6305.0 | 6304.1 | mutation | interval | 1 | 108 | True | elapsed_both |
| 818→819 | 3249.0 | 3249.4 | mutation | mutation | 3 | 68 | False | elapsed_both |
| 819→820 | 5972.0 | 5971.8 | mutation | mutation | 3 | 69 | False | elapsed_both |
| 820→821 | 4681.0 | 4681.2 | mutation | interval | 3 | 70 | True | elapsed_both |
| 821→822 | 5018.0 | 5017.4 | interval | mutation | 3 | 71 | False | elapsed_both |

Full >1000 ms rows, mutation-density windows, and resume triggers are in the JSON.

## Resume after long gaps

- isolated next interval (±600 ms): 65
- burst (≥2 interval rows within 2500 ms): 14

## Chunk correlation

- snapshots by chunk: `{'0': 250, '1': 250, '2': 250, '3': 244}`
- left bound of >1000 ms gaps by chunk: `{'0': 16, '1': 46, '3': 6}`
- left bound of >2000 ms gaps by chunk: `{'0': 6, '1': 31, '3': 4}`
- expected >1000 left bounds if proportional to chunk size: `{'0': 17.10261569416499, '1': 17.10261569416499, '2': 17.10261569416499, '3': 16.69215291750503}`
- near chunk boundary (first/last 5 of 250): 9 / 68 (13.2%)

Random fill would put about 10/250 = 4% of sequences near those edges; a much higher fraction would suggest IDB chunk-roll correlation.

## Wall clock vs monotonic

- pairs: 993; missing monotonic: 0
- strictly increasing monotonic: True
- nonpositive mono deltas: 0
- |wall−mono| mean/median/p95: 0.347 / 0.300 / 0.800 ms
- |wall−mono| >50 / >200 / >1000 ms: 0 / 0 / 0
- >2000 ms wall clock class counts: `{'elapsed_both': 41}`

A large wall gap with a matching monotonic gap is elapsed `performance.now()` time.
Sleep/suspension is **not** inferred from matching clocks. A wall jump would need
a large wall delta with a small monotonic delta. That class is counted above.

## 30-second windows

| i | t0 | span_ms | n_all | interval | mutation | scheduled | missing_est | >1s | >2s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 2026-09-07T13:53:51.026000+00:00 | 30000 | 194 | 57 | 136 | 60.0 | 3.0 | 1 | 0 |
| 1 | 2026-09-07T13:54:21.026000+00:00 | 30000 | 48 | 23 | 25 | 60.0 | 37.0 | 8 | 2 |
| 2 | 2026-09-07T13:54:51.026000+00:00 | 30000 | 9 | 2 | 7 | 60.0 | 58.0 | 8 | 5 |
| 3 | 2026-09-07T13:55:21.026000+00:00 | 30000 | 41 | 15 | 26 | 60.0 | 45.0 | 4 | 3 |
| 4 | 2026-09-07T13:55:51.026000+00:00 | 30000 | 14 | 3 | 11 | 60.0 | 57.0 | 9 | 4 |
| 5 | 2026-09-07T13:56:21.026000+00:00 | 30000 | 12 | 4 | 8 | 60.0 | 56.0 | 7 | 4 |
| 6 | 2026-09-07T13:56:51.026000+00:00 | 30000 | 8 | 1 | 7 | 60.0 | 59.0 | 5 | 4 |
| 7 | 2026-09-07T13:57:21.026000+00:00 | 30000 | 7 | 2 | 5 | 60.0 | 58.0 | 4 | 3 |
| 8 | 2026-09-07T13:57:51.026000+00:00 | 30000 | 11 | 3 | 8 | 60.0 | 57.0 | 8 | 6 |
| 9 | 2026-09-07T13:58:21.026000+00:00 | 30000 | 11 | 4 | 7 | 60.0 | 56.0 | 6 | 4 |
| 10 | 2026-09-07T13:58:51.026000+00:00 | 30000 | 89 | 27 | 62 | 60.0 | 33.0 | 2 | 2 |
| 11 | 2026-09-07T13:59:21.026000+00:00 | 30000 | 199 | 60 | 139 | 60.0 | 0.0 | 0 | 0 |
| 12 | 2026-09-07T13:59:51.026000+00:00 | 30000 | 175 | 50 | 125 | 60.0 | 10.0 | 3 | 1 |
| 13 | 2026-09-07T14:00:21.026000+00:00 | 30000 | 81 | 21 | 60 | 60.0 | 39.0 | 3 | 3 |
| 14 | 2026-09-07T14:00:51.026000+00:00 | 12423 | 95 | 25 | 70 | 24.8 | 0.0 | 0 | 0 |

Sum of per-window missing_interval_est: 568.0

## Observational patterns (not missing telemetry)

- quiet-like long gaps (identical five, 0 mutations ±5 s): 2
- serialized-like (≥3 mutations in 150 ms before gap): 0
- timer-loss-like (interval-to-interval >500 ms **and** ≥1 mutation between those interval rows): 157

Quiet market cannot explain missing **interval** rows under the 1.3.3 heartbeat:
unchanged bid/ask/last/mark/index still commit. Mutations in a window prove the
content script was extracting DOM; sparse interval rows in the same window are
therefore not "nothing to emit".

## Findings

The ~568 missing interval rows are **time-local**, not a session-wide 1000 ms clamp.
Window 0 is near-complete (57/60). Window 11 is exact (60/60). Windows 2–9 persist
only 1–4 interval rows per 30 s while MutationObserver still fires (7–11 mutations).
Later windows do **not** overshoot 60 interval rows, so the missing ticks were not
delivered late: a drained emitChain backlog would have produced a surplus of
interval rows after the sparse period. RAW therefore favors scheduled interval
callbacks that never became persisted snapshots over quiet-market skips or
pure serialization delay.

Of 68 consecutive gaps >1000 ms, 30 keep identical bid/ask/last/mark/index and
38 change **last only**. Unchanged-field age_ms grows by about the gap length.

Chunk fill is not a smoking gun. Chunk 2 (seq 501–750) has zero >1000 ms gaps
while holding 250 snapshots. Most long gaps sit in chunk 1 because that is when
the sparse wall-clock period occurred. 9/68 left bounds are near a 250 boundary
(13.2% vs ~4% if uniform); that excess is confounded by the seq 249–252 storm.

Wall vs monotonic agree to <1 ms p95. All 41 gaps >2000 ms are `elapsed_both`.
RAW does not support a wall-clock jump. Sleep/suspension is not inferred.

What remains indistinguishable without missing telemetry: Chrome timer clamping
or background throttling, setInterval callbacks never scheduled, versus an
un-telemetred drop before persist. Visibility state is not in the snapshot.

If every setInterval callback ran, interval rows would still persist even when
emitChain delayed them, unless capturing became false or sendMessage failed
(both would stop the session). This session ended `stopped` with `storage_error=null`.

## What v1 RAW cannot infer

- `scheduled_tick_monotonic`
- `queue_wait_ms`
- `queue_depth`
- `visibility_state`
- `background_message_receipt_time`
- `idb_append_start_ms`
- `idb_append_end_ms`

Do not infer those fields. Queue wait, visibility, and IDB append duration are
absent from schema `mexc_ui_raw_snapshot` v1.

## Decision

**STOP_FOR_LEAD_REVIEW.** Do not change capture implementation in this milestone.
Do not amend protocol v2. Do not start the 8–12h corpus. Do not retune mom/gap.

