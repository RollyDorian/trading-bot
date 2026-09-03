# MEXC TAO long observation v1

STATUS: `MEXC_TAO_LONG_OBSERVATION_READY_WITH_FINDINGS`

DECISION: `STOP_FOR_LEAD_REVIEW`

ML_STATUS: `NOT_STARTED`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false** (frozen `author_observed_v0` / `conservative_v0`)

Prior milestone: `docs/mexc_ui_extension_e2e_and_long_capture_v1.md`

## Purpose

Describe the real 8–12 hour unpacked-extension `TAOUSDT` capture. Profiles
stay frozen. No ML, PAPER, live execution, or mom/gap/exit retune.

Invalid observations **before** the first executable market-data print after
each session start/reload are `STARTUP_WARMUP`, not selector failure. They
remain in the raw NDJSON. Readiness begins only when catalog-required fields
(symbol, bid, ask) normalize to a valid Observation. Any missing or corrupt
executable BBO **after** readiness is `DATA_INVALID` and is **not**
forward-filled.

## Capture file

- path: `data/mexc_ui_capture/mexc_ui_capture_sessions_2026-09-03T04-12-21-619Z.ndjson`
- name: `mexc_ui_capture_sessions_2026-09-03T04-12-21-619Z.ndjson`
- sha256: `7a41c34d4ae855850cd8a1a47e438e940c38e09d6f5a555c3f397e5650da9c2a`
- bytes: 437759548
- duration hours: 11.6702
- 8–12h window (8h–12h30m): **yes**

Raw capture is gitignored under `data/mexc_ui_capture/`. This report is the
lead artifact.

## Decision

**STOP_FOR_LEAD_REVIEW.** Do not start ML or PAPER. Do not retune frozen
profiles from this session. `STARTUP_WARMUP` is pre-readiness invalid, not a
selector fail. `DATA_INVALID` after readiness is not forward-filled.

### Findings

- Raw symbol field is missing on every snapshot. Replay identity was recovered from page_path /ru-RU/futures/TAO_USDT. NDJSON was not rewritten.
- mark (Fair Price) is missing on every snapshot. English header labels likely did not match the ru-RU UI. mid-mark / mark-index stats are empty.
- index is missing on every snapshot. Same locale-label issue as mark.
- orderbook_heading_count=0 on every row; executable BBO is 100% live_asks_bids_wrapper.
- HYPOTHESIS_SMOKE produced 0 candidates and 0 trades. Frozen author_observed_v0 gap is mid_vs_mark and mark is absent. This is not a reason to retune mom/gap/exits.
- Parsed last/mid sit near 21811–21875 while Phase A TAO was ~216. Wrapper bid/ask raw_text has no decimal; last uses a comma. bps stats are reported in native parsed units without rescaling.

## Capture quality

| Metric | Value |
| --- | --- |
| snapshots | 162075 |
| READY_VALID | 162075 (100.0000%) |
| STARTUP_WARMUP | 0 (0.0000%) |
| DATA_INVALID | 0 (0.0000%) |
| sessions | 1 |
| chunks | 649 |
| sequence diagnostics | none |
| storage errors | none |
| crossed BBO (`n_bid_ge_ask`) | 0 |
| simultaneous bid+ask+mark+index | 0 |
| simultaneous bid+ask+last+mark+index | 0 |
| timing_adequacy | `MISSING_MARK_INDEX_OR_BBO` |
| replay canonical sha256 | `dbad19b0b3877639cee09fc825a22338a69fbff9d0441ed0697b7f92e1b1316c` |
| trigger mix | {'manual': 1, 'mutation': 161671, 'interval': 403} |
| first ready | {'session_id': 'bc4819c7-6035-4078-8620-e6b6dd122c7b', 'sequence': 1, 'received_at_local': '2026-09-02T16:32:02.198Z'} |

Interarrival (all snapshots, including warmup): `n=162074, p50=176.0, p90=552.0, p95=735.0, p99=1071.0, min=58.0, max=2067.0`.

Selector `chosen_bbo_source`: live_asks_bids_wrapper=162075.

### Sessions

| session_id | started_at | ended_at | n_snapshots | chunks | status | storage_error |
| --- | --- | --- | --- | --- | --- | --- |
| `bc4819c7-6035-4078-8620-e6b6dd122c7b` | 2026-09-02T16:32:02.130Z | 2026-09-03T04:12:14.999Z | 162075 | 649 | stopped | None |

### Interruptions

| kind | from | to | gap_ms |
| --- | --- | --- | --- |
| none | — | — | — |

### Warmup / DATA_INVALID bursts

| class | n | seq | duration_ms | start_t |
| --- | --- | --- | --- | --- |
| (none) | 0 | — | — | — |

Warmup reasons: `{}`

DATA_INVALID reasons: `{}`

Invalid reasons (unclassified mix of skipped_reason + snapshot flags):
`{}`

Field age (ms): see JSON `capture_quality.field_age_ms`.

## Descriptive market dynamics

Not a trading rule. Executable mid is present only on `READY_VALID` rows.
`STARTUP_WARMUP` / `DATA_INVALID` contribute `mid=None` so a missing BBO is
not replaced by the previous mid.

Start/end: `{'first_t': '2026-09-02T16:32:02.198000+00:00', 'last_t': '2026-09-03T04:12:14.993000+00:00', 'first': {'last': 21811.0, 'mid': 21811.5, 'mark': None, 'index': None}, 'last': {'last': 21872.0, 'mid': 21875.5, 'mark': None, 'index': None}}`

Spread (bps, ready executable): `n=162075, mean=1.1536, std=0.6737, p50=1.3760, p90=1.8448, p95=2.3121, p99=3.2408, min=0.4569, max=10.6410`

| gap | distribution |
| --- | --- |
| mid−mark | n=0 |
| mid−index | n=0 |
| mark−index | n=0 |

### Horizon returns (bps)

Pairs skipped when either endpoint lacks that field (no fill).

| H | last | mid | mark | index |
| --- | --- | --- | --- | --- |
| 1s | n=162073 p50=0.000 ≥1/2/3/5/10bps=14470/8552/5217/1325/160 | n=162073 p50=0.000 ≥1/2/3/5/10bps=19182/9490/3462/956/132 | n=0 p50=n/a ≥1/2/3/5/10bps=0/0/0/0/0 | n=0 p50=n/a ≥1/2/3/5/10bps=0/0/0/0/0 |
| 2s | n=162071 p50=0.000 ≥1/2/3/5/10bps=25246/15138/9259/2469/285 | n=162071 p50=0.000 ≥1/2/3/5/10bps=31945/16653/6702/1936/247 | n=0 p50=n/a ≥1/2/3/5/10bps=0/0/0/0/0 | n=0 p50=n/a ≥1/2/3/5/10bps=0/0/0/0/0 |
| 5s | n=162061 p50=0.000 ≥1/2/3/5/10bps=52955/32968/20844/6539/770 | n=162061 p50=0.000 ≥1/2/3/5/10bps=63470/35001/17644/5928/700 | n=0 p50=n/a ≥1/2/3/5/10bps=0/0/0/0/0 | n=0 p50=n/a ≥1/2/3/5/10bps=0/0/0/0/0 |
| 10s | n=162058 p50=0.000 ≥1/2/3/5/10bps=86014/57477/38803/15125/1928 | n=162058 p50=0.000 ≥1/2/3/5/10bps=90364/58957/36134/14930/1927 | n=0 p50=n/a ≥1/2/3/5/10bps=0/0/0/0/0 | n=0 p50=n/a ≥1/2/3/5/10bps=0/0/0/0/0 |
| 30s | n=162034 p50=0.000 ≥1/2/3/5/10bps=122090/98996/78744/47104/11231 | n=162034 p50=0.000 ≥1/2/3/5/10bps=123816/98300/76648/46153/11259 | n=0 p50=n/a ≥1/2/3/5/10bps=0/0/0/0/0 | n=0 p50=n/a ≥1/2/3/5/10bps=0/0/0/0/0 |
| 60s | n=162006 p50=0.000 ≥1/2/3/5/10bps=136556/119564/103850/74499/27366 | n=162006 p50=0.000 ≥1/2/3/5/10bps=137014/118662/101459/72991/26741 | n=0 p50=n/a ≥1/2/3/5/10bps=0/0/0/0/0 | n=0 p50=n/a ≥1/2/3/5/10bps=0/0/0/0/0 |

### Lead/lag cross-correlation (1s grid, lags −5…+5 s)

Positive lag k is corr(x[t], y[t+k]). Peak |corr| per pair:

| pair | peak lag s | corr |
| --- | --- | --- |
| `last_vs_mark` | None | n/a |
| `last_vs_index` | None | n/a |
| `mark_vs_index` | None | n/a |
| `mid_vs_last` | 0 | 0.3558 |
| `mid_vs_mark` | None | n/a |

Full lag maps: JSON `descriptive_market.lead_lag_xcorr`.

## HYPOTHESIS_SMOKE (not performance)

Frozen `author_observed_v0` only. Candidate/trade counts and any PnL figures are **HYPOTHESIS_SMOKE** only. They are not strategy evidence and must not be used to retune mom/gap/exit/threshold/throttle/sizing.

| item | value |
| --- | --- |
| label | `HYPOTHESIS_SMOKE` |
| observations | 162075 |
| n_candidates | 0 |
| n_accepted_for_shadow | 0 |
| n_trades | 0 |
| n_open | 0 |
| throttle | {} |
| exits | {} |
| trade gross_bps | n=0 |

Replay ok: **True**. Export sha256 is of the NDJSON
bytes; replay sha256 is of canonical valid observations. Repeating the
canonical hash on the same file is deterministic.

## What was not done

- No mom/gap/exit/threshold/throttle/sizing change
- No ML, PAPER, or live orders
- No B2 upload of this capture
- Raw warmup/invalid rows were not deleted or rewritten
