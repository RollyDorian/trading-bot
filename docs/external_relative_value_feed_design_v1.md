# External relative-value feed design review v1

STATUS: `EXTERNAL_FEED_DESIGN_READY`

ML_STATUS: `BLOCKED`

IMPLEMENTATION_IN_THIS_MILESTONE: **false**

DEPLOYMENT_IN_THIS_MILESTONE: **false**

## Purpose

Design the smallest safe **public** external ETH market-data feed that can
falsify or support:

> external liquid venue price discovery leads Hibachi → temporary executable lag
> → convergence on Hibachi TOB mid

This document is a **human-approval package**. It does not implement, enable, or
deploy a second collector.

Durable Hibachi-only negative results remain closed (short-horizon directional
microstructure, maker rescue, same-family longer horizons). Do not reopen them
via model complexity.

---

## 1. Testable hypothesis

### H1 — Arrival-time lead-lag (trading-causal)

Let \(E_t\) be an external **executable** mid or aggressive trade impulse observed
at local `received_at = t` on the research/pilot host.

Over horizons \(\Delta \in \{250\,\mathrm{ms}, 500\,\mathrm{ms}, 1\,\mathrm{s}, 2\,\mathrm{s}, 5\,\mathrm{s}\}\):

1. Hibachi executable mid (from Hibachi TOB) moves in the **same signed direction**
   after \(t\) more than chance;
2. The **gross** Hibachi executable move, conditional on a sparse external impulse,
   is **materially above** Hibachi all-in friction for the proposed execution
   style (default exploratory gate: TAKER_TAKER ≈ **11.05 bps** unless a
   separately justified style is predeclared);
3. Evidence uses **only information available after local receipt** (no
   exchange-time lookahead into Hibachi decisions).

Sub-100 ms horizons are **diagnostic-only** unless clock + network evidence later
supports them. Initial trustworthy trading inference target: **≥250 ms**, with
**~100 ms** treated as the optimistic lower bound of VPS clock/network quality
(see Clock).

### H1-null (reject feed family)

If arrival-time analysis shows Hibachi co-moves with no exploitable lag residual,
or residual gross stays below the predeclared economic gate across chronological
blocks, **REJECT** `EXTERNAL_RELATIVE_VALUE_LEAD_LAG` for this venue/instrument.

### Non-goals

- Not hedged cross-venue arbitrage (two executable legs).
- Not Hibachi-only microstructure ML.
- Not private/account APIs.

---

## 2. Candidate venues

Four public candidates were screened. Selection favors liquidity relevance to
USDT-margined ETH price discovery, public WS without auth, timestamp/sequence
quality, and implementation complexity for a **minimal** trades + BBO pilot.

| Venue | Instrument | Role |
|---|---|---|
| Binance USD-M Futures | `ETHUSDT` perpetual | **Primary recommendation** |
| Bybit V5 linear | `ETHUSDT` perpetual | Fallback |
| OKX V5 | `ETH-USDT-SWAP` | Alternate |
| Coinbase Advanced Trade (public) | `ETH-USD` spot | Rejected for first pilot |

Machine-readable matrix: `docs/external_feed_venue_matrix_v1.json`.

### 2.1 Binance USD-M `ETHUSDT` (recommended)

| Criterion | Assessment |
|---|---|
| Liquidity / price-discovery value | Highest among screened USDT ETH perps; dominant global reference |
| Public WS (no auth) | Yes — market streams |
| Trade stream | `aggTrade` / `trade` (prefer `aggTrade` for rate control) |
| BBO/TOB | `bookTicker` (best bid/ask + qty) |
| Depth | Available; **not** in first pilot |
| Exchange timestamps | Millisecond event times on trade/aggTrade (`T`); event time `E` |
| Sequence / update IDs | `bookTicker.u` updateId; trade IDs on trades |
| Snapshot/reconnect | Stream resubscribe; depth would need REST snapshot — N/A for BBO+trades |
| Heartbeat | Binance WS ping/pong; idle disconnect semantics documented |
| Rate limits | Connection/subscribe limits; single-symbol BBO+aggTrade is light vs depth |
| Expected message rate | Order-of-magnitude **10–80 msg/s** average; spikes **>200/s** in bursts |
| Expected bytes | See §8 |
| Implementation complexity | Medium (2026 USDM WS path split `/public` vs `/market`) |
| Major risks | Endpoint migration (legacy USDM WS retirement **2026-04-23**); geo/IP variance; burst disk |

**2026 note:** USD-M Futures WS is split into dedicated bases (public high-frequency
vs market). Pilot adapter must target current documented URLs (e.g. public for
`bookTicker`/`depth`, market for some trade/kline family streams) — verify against
Binance docs at implementation time. Do not hard-code retired legacy hosts.

### 2.2 Bybit V5 linear `ETHUSDT` (fallback)

| Criterion | Assessment |
|---|---|
| Liquidity | High; slightly behind Binance for ETHUSDT discovery in most regimes |
| Public WS | `wss://stream.bybit.com/v5/public` (linear), no auth |
| Trade | `publicTrade.ETHUSDT` with match time `T`, cross `seq` |
| BBO | `orderbook.1.ETHUSDT` with `u`, `seq`, matching-engine `cts` |
| Timestamps | ms `ts` / `T` / `cts` — strong for exchange-time diagnostics |
| Complexity | Low–medium; clean V5 subscribe JSON |
| Risks | Regional reach; lower discovery weight than Binance |

### 2.3 OKX `ETH-USDT-SWAP` (alternate)

| Criterion | Assessment |
|---|---|
| Liquidity | High |
| Public WS | `wss://ws.okx.com:8443/ws/v5/public`, no auth for public channels |
| Trade | public trades |
| BBO | `bbo-tbt` (≈10 ms) or `books5` (≈100 ms snapshots) |
| Sequence | `seqId` / `prevSeqId` on book channels |
| Risks | Some ultra-low-latency book channels are VIP-gated; stick to public BBO/trades |
| Complexity | Medium (channel naming / VIP footguns) |

### 2.4 Coinbase `ETH-USD` spot (not first pilot)

Spot USD vs Hibachi USDT-P mixes FX/stablecoin basis into the lag residual.
Useful later for cross-product RV; **not** the minimal first lead-lag test.

---

## 3. Recommended primary feed

**RECOMMENDED_FEED**

- **Venue:** Binance USD-M Futures  
- **Instrument:** `ETHUSDT` perpetual  
- **Streams (minimal):**  
  1. `bookTicker` — executable BBO proxy  
  2. `aggTrade` — public aggressive flow / impulse events  
- **Fallback:** Bybit linear `ETHUSDT` with `orderbook.1` + `publicTrade`  
- **Explicitly deferred:** full depth, mark/index, liquidations, multi-symbol fanout

**WHY:** Binance is the strongest public ETHUSDT price-discovery reference among
candidates; BBO+aggTrade is sufficient to define external mid/trade impulses
without deep-book storage cost; no auth for public market data.

---

## 4. Timestamp model

Every external RAW envelope **must** retain:

| Field | Role |
|---|---|
| `venue` | e.g. `binance_usdm` |
| `instrument` | e.g. `ETHUSDT` |
| `event_type` | `book_ticker` / `agg_trade` / … |
| `exchange_at` | venue event/match time when present |
| `received_at` | local monotonic-wall receipt time (UTC) |
| `connection_id` | WS session UUID |
| `local_sequence` | strictly increasing per connection |
| `exchange_sequence` | updateId / seq / trade id when supplied (nullable) |
| `schema_version` | envelope version |
| `payload` | raw JSON (append-only) |
| `clock_quality` (optional metadata) | NTP sync flag / offset sample near session |

### Two analysis clocks (both required in protocol)

1. **Exchange-time analysis** — market-structure diagnostics, desync detection.  
2. **Arrival-time analysis** — **only** clock allowed for trading-causal claims.

**Rule:** a Hibachi decision at research time \(t\) may use external events with
`received_at ≤ t` only. Never align solely on `exchange_at` for PnL causality.

---

## 5. Clock synchronization

### Observed VPS state (read-only, 2026-08-11)

- `timedatectl`: **System clock synchronized: yes**; **NTP service: active**
- Free disk ≈ **5.9 GiB** on `/` (emergency floor remains **5 GiB**)
- Hibachi collector: healthy; postgres: healthy  
  (observation only — no remediation in this milestone)

`chronyc` was not available in the deploy account path; NTP discipline is still
active via systemd/timesyncd-or-equivalent as reported by `timedatectl`.

### Design requirements

- Record NTP sync boolean (and offset if readable without privilege escalation)
  at session start / periodic health ticks.
- **No** manual timestamp rewriting that can create lookahead.
- Trustworthy cross-venue trading inference on this host: treat **~100 ms** as
  optimistic; design gates around **≥250 ms** lags first.
- If future measurements show offset jitter ≫ 50–100 ms, widen minimum lag bin
  and document degraded timing class.

---

## 6. Failure-domain isolation

**Invariant**

`Hibachi collector failure domain ≠ external reference collector failure domain`

| Requirement | Design |
|---|---|
| Process | Separate Compose service, e.g. `external-ref-collector` |
| Restart | Independent `restart` policy; Hibachi never `depends_on` external |
| Health | Separate healthcheck; external unhealthy ≠ Hibachi stop |
| Logs | Separate container logs |
| Buffer | Dedicated spool dir; bounded; drop/pause **external only** on overflow |
| Code | No venue adapter imports inside Hibachi stream path |
| Config | Feature flag **default OFF** |

External timeout, disconnect, parse error, rate limit, or disk pause **must not**
propagate fatal exceptions into the Hibachi collector.

---

## 7. Storage architecture (capacity-constrained)

### Constraint

VPS free ≈ 5.9 GiB with a **5 GiB emergency floor** → ~0.9 GiB practical headroom.
Writing high-rate external RAW into PostgreSQL `market_events` (or a sibling hot
table on the same instance) risks WAL/heap pressure against Hibachi lifecycle.

### Decision: prefer Option B

**B — Separate bounded local spool → Parquet → B2** (recommended)

- Append NDJSON/JSONL or length-prefixed records to a **UID 10001 / mode 0700**
  spool directory owned for the external service only.
- Rotate files by time (e.g. 5–15 min) or size (e.g. 32–64 MiB).
- Convert rotated files to **zstd Parquet** offline/batch on the same host or a
  research host; upload with Hibachi-like COMPLETED semantics.
- Keep on-box retention tiny (hours), not days.

**A — Separate PostgreSQL partitioned table** — **rejected for first pilot** on
this VPS (WAL + dual hot writers + generation policy entanglement). Revisit only
with explicit capacity headroom and separate approval.

**C — Shared generalized `market_events`** — **rejected** (contaminates proven
Hibachi RAW lifecycle and DROP/archive identity).

### Minimal external RAW logical schema

```
id                  # monotonic within spool/file or archive window
venue
instrument
event_type
exchange_at         # timestamptz nullable
received_at         # timestamptz NOT NULL
connection_id
local_sequence
exchange_sequence   # text/bigint nullable
schema_version
payload             # jsonb/raw text
```

No hot-path normalization beyond envelope validation.

### Expected rate (pilot planning estimates)

Assumptions for Binance `ETHUSDT` `bookTicker` + `aggTrade`:

| Metric | Conservative avg | Busy / spike |
|---|---:|---:|
| Messages / sec | 20–40 | 100–300+ |
| Bytes / envelope+payload | 500–800 | ≤2 KiB |
| MiB / hour (uncompressed spool) | ~40–90 | 200+ |
| MiB / day (uncompressed) | ~1.0–2.2 GiB | — |
| MiB / day (zstd Parquet, est.) | ~250–600 MiB | — |

**Capacity rule:** external spool + pending Parquet on VPS must never reduce free
disk below **5 GiB + 0.5 GiB safety margin** (operator-set). On breach: **pause or
stop external only**; Hibachi continues.

---

## 8. B2 archival contract

Mirror Hibachi evidence spirit without forcing identical generation DROP code:

1. Bounded window export (time- or size-bounded Parquet bundle)
2. `logical_checksums.sha256` + physical checksums
3. `archive_metadata.json` / `provenance.json` (venue, instrument, schema, clock notes)
4. Upload with explicit `--confirm-upload`
5. Download verify SHA-256
6. Restore validation
7. Publish `COMPLETED` only after checks

Prefix suggestion: `archives/external/<venue>/<instrument>/<dataset_id>/`

Do **not** mutate existing Hibachi B2 prefixes. External archive failure must not
block Hibachi generation archive.

Research reproducibility: lead-lag studies materialize from **COMPLETED** external
RAW + Hibachi RAW only (no hot-PG historical scans).

---

## 9. Offline research artifact: `cross_venue_market_state`

Built **offline** from verified RAW (never on Hibachi write path).

| Side | Fields |
|---|---|
| Hibachi | bid, ask, mid, received_at, quality/staleness |
| External | bid, ask, mid, last trade, received_at, exchange_at, sequence quality |
| Derived | `ext_minus_hibachi_mid_bps`; lagged external returns; Hibachi forward executable return; ages; clock/session quality |

Joins: as-of merge on **`received_at`** for causal panels; optional parallel
exchange-time panel clearly labeled `diagnostic_only`.

---

## 10. Lead-lag screening protocol (pre-ML)

Predeclared diagnostics only — **no ML**, no broad lag fishing after peeking.

1. Sparse external impulse events: \|Δ mid\| or trade notional ≥ predeclared cut
   (freeze cuts before examining Hibachi response).
2. Arrival-time event-response curves for Hibachi mid at
   `{250ms, 500ms, 1s, 2s, 5s}`.
3. Signed hit rate vs sign of external impulse.
4. Mean / p50 / p95 gross Hibachi executable move (bps).
5. Continuous cross-correlation on coarse grids (secondary).
6. Chronological block stability (e.g. halves of pilot).
7. Cost overlay vs predeclared execution style.

---

## 11. Economic acceptance gate (predeclared)

Declare feed **PROMISING_FOR_FURTHER_COLLECTION** only if exploratory pilot shows
**all** of:

| Gate | Threshold (initial proposal) |
|---|---|
| Sample | ≥ **200** non-overlapping external impulse events |
| Direction | Same-sign Hibachi response rate ≥ **55%** at best predeclared lag ∈ {0.5s,1s,2s} |
| Gross | Conditional mean signed Hibachi executable move ≥ **15 bps**  
  (≈ 11.05 friction + ~4 bps safety) at that lag **or**  
  p25 of signed move ≥ **11.05 bps** with n≥200 |
| Stability | Gate passes on **both** chronological halves (no single-burst artifact) |
| Causality | Arrival-time panel; exchange-time-only “edge” insufficient |
| Costs | Style frozen before measurement (default TAKER_TAKER) |

If gates fail: `EXTERNAL_LEAD_LAG_REJECTED_FOR_VENUE` — do not add ML.

These thresholds are **predeclared before pilot data examination**. Changing them
requires a new versioned protocol doc.

---

## 12. OOS discipline

New experiment family → new periods:

| Phase | Use |
|---|---|
| Technical canary | Connectivity/schema only |
| Quality pilot | Timing, rates, integrity |
| Exploratory collection | Gate evaluation / screening |
| Validation | Locked protocol check |
| Final OOS | Untouched future block after protocol freeze |

Do **not** reuse `g_7471913` or other already-inspected Hibachi periods as clean
final OOS for this family. Preserve
`RESERVED_NEXT_VERIFIED_GENERATION_AFTER_g_7871913` (or successor) for Hibachi
side; designate a **fresh** external+Hibachi joint OOS window when protocol freezes.

---

## 13. Bounded pilot plan

| Stage | Duration | Goal | Auto-promote? |
|---|---|---|---|
| Technical canary | **30–60 min** | Both collectors independent; external topics live; timestamps plausible; disk within estimate | No |
| Quality pilot | **6–24 h** | Reconnect semantics; sequence behavior; rate vs estimate; no Hibachi impact | No |
| Exploratory | **several days** only after explicit approval | Economic gate evaluation | No |

No indefinite collection authorization in this design.

### Technical canary acceptance checklist

- [ ] External service starts with flag OFF→ON only under approval
- [ ] Hibachi collector remains healthy / unaffected
- [ ] External stream receives `bookTicker` + `aggTrade`
- [ ] `received_at` and `exchange_at` populated where expected
- [ ] `local_sequence` monotonic per connection
- [ ] Reconnect produces new `connection_id` without crashing Hibachi
- [ ] Spool size within estimate; free disk ≥ 5 GiB + margin
- [ ] No writes into `market_events`
- [ ] Public data only (no keys)

---

## 14. Security

- Public market data only  
- No account APIs, balances, positions, orders, withdrawals  
- No API keys in Git or Compose for this pilot  
- If a future venue requires auth for “public” data: escalate complexity and
  re-approve (Binance/Bybit public market WS do not require this for the chosen streams)

---

## 15. Rollback design

| Item | Design |
|---|---|
| Service name | `external-ref-collector` (proposed) |
| Default | `EXTERNAL_REF_ENABLED=false` / profile not in default Compose up |
| Stop | `docker compose stop external-ref-collector` — Hibachi untouched |
| Remove | remove service from override / disable profile |
| Temp RAW | delete only external spool path after archive or explicit discard approval |
| Schema | **no** Alembic change required for Option B spool pilot |
| Config revert | restore env flag OFF; no Hibachi env mutation |

Pilot must be reversible without Hibachi restart.

---

## 16. Repository plan (not implemented)

Proposed package boundary (future implementation milestone):

```
src/trading_bot/external_market_data/
  __init__.py
  envelope.py          # RAW schema + schema_version
  health.py            # isolated health / clock metadata
  spool_writer.py      # bounded fail-closed writer
  archive_contract.py  # window → checksum → COMPLETED semantics
  adapters/
    binance_usdm.py    # primary
    bybit_linear.py    # fallback
  research/
    cross_venue_state.py   # offline only
    lead_lag_protocol.py   # offline only
```

Compose: optional profile `external-ref` **omitted** from default production up.

**Do not** couple adapters into `HibachiMarketStream`.

---

## 17. Production boundaries (this milestone)

Observed only; **unchanged**:

- Hibachi collector not restarted for this design
- Subscriptions / partition sizes / DROP policy untouched
- No hot-PG historical scan
- No B2 mutation
- No PAPER/LIVE

---

## Final report card

STATUS: `EXTERNAL_FEED_DESIGN_READY`

CANDIDATES: Binance USDM ETHUSDT; Bybit ETHUSDT; OKX ETH-USDT-SWAP; Coinbase ETH-USD (deferred)

RECOMMENDED_FEED: Binance USD-M / `ETHUSDT` / `bookTicker` + `aggTrade`

WHY: Dominant public ETHUSDT discovery; minimal streams; no auth

TIMESTAMPS: exchange ms + local `received_at`; updateId/seq when present; dual clock analysis

EXPECTED_RATE: ~20–40 msg/s avg (spikes higher); ~40–90 MiB/h spool; ~0.25–0.6 GiB/day Parquet est.

STORAGE: Option B spool → Parquet → B2; not `market_events`; respect 5 GiB floor

FAILURE_ISOLATION: separate container/process; independent restart/health; never fatal to Hibachi

CLOCK: NTP active/synchronized on VPS; trading gates ≥250 ms; ~100 ms optimistic floor

RESEARCH_PROTOCOL: sparse impulses + arrival-time response curves; no ML

ECONOMIC_GATE: n≥200; ≥55% same-sign; mean signed ≥15 bps or p25≥11.05; both halves; arrival-time only

PILOT: 30–60 min canary → 6–24 h quality → multi-day exploratory only with approval

ROLLBACK: OFF-by-default service; stop/remove without touching Hibachi; discard external spool only

PRODUCTION: Hibachi unchanged

ML_STATUS: `BLOCKED`

BLOCKERS: none factual for **design approval**; implementation still requires human go-ahead; VPS headroom is tight (~0.9 GiB above 5 GiB floor) — spool caps mandatory

NEXT (not executed):

`request explicit human approval to implement the isolated external feed in OFF-by-default mode and run only a bounded technical canary`
