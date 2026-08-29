"""ETH executable-path quality remediation v1.

Root-cause, freeze eligibility from feed semantics, rebuild market_state_1s
from verified restored RAW, rerun frozen first-passage and TP/SL grids.
Does not overwrite v1 reports. No ML, PAPER, or live trading.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trading_bot.research.collection_gaps import load_collection_gaps
from trading_bot.research.pipeline.cost_evidence import (
    funding_contribution_bps,
    hibachi_public_fee_schedule,
    round_trip_friction_bps,
)
from trading_bot.research.pipeline.executable_tob import (
    EXECUTABLE_TOB_ELIGIBILITY,
    EXECUTABLE_TOB_SOURCES,
)
from trading_bot.research.pipeline.first_passage_corpus import (
    V1_UNTOUCHED_OOS_UTC_DATES,
    materialize_from_local_restored,
)
from trading_bot.research.pipeline.first_passage_opportunity import (
    FIRST_PASSAGE_HORIZONS_SECONDS,
    FIRST_PASSAGE_THRESHOLDS_BPS,
    analyze_first_passage,
    day_block_stability,
    extract_exec_nonoverlap_snapshot,
    render_full_corpus_markdown,
)
from trading_bot.research.pipeline.tp_sl_first_touch import (
    ALL_HORIZONS_SECONDS,
    CURRENT_MODELED_LATENCY_BPS_PER_SIDE,
    EXECUTION_DELAYS_SECONDS,
    LATENCY_BPS_PER_SIDE_GRID,
    PRIMARY_HORIZONS_SECONDS,
    SL_THRESHOLDS_BPS,
    TP_THRESHOLDS_BPS,
    analyze_tp_sl_first_touch,
    assess_primary_contamination,
    audit_cost_decomposition,
    discovery_dates_from_full_corpus_doc,
    filter_tp_sl_series,
    index_restored_raw_windows,
    load_tp_sl_series_from_parquet,
    render_tp_sl_markdown,
    scan_forensic_excursions,
    spot_check_restored_raw_events,
)

ROOT = Path("data/research/full_corpus")
OLD_MS = ROOT / "runs" / "first_passage_full_corpus"
NEW_MS = ROOT / "runs" / "executable_path_clean_v1"
RESTORE = ROOT / "restored_events"
FULL_CORPUS_DOC = Path("docs/eth_first_passage_full_corpus_v1.json")
FP_V1 = Path("docs/eth_first_passage_full_corpus_v1.json")
TPSL_V1 = Path("docs/eth_tp_sl_first_touch_feasibility_v1.json")
DOCS = Path("docs")


def _peak_rss_mib() -> float | None:
    try:
        import psutil  # type: ignore[import-untyped]

        return float(psutil.Process().memory_info().rss / (1024 * 1024))
    except Exception:
        return None


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _row_at(path: Path, when: datetime) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    target = int(when.timestamp())
    closest: dict[str, Any] | None = None
    closest_dt = 10**9
    table = pq.read_table(path)
    for row in table.to_pylist():
        ts = _dt(row["decision_time"])
        delta = abs(int(ts.timestamp()) - target)
        if delta < closest_dt:
            closest_dt = delta
            closest = row
            if delta == 0:
                break
    return closest


def _taker_rt(spreads: list[float]) -> tuple[float, float | None]:
    ordered = sorted(spreads)
    median = ordered[(len(ordered) - 1) // 2] if ordered else None
    fees = hibachi_public_fee_schedule()
    friction = round_trip_friction_bps(
        taker_fee_rate=float(fees["tier1_taker_fee_rate"]),
        slippage_bps_per_side=0.0,
        latency_bps_per_side=1.0,
        spread_bps_round_trip=float(median or 0.0),
        funding_bps=funding_contribution_bps(15.0),
    )
    return friction, median


def _tpsl_cell(
    report: Mapping[str, Any],
    horizon: int,
    tp: int,
    sl: int,
    direction: str,
) -> dict[str, Any]:
    layer = (report.get("delay_0s") or {}).get("non_overlapping") or {}
    return (
        ((layer.get(f"{horizon}s") or {}).get(str(tp)) or {}).get(str(sl)) or {}
    ).get(direction) or {}


def _primary_contamination_proof(stats: Mapping[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    max_frac = 0.0
    for horizon in PRIMARY_HORIZONS_SECONDS:
        for tp in TP_THRESHOLDS_BPS:
            for sl in SL_THRESHOLDS_BPS:
                for direction in ("long", "short"):
                    cell = _tpsl_cell(stats, horizon, int(tp), int(sl), direction)
                    n_valid = int(cell.get("n_valid_starts") or 0)
                    n_invalid = int(cell.get("n_data_invalid") or 0)
                    n_stale = int(
                        cell.get("n_tp_or_sl_resolved_on_stale_or_quote_fallback") or 0
                    )
                    n_hit = int(cell.get("n_tp_first") or 0) + int(cell.get("n_sl_first") or 0)
                    frac = (n_stale / n_hit) if n_hit else 0.0
                    max_frac = max(max_frac, frac)
                    rows.append(
                        {
                            "horizon_seconds": horizon,
                            "tp_bps": tp,
                            "sl_bps": sl,
                            "direction": direction,
                            "n_valid": n_valid,
                            "n_data_invalid": n_invalid,
                            "n_tp_or_sl": n_hit,
                            "n_stale_or_fallback_touches": n_stale,
                            "contaminated_resolution_fraction": frac,
                        }
                    )
    return {
        "primary_cells": rows,
        "max_contaminated_resolution_fraction": max_frac,
        "stale_or_fallback_barrier_resolutions_by_construction": max_frac == 0.0,
        "note": (
            "DATA_INVALID is a dropped unobservable window, not TIMEOUT. "
            "Target is 0 stale/fallback barrier resolutions among executable samples."
        ),
    }


def _wide_native_bbo_forensics(
    *,
    spread_bps: Sequence[float | None],
    mid: Sequence[float],
    mark_price: Sequence[float | None] | None,
) -> dict[str, Any]:
    """Describe remaining venue-print quality. Not an eligibility filter."""

    n = len(spread_bps)
    n50 = n100 = n200 = n_mark25 = 0
    for index, spread in enumerate(spread_bps):
        if spread is not None and spread >= 50:
            n50 += 1
        if spread is not None and spread >= 100:
            n100 += 1
        if spread is not None and spread >= 200:
            n200 += 1
        if mark_price is not None and index < len(mark_price):
            mark = mark_price[index]
            price = mid[index] if index < len(mid) else None
            if (
                mark is not None
                and price is not None
                and price > 0
                and abs(float(mark) / float(price) - 1.0) * 10_000.0 >= 25.0
            ):
                n_mark25 += 1
    return {
        "n_rows": n,
        "n_spread_ge_50bps": n50,
        "n_spread_ge_100bps": n100,
        "n_spread_ge_200bps": n200,
        "n_mark_vs_mid_ge_25bps": n_mark25,
        "applied_as_eligibility_filter": False,
        "lead_options_not_applied": [
            "keep_5s_freshness_only",
            "invalidate_when_mark_vs_mid_ge_25bps_forensic_constant",
            "invalidate_when_spread_ge_lead_chosen_cap",
            "require_quote_and_reconstructed_book_agree",
        ],
        "note": (
            "Wide native Hibachi BBO (far ask, trades/mark on the bid) remains "
            "DIRECT_QUOTE_FRESH under the frozen 5s rule. A spread/mark cap "
            "was not chosen from TP/SL statistics."
        ),
    }


def _day_tp_rate(report: Mapping[str, Any], day: str, horizon: int) -> float | None:
    days = (report.get("day_stability_nonoverlap_offset0") or {}).get("per_utc_day") or []
    for row in days:
        if row.get("utc_date") == day:
            cell = (row.get("cells") or {}).get(f"{horizon}s_tp20_sl10_long") or {}
            return cell.get("tp_first_rate")
    return None


def build_rca() -> dict[str, Any]:
    """Trace contaminated clusters to RAW before any economic rerun."""

    traces: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = [
        {
            "label": "aug19_2050_500bps_cluster",
            "when": datetime(2026, 8, 19, 20, 50, 14, tzinfo=UTC),
            "hypothesis": "genuine_burst_after_capacity_stop_resume",
        },
        {
            "label": "aug19_2108_ghost_ask",
            "when": datetime(2026, 8, 19, 21, 8, 0, tzinfo=UTC),
            "hypothesis": "one_sided_flickering_native_bbo",
        },
        {
            "label": "aug19_clean_control",
            "when": datetime(2026, 8, 19, 18, 30, 0, tzinfo=UTC),
            "hypothesis": "clean_post_resume_control",
        },
        {
            "label": "jul30_contaminated_primary_proxy",
            "when": datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC),
            "hypothesis": "quote_fallback_used_as_executable_when_book_invalid",
        },
    ]
    restore_index = index_restored_raw_windows(RESTORE)
    for case in cases:
        when = case["when"]
        if not isinstance(when, datetime):
            raise TypeError(f"RCA case {case.get('label')!r} when must be datetime")
        day = when.date().isoformat()
        old = _row_at(
            OLD_MS / f"utc_{day}" / "market_state_1s" / "market_state_1s.parquet",
            when,
        )
        new = _row_at(
            NEW_MS / f"utc_{day}" / "market_state_1s" / "market_state_1s.parquet",
            when,
        )
        raw_note = spot_check_restored_raw_events(
            epoch_s=int(when.timestamp()),
            restore_index=restore_index,
            window_seconds=2,
        )
        traces.append(
            {
                **case,
                "when_utc": when.isoformat(),
                "old_market_state": {
                    "decision_time": str(old.get("decision_time")) if old else None,
                    "best_bid": old.get("best_bid") if old else None,
                    "best_ask": old.get("best_ask") if old else None,
                    "mid": old.get("mid") if old else None,
                    "spread_bps": old.get("spread_bps") if old else None,
                    "valid_book": old.get("valid_book") if old else None,
                    "quote_fresh": old.get("quote_fresh") if old else None,
                    "book_state": old.get("book_state") if old else None,
                    "mark_price": old.get("mark_price") if old else None,
                    "spot_price": old.get("spot_price") if old else None,
                    "connection_id": old.get("connection_id") if old else None,
                    "book_age_seconds": old.get("book_age_seconds") if old else None,
                },
                "new_market_state": {
                    "decision_time": str(new.get("decision_time")) if new else None,
                    "best_bid": new.get("best_bid") if new else None,
                    "best_ask": new.get("best_ask") if new else None,
                    "tob_source": new.get("tob_source") if new else None,
                    "executable_tob": new.get("executable_tob") if new else None,
                    "reconstructed_best_ask": new.get("reconstructed_best_ask") if new else None,
                    "mark_price": new.get("mark_price") if new else None,
                    "spot_price": new.get("spot_price") if new else None,
                    "spread_bps": new.get("spread_bps") if new else None,
                    "connection_id": new.get("connection_id") if new else None,
                }
                if new
                else None,
                "raw_spot_check": raw_note,
            }
        )
    return {
        "unknown_event_type_was_wrong_column": (
            "Restored B2 parquet uses `topic`, not `event_type`. "
            "spot_check previously counted every row as unknown. Payloads are "
            "ask_bid_price/orderbook/mark_price/spot_price/funding/trades."
        ),
        "quote_parser_root_cause": (
            "From 2026-08-19 onward, ask_bid_price data includes timestampMs. "
            "Exact-key contract expected only bidPrice/bidSize/askPrice/askSize, "
            "so 100% of quotes failed with payload.data fields do not match "
            "contract. That is a parser classification failure, not an unknown "
            "RAW type. July 29–Aug 6 quotes lack timestampMs and parsed."
        ),
        "schema_version_mapping": (
            "raw_row_to_market_event read schema_version (absent on B2 rows) "
            "and defaulted to 1, labelling reconstructed books best_effort_legacy. "
            "Archive column is raw_schema_version=2. exchange_sequence is null; "
            "local_sequence is connection-global so it cannot detect missed "
            "orderbook diffs."
        ),
        "aug19_2050": (
            "Same connection after the 18:04Z capacity-stop resume. RAW contains "
            "trades, marks, spots, quotes, and orderbook. After the quote parser "
            "fix, 20:50 TOB is DIRECT_QUOTE_FRESH (example 20:50:14 bid 2131.68 "
            "ask 2134.26, ~12 bps, mark 2134.26). Peak 1s mid jump in the next "
            "minute is ~112 bps with a temporarily wide native book, not an "
            "archive stitch. v1 500+ bps 60s MFE mixed this burst with later "
            "far-ask flicker; magnitude is not a silent forward-fill."
        ),
        "aug19_2108": (
            "Native ask_bid_price itself prints bid ~2315 / ask ~2372–2404 "
            "(spread 240–360 bps) while mark, spot, and trade prints stay "
            "~2312–2327. Reconstructed L2 ask matches the native quote ask, "
            "so this is not a reconstructor ghost invented after quote drop. "
            "Quote timestampMs repeats across ~3 Hz receipts (growing "
            "latency_ms, still <5s). Classification: one-sided/flickering "
            "native BBO (far resting ask, bid-side trades). Not quote-fallback, "
            "not a collection gap, not unknown event type. Under the frozen 5s "
            "rule it remains DIRECT_QUOTE_FRESH. A spread/mark cap would be a "
            "new eligibility rule and was not applied."
        ),
        "jul30_primary_contamination": (
            "v1 tagged valid_book=False rows as stale/quote-fallback. Those days "
            "had parsing quotes; market_state preferred reconstructed book and "
            "used quote only when the book was invalid. Fresh native BBO was "
            "treated as contamination. After remediation it is DIRECT_QUOTE_FRESH."
        ),
        "not_causes": [
            "archive stitching across the 18:04Z resume for the 20:50 cluster",
            "unknown RAW event types",
            "raw schema payload identity mismatch on topic/symbol",
        ],
        "external_binance": {
            "status": "NOT_AVAILABLE_LOCALLY",
            "note": (
                "No Binance USD-M spool/Parquet was present under data/ for "
                "forensic compare. Hibachi reconstructed TOB, direct quote, "
                "mark, spot, and trades are the available public prints."
            ),
        },
        "traces": traces,
    }


def _render_remediation_md(payload: Mapping[str, Any]) -> str:
    elig = payload.get("eligibility_freeze") or {}
    rca = payload.get("root_cause") or {}
    proof = payload.get("contamination_proof") or {}
    deltas = payload.get("before_after") or {}
    lines = [
        "# ETH executable path quality remediation v1",
        "",
        f"STATUS: `{payload.get('STATUS')}`",
        f"ML_STATUS: `{payload.get('ML_STATUS')}`",
        f"DECISION: `{payload.get('DECISION')}`",
        "",
        "## Eligibility freeze (before rerun economics)",
        "",
        f"- rule: {elig.get('rule')}",
        f"- preferred source: `{elig.get('preferred_source')}`",
        f"- max stale quote/book seconds: `{elig.get('max_stale_quote_seconds')}` / "
        f"`{elig.get('max_stale_book_seconds')}`",
        f"- bound fitted from TP/SL: `{elig.get('bound_fitted_from_tp_sl')}`",
        f"- quote fallback executable: `{elig.get('quote_fallback_executable')}`",
        f"- stale carry executable: `{elig.get('stale_carry_executable')}`",
        "",
        "A 1s row is executable only as `DIRECT_QUOTE_FRESH` (native BBO within "
        "5s, same connection) or `RECONSTRUCTED_BOOK_FRESH` (valid book within "
        "5s when no fresh quote exists). DATA_INVALID is not TIMEOUT.",
        "",
        "## Root cause",
        "",
        f"- {rca.get('quote_parser_root_cause')}",
        "",
        f"- {rca.get('schema_version_mapping')}",
        "",
        f"- Aug 19 20:50: {rca.get('aug19_2050')}",
        "",
        f"- Aug 19 21:08: {rca.get('aug19_2108')}",
        "",
        f"- July 30-style contamination: {rca.get('jul30_primary_contamination')}",
        "",
        f"- unknown types: {rca.get('unknown_event_type_was_wrong_column')}",
        "",
        f"- Binance external: `{(rca.get('external_binance') or {}).get('status')}`",
        "",
        "## Remaining venue-print quality (not filtered)",
        "",
        "Native Hibachi BBO can print a far ask while trades/mark stay on the "
        "bid (Aug 19 ~21:08). Those seconds stay `DIRECT_QUOTE_FRESH` under "
        "the frozen 5s rule. Options for a later eligibility rule were not "
        "applied and must not be fitted to TP/SL hit rates.",
        "",
        f"- wide-BBO forensics: `{payload.get('wide_native_bbo')}`",
        "",
        "## Rebuild",
        "",
        "Parser accepts v2 `timestampMs`. Mapper reads `raw_schema_version`. "
        "market_state_1s prefers the native quote and labels `tob_source`. "
        "Artifacts rebuilt under `data/research/full_corpus/runs/executable_path_clean_v1/`. "
        "v1 reports and the previous market_state tree were not overwritten.",
        "",
        "## Contamination proof (primary TP/SL, offset 0)",
        "",
        "- max stale/fallback resolution fraction: "
        f"`{proof.get('max_contaminated_resolution_fraction')}`",
        "- zero by construction: "
        f"`{proof.get('stale_or_fallback_barrier_resolutions_by_construction')}`",
        "",
        "| H | TP | SL | dir | n_valid | DATA_INVALID | TP+SL | stale/fallback | frac |",
        "|---:|---:|---:|---|---:|---:|---:|---:|---:|",
    ]
    for row in proof.get("primary_cells") or []:
        if int(row.get("tp_bps") or 0) not in {20, 25, 30}:
            continue
        if int(row.get("horizon_seconds") or 0) not in {120, 180, 300}:
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("horizon_seconds")),
                    str(int(row.get("tp_bps"))),
                    str(int(row.get("sl_bps"))),
                    str(row.get("direction")),
                    str(row.get("n_valid")),
                    str(row.get("n_data_invalid")),
                    str(row.get("n_tp_or_sl")),
                    str(row.get("n_stale_or_fallback_touches")),
                    f"{100.0 * float(row.get('contaminated_resolution_fraction') or 0):.2f}%",
                ]
            )
            + " |"
        )
    fp = deltas.get("first_passage") or {}
    tpsl = deltas.get("tp_sl") or {}
    lines += [
        "",
        "## Before/after (frozen grids, no retune)",
        "",
        "### First passage executable either-side hit fraction (non-overlap mean)",
        "",
        "| cell | v1 | clean |",
        "|---|---:|---:|",
    ]
    for key, row in fp.items():
        lines.append(
            f"| {key} | {row.get('before')} | {row.get('after')} |"
        )
    lines += [
        "",
        "### TP/SL 120/180/300 × TP20/25/30 SL10 long (offset 0)",
        "",
        "| cell | v1 n | v1 TP-first | clean n | clean TP-first | clean DATA_INVALID |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for key, row in tpsl.items():
        lines.append(
            "| "
            + " | ".join(
                [
                    key,
                    str(row.get("before_n")),
                    str(row.get("before_tp_first")),
                    str(row.get("after_n")),
                    str(row.get("after_tp_first")),
                    str(row.get("after_data_invalid")),
                ]
            )
            + " |"
        )
    lines += [
        "",
        "### UTC day TP-first 120s TP20 SL10 long",
        "",
        f"- 2026-08-19 v1 → clean: `{deltas.get('aug19_120s_tp20_sl10_long')}`",
        f"- 2026-08-20 v1 → clean: `{deltas.get('aug20_120s_tp20_sl10_long')}`",
        "",
        "## Versioned rerun reports",
        "",
        "- `docs/eth_first_passage_full_corpus_clean_v1.md`",
        "- `docs/eth_tp_sl_first_touch_feasibility_clean_v1.md`",
        "",
        "Existing v1 reports remain immutable.",
        "",
        "## Lead decision",
        "",
        "STOP_FOR_LEAD_REVIEW. Do not start ML, feature selection, TP/SL",
        "optimization, PAPER, or live trading.",
        "",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    started = time.perf_counter()
    dates = discovery_dates_from_full_corpus_doc(FULL_CORPUS_DOC)
    print(
        json.dumps(
            {
                "phase": "eligibility_freeze",
                "eligibility": EXECUTABLE_TOB_ELIGIBILITY,
                "executable_sources": sorted(EXECUTABLE_TOB_SOURCES),
                "discovery_dates": dates,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    print(json.dumps({"phase": "rebuild_start"}, sort_keys=True), flush=True)
    materialize = materialize_from_local_restored(
        restore_root=RESTORE,
        runs_root=NEW_MS,
        discovery_dates=dates,
        force_rebuild=False,
        keep_normalized=False,
    )
    print(
        json.dumps(
            {
                "phase": "rebuild_done",
                "errors": materialize.get("errors"),
                "days": [
                    {"utc_date": item.get("utc_date"), "status": item.get("status")}
                    for item in materialize.get("day_outputs") or []
                ],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    paths = list(materialize.get("market_state_paths") or [])
    errors = list(materialize.get("errors") or [])
    if errors:
        raise SystemExit(f"clean market_state rebuild failed: {errors}")
    if len(paths) != len(dates):
        raise SystemExit(
            f"clean market_state incomplete: {len(paths)} days, expected {len(dates)}"
        )

    series = load_tp_sl_series_from_parquet(paths)
    gaps = load_collection_gaps()
    filtered, dropped = filter_tp_sl_series(series, gaps)
    tob = list(filtered.get("tob_source") or [])
    source_mix = dict(Counter(str(item) for item in tob))
    print(
        json.dumps(
            {
                "phase": "loaded",
                "rows": len(filtered["epoch_s"]),
                "dropped_gap_or_invalid": dropped,
                "tob_source_mix": source_mix,
            },
            sort_keys=True,
        ),
        flush=True,
    )

    epoch = list(filtered["epoch_s"])
    bid = list(filtered["bid"])
    ask = list(filtered["ask"])
    mid = list(filtered["mid"])
    times = list(filtered["times"])
    tob_source = list(filtered.get("tob_source") or [])
    conn = list(filtered.get("connection_id") or [])
    usable_hours = len(epoch) / 3600.0
    spreads = [
        float(value)
        for value in filtered.get("spread_bps") or []
        if value is not None
    ]
    taker_rt, median_spread = _taker_rt(spreads)
    wide_bbo = _wide_native_bbo_forensics(
        spread_bps=list(filtered.get("spread_bps") or []),
        mid=mid,
        mark_price=list(filtered.get("mark_price") or []),
    )
    print(
        json.dumps({"phase": "wide_native_bbo", "stats": wide_bbo}, sort_keys=True),
        flush=True,
    )

    print(json.dumps({"phase": "first_passage"}, sort_keys=True), flush=True)
    fp = analyze_first_passage(
        epoch,
        bid,
        ask,
        mid,
        horizons=FIRST_PASSAGE_HORIZONS_SECONDS,
        thresholds=FIRST_PASSAGE_THRESHOLDS_BPS,
        usable_hours=usable_hours,
        tob_source=tob_source or None,
        connection_id=conn or None,
    )
    stability = day_block_stability(
        times,
        bid,
        ask,
        mid,
        horizons=(60, 120, 300, 600),
        thresholds=(10.0, 15.0, 20.0, 25.0, 30.0),
        tob_source=tob_source or None,
        connection_id=conn or None,
    )
    fp_v1 = json.loads(FP_V1.read_text(encoding="utf-8")) if FP_V1.is_file() else {}
    fp_before = extract_exec_nonoverlap_snapshot(fp_v1)
    fp_after = extract_exec_nonoverlap_snapshot(fp)
    fp_delta = {
        key: {
            "before": (fp_before.get(key) or {}).get("either_side_hit_fraction_mean"),
            "after": (fp_after.get(key) or {}).get("either_side_hit_fraction_mean"),
        }
        for key in sorted(set(fp_before) | set(fp_after))
    }

    print(json.dumps({"phase": "tp_sl"}, sort_keys=True), flush=True)
    tpsl = analyze_tp_sl_first_touch(
        epoch,
        bid,
        ask,
        mid,
        horizons=ALL_HORIZONS_SECONDS,
        tp_grid=TP_THRESHOLDS_BPS,
        sl_grid=SL_THRESHOLDS_BPS,
        delays_seconds=EXECUTION_DELAYS_SECONDS,
        rolling_delays=(0,),
        latency_bps_per_side_grid=LATENCY_BPS_PER_SIDE_GRID,
        median_spread_bps=median_spread,
        valid_book=filtered.get("valid_book"),
        book_age_seconds=filtered.get("book_age_seconds"),
        tob_source=tob_source or None,
        executable_tob=filtered.get("executable_tob"),
        connection_id=conn or None,
    )
    forensic = scan_forensic_excursions(
        epoch,
        bid,
        ask,
        mid,
        valid_book=filtered.get("valid_book"),
        book_age_seconds=filtered.get("book_age_seconds"),
        quote_age_seconds=filtered.get("quote_age_seconds"),
        mark_price=filtered.get("mark_price"),
    )
    contamination = assess_primary_contamination(stats=tpsl, forensic=forensic)
    forensic["contamination"] = contamination
    proof = _primary_contamination_proof(tpsl)
    tpsl_v1 = json.loads(TPSL_V1.read_text(encoding="utf-8")) if TPSL_V1.is_file() else {}
    tpsl_delta: dict[str, Any] = {}
    for horizon in PRIMARY_HORIZONS_SECONDS:
        for tp in (20, 25, 30):
            key = f"{horizon}s_tp{tp}_sl10_long"
            before = _tpsl_cell(tpsl_v1, horizon, tp, 10, "long")
            after = _tpsl_cell(tpsl, horizon, tp, 10, "long")
            tpsl_delta[key] = {
                "before_n": before.get("n_valid_starts"),
                "before_tp_first": before.get("n_tp_first"),
                "after_n": after.get("n_valid_starts"),
                "after_tp_first": after.get("n_tp_first"),
                "after_data_invalid": after.get("n_data_invalid"),
                "after_stale": after.get("n_tp_or_sl_resolved_on_stale_or_quote_fallback"),
            }

    rca = build_rca()
    cost = audit_cost_decomposition(
        median_spread_bps=median_spread,
        holding_seconds=300.0,
        latency_bps_per_side=CURRENT_MODELED_LATENCY_BPS_PER_SIDE,
    )
    v1_full = json.loads(FULL_CORPUS_DOC.read_text(encoding="utf-8"))
    fp_report = {
        **fp,
        "report_title": "# ETH first-passage full-corpus clean v1",
        "STATUS": "ETH_FIRST_PASSAGE_FULL_CORPUS_CLEAN_READY",
        "ML_STATUS": "NOT_STARTED",
        "DECISION": "STOP_FOR_LEAD_REVIEW",
        "corpus": {
            "discovery_utc_dates": dates,
            "discovery_usable_hours": usable_hours,
            "discovery_rows": len(epoch),
            "oos_untouched": list(V1_UNTOUCHED_OOS_UTC_DATES),
            "price_path_on_oos": False,
            "market_state_run": str(NEW_MS),
        },
        "corpus_split": v1_full.get("corpus_split") or v1_full.get("corpus"),
        "b2_inventory": v1_full.get("b2_inventory") or {},
        "cost_layer": {
            "taker_rt_break_even_bps_observed": taker_rt,
            "median_spread_bps": median_spread,
        },
        "day_block_stability": stability,
        "aug6_vs_expanded": {},
        "economic_frequency_commentary": [],
        "eligibility": EXECUTABLE_TOB_ELIGIBILITY,
        "tob_source_mix": source_mix,
    }
    tpsl_status = (
        "ETH_TP_SL_FIRST_TOUCH_FEASIBILITY_CLEAN_BLOCKED_BAD_DATA"
        if contamination.get("escalate")
        else "ETH_TP_SL_FIRST_TOUCH_FEASIBILITY_CLEAN_READY"
    )
    tpsl_report = {
        **tpsl,
        "report_title": "# ETH TP×SL first-touch feasibility clean v1",
        "STATUS": tpsl_status,
        "ML_STATUS": "NOT_STARTED",
        "DECISION": "STOP_FOR_LEAD_REVIEW",
        "cost_audit": cost,
        "forensic_qa": forensic,
        "corpus": {
            "discovery_utc_dates": dates,
            "discovery_usable_hours": usable_hours,
            "discovery_rows": len(epoch),
            "dropped_gap_or_invalid": dropped,
            "oos_untouched": list(V1_UNTOUCHED_OOS_UTC_DATES),
            "price_path_on_oos": False,
            "market_state_run": str(NEW_MS),
        },
        "eligibility": EXECUTABLE_TOB_ELIGIBILITY,
        "contamination_proof": proof,
        "tob_source_mix": source_mix,
    }

    wall_seconds = time.perf_counter() - started
    rss_end = _peak_rss_mib()
    remediation: dict[str, Any] = {
        "STATUS": "ETH_EXECUTABLE_PATH_QUALITY_REMEDIATION_READY",
        "ML_STATUS": "NOT_STARTED",
        "DECISION": "STOP_FOR_LEAD_REVIEW",
        "eligibility_freeze": EXECUTABLE_TOB_ELIGIBILITY,
        "root_cause": rca,
        "rebuild": {
            "runs_root": str(NEW_MS),
            "force_rebuild": False,
            "errors": materialize.get("errors"),
            "day_outputs": materialize.get("day_outputs"),
            "tob_source_mix": source_mix,
            "dropped_gap_or_invalid": dropped,
        },
        "contamination_proof": proof,
        "wide_native_bbo": wide_bbo,
        "before_after": {
            "first_passage": fp_delta,
            "tp_sl": tpsl_delta,
            "aug19_120s_tp20_sl10_long": {
                "before": _day_tp_rate(tpsl_v1, "2026-08-19", 120),
                "after": _day_tp_rate(tpsl, "2026-08-19", 120),
            },
            "aug20_120s_tp20_sl10_long": {
                "before": _day_tp_rate(tpsl_v1, "2026-08-20", 120),
                "after": _day_tp_rate(tpsl, "2026-08-20", 120),
            },
            "discovery_hours": {
                "before": v1_full.get("corpus", {}).get("discovery_usable_hours"),
                "after": usable_hours,
            },
        },
        "runtime": {
            "wall_seconds": wall_seconds,
            "rss_end_mib": rss_end,
        },
        "versioned_reports": {
            "first_passage": "docs/eth_first_passage_full_corpus_clean_v1.md",
            "tp_sl": "docs/eth_tp_sl_first_touch_feasibility_clean_v1.md",
        },
    }

    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "eth_first_passage_full_corpus_clean_v1.json").write_text(
        json.dumps(fp_report, indent=2, default=str), encoding="utf-8"
    )
    (DOCS / "eth_first_passage_full_corpus_clean_v1.md").write_text(
        render_full_corpus_markdown(fp_report), encoding="utf-8"
    )
    (DOCS / "eth_tp_sl_first_touch_feasibility_clean_v1.json").write_text(
        json.dumps(tpsl_report, indent=2, default=str), encoding="utf-8"
    )
    (DOCS / "eth_tp_sl_first_touch_feasibility_clean_v1.md").write_text(
        render_tp_sl_markdown(tpsl_report), encoding="utf-8"
    )
    (DOCS / "eth_executable_path_quality_remediation_v1.json").write_text(
        json.dumps(remediation, indent=2, default=str), encoding="utf-8"
    )
    (DOCS / "eth_executable_path_quality_remediation_v1.md").write_text(
        _render_remediation_md(remediation), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "phase": "done",
                "STATUS": remediation["STATUS"],
                "tp_sl_status": tpsl_status,
                "max_stale_frac": proof.get("max_contaminated_resolution_fraction"),
                "wall_seconds": wall_seconds,
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
