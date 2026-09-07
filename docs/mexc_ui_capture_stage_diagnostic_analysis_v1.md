# MEXC UI capture stage diagnostic analysis v1

MILESTONE: `MEXC_UI_CAPTURE_STAGE_DIAGNOSTIC_ANALYSIS_V1`

STATUS: `MEXC_UI_CAPTURE_STAGE_DIAGNOSTIC_ANALYSIS_READY`

DECISION: `STOP_FOR_LEAD_REVIEW`

ROOT_CAUSE: `MIXED_CAUSE`

Phase causes: initial_visible=`NEAR_NOMINAL`, hidden=`TIMER_RENDERER_SCHEDULING_DOMINANT`, final_visible=`CONTENT_FIFO_BACKPRESSURE_DOMINANT`

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false**

MOM/GAP: **not inspected**

Capture implementation: **unchanged**

Protocol v2.0.0: **unchanged** (can remain unchanged)

## Capture lock

- sha256: `c3699bb2261b153dda093e36e801e9f352ffb02891345c7aea1b1ff1ba6c81c2`
- path: `data/mexc_ui_capture/mexc_ui_capture_f73988c1-590c-4a92-a98c-2890b0749061_2026-09-07T16-15-40-639Z.ndjson`
- extension: `1.3.4`
- diagnostic_format_version: `1`
- operator Chrome (attested, not in RAW): `152.0.7977.82`
- session `f73988c1-590c-4a92-a98c-2890b0749061` 2026-09-07T16:03:17.046Z → 2026-09-07T16:15:37.650Z status `stopped` interval_ms=500 n_snapshots=3735
- page_path: `/futures/TAO_USDT`

## Phase reconstruction

Phases are taken only from `timer_registered`, `visibilitychange`, and `session_stop`. The intended ~2 / ~6 / ~4 minute plan is operator context and is **not** a timestamp source.

| phase | vis | start | end | ms | min | expected 500ms |
| --- | --- | --- | --- | --- | --- | --- |
| initial_visible | visible | timer_registered | visibilitychange:visible->hidden | 120636.2 | 2.01 | 241 |
| hidden | hidden | visibilitychange:visible->hidden | visibilitychange:hidden->visible | 359021.4 | 5.98 | 718 |
| final_visible | visible | visibilitychange:hidden->visible | session_stop | 260926.7 | 4.35 | 521 |

Content monotonic bounds: timer_registered=16888.7 → hidden=137524.9 → visible=496546.3 → stop=757473.0.

## Whole-session path

- expected heartbeat opportunities: 1481
- timer_callbacks counter / persisted interval: 1137 / 1135 (missing_est 346, ratio 0.766)
- interval callback_delay_ms: n=1135 min=0.1 p50=148432.4 p90=171572.7 p95=171587.1 p99=171622.5 max=171932.4
- interval callback interarrival_ms: n=1134 min=74.8 p50=524.9 p90=1002.2 p95=1009.8 p99=1082.1 max=1158.5
- content queue wait_ms: n=3735 min=0.0 p50=10.9 p90=715.9 p95=1261.2 p99=2276.7 max=2634.0
- content queue depth high-water: 26 (session summary 26)
- extract_duration_ms: n=3735 min=62.9 p50=75.8 p90=113.0 p95=138.1 p99=174.8 max=221.4
- background queue wait_ms: n=3735 min=0.0 p50=0.1 p90=0.1 p95=0.2 p99=0.3 max=2.7
- background queue depth high-water: 0 (session summary 0)
- append_duration_ms histogram (full session): n=3735 min=1.2 p50=16.0 p90=16.0 p95=32.0 p99=32.0 max=88.5
- ack_latency_ms histogram (full session): n=3735 min=2.7 p50=32.0 p90=64.0 p95=64.0 p99=128.0 max=130.9
- append_duration joined from completion ring: n=1280 min=1.2 p50=10.5 p90=14.8 p95=15.9 p99=17.8 max=42.4 (n_joined=1280)
- ack_latency joined from completion ring: n=1280 min=2.7 p50=15.6 p90=33.1 p95=40.5 p99=53.2 max=93.0 (n_joined=1280)
- Content `performance.now()` and worker `performance.now()` are never subtracted; IPC residual uses ACK minus append minus background wait on the same joined request when those fields exist.

Append substages (interval details + mutation samples; not every row):

- `meta_lookup_ms`: n=1417 min=0.40 p50=0.70 p90=0.90 p95=1.10 p99=2.70 max=3.60
- `db_open_ms`: n=1417 min=0.00 p50=0.20 p90=0.30 p95=0.40 p99=0.70 max=3.00
- `session_read_ms`: n=1417 min=0.00 p50=0.10 p90=0.20 p95=0.30 p99=0.60 max=2.00
- `chunk_read_ms`: n=1417 min=0.10 p50=1.90 p90=3.40 p95=3.80 p99=4.70 max=6.80
- `stringify_put_ms`: n=1417 min=0.00 p50=3.10 p90=5.90 p95=6.70 p99=8.10 max=15.80
- `tx_wait_ms`: n=1417 min=0.10 p50=3.80 p90=4.90 p95=5.70 p99=7.70 max=16.50
- `close_ms`: n=0 min=n/a p50=n/a p90=n/a p95=n/a p99=n/a max=n/a

## Per phase

### initial_visible (`NEAR_NOMINAL`)

- snapshots 934 (interval 240, mutation 693, manual 1)
- expected slots 241, persisted interval 240, missing_est 1, ratio 0.996
- interval ordinal 1–240; ideal slot 1–240
- callback_delay_ms: n=240 min=0.1 p50=26.1 p90=92.8 p95=105.5 p99=120.8 max=151.1
- callback interarrival_ms: n=239 min=358.1 p50=499.1 p90=562.9 p95=579.2 p99=599.9 max=649.3
- content wait_ms: n=934 min=0.0 p50=10.9 p90=221.9 p95=300.5 p99=418.8 max=545.6
- interval-only content wait_ms: n=240 min=0.0 p50=6.4 p90=203.5 p95=263.9 p99=418.8 max=528.3
- content depth high-water: 5
- extract_duration_ms: n=934 min=62.9 p50=73.6 p90=80.5 p95=84.2 p99=97.5 max=139.1
- background wait_ms: n=934 min=0.0 p50=0.1 p90=0.1 p95=0.2 p99=0.3 max=0.8
- background depth high-water: 0
- append_duration joined: n=934 min=1.2 p50=10.6 p90=15.4 p95=16.3 p99=18.6 max=42.4 (n=934)
- append_timings_sum_ms: n=403 min=1.2 p50=9.8 p90=14.3 p95=15.4 p99=16.6 max=26.6
- ack_latency joined: n=934 min=3.7 p50=17.4 p90=33.1 p95=40.1 p99=53.6 max=93.0 (n=934)

### hidden (`TIMER_RENDERER_SCHEDULING_DOMINANT`)

- snapshots 733 (interval 375, mutation 358, manual 0)
- expected slots 718, persisted interval 375, missing_est 343, ratio 0.522
- interval ordinal 241–615; ideal slot 241–958
- callback_delay_ms: n=375 min=162.8 p50=87438.0 p90=153434.9 p95=162930.2 p99=170590.1 max=171932.4
- callback interarrival_ms: n=374 min=74.8 p50=999.3 p90=1014.6 p95=1057.4 p99=1124.3 max=1158.5
- content wait_ms: n=733 min=0.0 p50=0.1 p90=16.7 p95=120.4 p99=198.8 max=279.8
- interval-only content wait_ms: n=375 min=0.0 p50=0.1 p90=6.5 p95=11.3 p99=158.0 max=196.6
- content depth high-water: 3
- extract_duration_ms: n=733 min=67.9 p50=113.2 p90=157.3 p95=175.5 p99=200.9 max=221.4
- background wait_ms: n=733 min=0.0 p50=0.0 p90=0.1 p95=0.1 p99=0.2 max=0.4
- background depth high-water: 0
- append_duration joined: n=346 min=1.7 p50=10.5 p90=13.9 p95=14.4 p99=15.8 max=21.4 (n=346)
- append_timings_sum_ms: n=397 min=1.6 p50=9.7 p90=13.4 p95=13.9 p99=15.6 max=19.0
- ack_latency joined: n=346 min=2.7 p50=13.2 p90=32.5 p95=40.9 p99=52.2 max=75.8 (n=346)

### final_visible (`CONTENT_FIFO_BACKPRESSURE_DOMINANT`)

- snapshots 2068 (interval 520, mutation 1548, manual 0)
- expected slots 521, persisted interval 520, missing_est 1, ratio 0.998
- interval ordinal 616–1135; ideal slot 959–1478
- callback_delay_ms: n=520 min=171500.0 p50=171532.1 p90=171588.8 p95=171603.0 p99=171624.6 max=171910.9
- callback interarrival_ms: n=519 min=116.8 p50=500.2 p90=556.8 p95=566.8 p99=599.5 max=660.5
- content wait_ms: n=2068 min=0.0 p50=94.3 p90=1180.7 p95=1525.7 p99=2333.7 max=2634.0
- interval-only content wait_ms: n=520 min=0.0 p50=11.1 p90=1006.1 p95=1444.6 p99=2301.7 max=2555.2
- content depth high-water: 26
- extract_duration_ms: n=2068 min=63.8 p50=74.7 p90=82.6 p95=85.9 p99=97.0 max=126.3
- background wait_ms: n=2068 min=0.0 p50=0.1 p90=0.1 p95=0.2 p99=0.3 max=2.7
- background depth high-water: 0
- append_duration joined: n=0 min=n/a p50=n/a p90=n/a p95=n/a p99=n/a max=n/a (n=0)
- append_timings_sum_ms: n=617 min=1.6 p50=11.2 p90=15.8 p95=16.7 p99=19.6 max=29.0
- ack_latency joined: n=0 min=n/a p50=n/a p90=n/a p95=n/a p99=n/a max=n/a (n=0)

## Visible vs hidden

callback_delay_ms = callback_mono - (timer_registered + ordinal*500). Hidden ~1 s delivery makes ordinal lag the 500 ms grid, so delay stays ~minutes in the later visible phase even though interarrival returns to ~500 ms. Do not treat that leftover offset as continued timer failure after unhide.

- interval interarrival p50 ms: initial_visible 499.1, hidden 999.3, final_visible 500.2
- conservation ratio: initial_visible 0.996, hidden 0.522, final_visible 0.998
- content wait p95 ms / depth HW: hidden 120.4 / 3; final_visible 1525.7 / 26

Hidden delivery is a ~1 s background timer clamp when conservation drops and queue wait stays small. After unhide, ~500 ms callbacks can return while mutation traffic lengthens content wait.

## Reconciliation, fencing, lifecycle, truncation

- enqueued 3745, started 3735, persisted 3735, acked 3734, abandoned 0, outstanding_at_stop 11
- Stop does not drain emitChain. outstanding_at_stop counts waiting+active at the Stop flag; abandoned stays 0 if those tasks had not yet settled when the content diagnostic delta was flushed.
- producer_epochs (1): `['6acb0185-805a-4a8f-bc93-d0b5b6b7ad92']`
- worker_boot_ids (1): `['e2e9bc0d-888d-49e9-aea9-5d8660f40b82']`
- stale_generation rows: 0; session_id_mismatch rows: 0
- lifecycle: visibilitychange=2, pageshow=1, pagehide=0, freeze=0, resume=0, worker_boot=1
- No `freeze` / `resume` / `pagehide` during the running session. `pageshow` is the content-script load (session_generation 0).
- diagnostic_truncated=True: suppressed mutation details 2317, slow examples 1040, completions 4910 (unique ordinals 1280, max 1280); interval details 1135, lifecycle suppressed 0

Counters and histograms still cover the full session after those caps. Do not treat the completion ring as a complete ACK join.

## Raw-observation gaps

- n >1000 ms: 60; n >2000 ms: 0; max 1059.6 ms
- primary attribution >1000 ms: `{'delayed_or_missing_timer_callback': 58, 'unknown': 2}`
- primary attribution >2000 ms: `{}`

Attribution uses the closing request's own wait/extract/callback gap/slot jump. Percentiles from different stages are never added together.

| seq_b | seq_a | phase | gap_ms | primary | wait | extract | slot |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 955 | 956 | hidden | 1018.9 | delayed_or_missing_timer_callback | 0.1 | 96.9 | 2 |
| 961 | 962 | hidden | 1003.2 | delayed_or_missing_timer_callback | 0.0 | 101.6 | 2 |
| 964 | 965 | hidden | 1012.3 | delayed_or_missing_timer_callback | 0.0 | 99.0 | 2 |
| 967 | 968 | hidden | 1013.4 | delayed_or_missing_timer_callback | 0.1 | 102.1 | 2 |
| 974 | 975 | hidden | 1016.3 | delayed_or_missing_timer_callback | 0.0 | 113.0 | 2 |
| 977 | 978 | hidden | 1024.1 | delayed_or_missing_timer_callback | 0.0 | 100.9 | 2 |
| 986 | 987 | hidden | 1020.7 | delayed_or_missing_timer_callback | 0.1 | 105.6 | 2 |
| 989 | 990 | hidden | 1015.2 | delayed_or_missing_timer_callback | 0.1 | 97.9 | 2 |
| 996 | 997 | hidden | 1019.5 | delayed_or_missing_timer_callback | 0.1 | 101.6 | 2 |
| 1001 | 1002 | hidden | 1017.8 | delayed_or_missing_timer_callback | 0.0 | 97.9 | 2 |
| 1006 | 1007 | hidden | 1033.2 | delayed_or_missing_timer_callback | 0.1 | 101.0 | 2 |
| 1020 | 1021 | hidden | 1011.5 | delayed_or_missing_timer_callback | 0.1 | 99.7 | 2 |
| 1043 | 1044 | hidden | 1019.2 | delayed_or_missing_timer_callback | 0.1 | 94.7 | 2 |
| 1051 | 1052 | hidden | 1043.7 | delayed_or_missing_timer_callback | 0.2 | 120.8 | 2 |
| 1055 | 1056 | hidden | 1003.7 | delayed_or_missing_timer_callback | 0.1 | 103.3 | 2 |
| 1062 | 1063 | hidden | 1025.1 | delayed_or_missing_timer_callback | 0.1 | 109.4 | 2 |
| 1066 | 1067 | hidden | 1013.5 | delayed_or_missing_timer_callback | 0.1 | 106.5 | 2 |
| 1079 | 1080 | hidden | 1016.0 | delayed_or_missing_timer_callback | 0.0 | 99.5 | 2 |
| 1124 | 1125 | hidden | 1007.2 | delayed_or_missing_timer_callback | 0.0 | 105.7 | 2 |
| 1146 | 1147 | hidden | 1006.0 | delayed_or_missing_timer_callback | 0.1 | 111.6 | 2 |
| 1149 | 1150 | hidden | 1006.4 | delayed_or_missing_timer_callback | 0.0 | 111.5 | 2 |
| 1171 | 1172 | hidden | 1013.1 | delayed_or_missing_timer_callback | 0.0 | 117.5 | 2 |
| 1182 | 1183 | hidden | 1006.3 | delayed_or_missing_timer_callback | 0.1 | 102.0 | 2 |
| 1189 | 1190 | hidden | 1036.1 | delayed_or_missing_timer_callback | 0.0 | 133.4 | 2 |
| 1212 | 1213 | hidden | 1026.4 | delayed_or_missing_timer_callback | 0.0 | 113.2 | 2 |
| 1224 | 1225 | hidden | 1004.8 | delayed_or_missing_timer_callback | 0.1 | 110.0 | 2 |
| 1232 | 1233 | hidden | 1027.0 | delayed_or_missing_timer_callback | 0.1 | 101.9 | 2 |
| 1235 | 1236 | hidden | 1015.1 | delayed_or_missing_timer_callback | 0.1 | 114.9 | 2 |
| 1238 | 1239 | hidden | 1008.0 | delayed_or_missing_timer_callback | 0.0 | 100.9 | 2 |
| 1241 | 1242 | hidden | 1004.0 | delayed_or_missing_timer_callback | 0.1 | 101.5 | 2 |
| 1248 | 1249 | hidden | 1046.4 | delayed_or_missing_timer_callback | 0.1 | 114.8 | 2 |
| 1263 | 1264 | hidden | 1055.2 | delayed_or_missing_timer_callback | 0.1 | 120.4 | 2 |
| 1266 | 1267 | hidden | 1022.8 | unknown | 0.0 | 117.1 | None |
| 1290 | 1291 | hidden | 1006.3 | delayed_or_missing_timer_callback | 0.2 | 101.6 | 2 |
| 1293 | 1294 | hidden | 1006.4 | delayed_or_missing_timer_callback | 0.0 | 100.1 | 2 |
| 1296 | 1297 | hidden | 1013.6 | delayed_or_missing_timer_callback | 0.0 | 116.0 | 2 |
| 1311 | 1312 | hidden | 1012.3 | delayed_or_missing_timer_callback | 0.0 | 112.8 | 2 |
| 1314 | 1315 | hidden | 1039.4 | delayed_or_missing_timer_callback | 0.4 | 119.0 | 2 |
| 1317 | 1318 | hidden | 1001.2 | delayed_or_missing_timer_callback | 0.1 | 102.6 | 2 |
| 1347 | 1348 | hidden | 1002.0 | delayed_or_missing_timer_callback | 0.0 | 94.9 | 2 |
| 1400 | 1401 | hidden | 1000.8 | delayed_or_missing_timer_callback | 0.1 | 101.8 | 2 |
| 1409 | 1410 | hidden | 1011.8 | delayed_or_missing_timer_callback | 0.1 | 112.4 | 2 |
| 1412 | 1413 | hidden | 1017.9 | delayed_or_missing_timer_callback | 0.2 | 114.1 | 2 |
| 1416 | 1417 | hidden | 1005.4 | delayed_or_missing_timer_callback | 0.0 | 100.6 | 2 |
| 1428 | 1429 | hidden | 1005.1 | delayed_or_missing_timer_callback | 0.1 | 102.6 | 2 |
| 1431 | 1432 | hidden | 1024.8 | delayed_or_missing_timer_callback | 0.1 | 114.4 | 2 |
| 1434 | 1435 | hidden | 1019.3 | delayed_or_missing_timer_callback | 0.1 | 115.5 | 2 |
| 1463 | 1464 | hidden | 1027.5 | delayed_or_missing_timer_callback | 0.0 | 114.6 | 2 |
| 1467 | 1468 | hidden | 1002.6 | delayed_or_missing_timer_callback | 0.0 | 103.2 | 2 |
| 1476 | 1477 | hidden | 1049.8 | unknown | 0.0 | 118.3 | None |
| 1480 | 1481 | hidden | 1021.5 | delayed_or_missing_timer_callback | 0.1 | 118.0 | 2 |
| 1512 | 1513 | hidden | 1036.7 | delayed_or_missing_timer_callback | 0.1 | 116.8 | 2 |
| 1528 | 1529 | hidden | 1044.6 | delayed_or_missing_timer_callback | 0.0 | 117.3 | 2 |
| 1532 | 1533 | hidden | 1013.0 | delayed_or_missing_timer_callback | 0.1 | 115.3 | 2 |
| 1554 | 1555 | hidden | 1037.2 | delayed_or_missing_timer_callback | 0.0 | 129.5 | 2 |
| 1574 | 1575 | hidden | 1016.2 | delayed_or_missing_timer_callback | 0.2 | 102.3 | 2 |
| 1577 | 1578 | hidden | 1013.6 | delayed_or_missing_timer_callback | 0.0 | 118.6 | 2 |
| 1599 | 1600 | hidden | 1016.6 | delayed_or_missing_timer_callback | 0.0 | 113.6 | 2 |
| 1629 | 1630 | hidden | 1001.4 | delayed_or_missing_timer_callback | 0.1 | 105.2 | 2 |
| 1639 | 1640 | hidden | 1059.6 | delayed_or_missing_timer_callback | 0.1 | 141.4 | 2 |

## Root cause

**MIXED_CAUSE.** Hidden phase is `TIMER_RENDERER_SCHEDULING_DOMINANT` (conservation 0.522, interval interarrival p50 999.3 ms, wait p95 120.4 ms, depth 3). Final visible is `CONTENT_FIFO_BACKPRESSURE_DOMINANT` (conservation 0.998, interarrival p50 500.2 ms, wait p95 1525.7 ms, depth 26). Initial visible is `NEAR_NOMINAL`.

Storage, extraction, and service-worker restart are not dominant when background depth stays 0, extract remains well under 250 ms, and append histogram max stays tens of milliseconds.

## Remediation recommendation (not implemented)

Supported next implementation, smallest first:

- interval-only raw observations / mutations as dirty notifications
- heartbeat-priority + mutation coalescing

- Hidden-phase interval delivery is ~1 Hz (Chrome timer clamp). Heartbeat-priority, mutation coalescing, persistence decoupling, and IDB batching cannot create timer callbacks a hidden renderer does not fire.
- Final visible phase restores ~500 ms callback interarrival while the content FIFO depth and wait grow with mutation traffic. Smallest justified change is to stop optional mutation snapshots from sharing the ACK-bound FIFO with the heartbeat (candidate D, or A if mutation rows must remain).
- IndexedDB append stays tens of milliseconds with background depth 0, so observation/persistence decoupling and batched IDB are not the measured cause and are not recommended from this run.

## Protocol v2

Protocol v2.0.0 can remain unchanged. received_at_local stays extract-time. Do not backdate to timer deadlines, exclude the hidden phase, or relax p95/p99 bars to manufacture a pass.

## Decision

**STOP_FOR_LEAD_REVIEW.** Do not change capture code in this milestone. Do not start the 8–12 h corpus. Do not retune mom/gap. Do not start ML, PAPER, or LIVE.

