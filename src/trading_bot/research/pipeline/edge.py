"""Exploratory signal-strength characterization (no OOS threshold fitting)."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trading_bot.research.pipeline import LABEL_HORIZONS_SECONDS

PRIMARY_SIGNALS: tuple[str, ...] = (
    "imbalance",
    "microprice_dev_bps",
    "ofi_1s",
    "ofi_5s",
    "ofi_15s",
    "signed_trade_flow_1s",
)
QUANTILE_TAILS: tuple[float, ...] = (0.50, 0.25, 0.10, 0.05, 0.02, 0.01)
FRONTIER_ABS_QUANTILES: tuple[float, ...] = (0.50, 0.75, 0.90, 0.95, 0.98, 0.99)


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _finite(values: list[Any]) -> list[float]:
    out: list[float] = []
    for value in values:
        if value is None:
            continue
        number = float(value)
        if math.isfinite(number):
            out.append(number)
    return out


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    if q <= 0:
        return sorted_vals[0]
    if q >= 1:
        return sorted_vals[-1]
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    weight = pos - lo
    return sorted_vals[lo] * (1.0 - weight) + sorted_vals[hi] * weight


def join_features_labels(features_path: Path, labels_path: Path) -> list[dict[str, Any]]:
    features = {
        _dt(row["decision_time"]): row for row in pq.read_table(features_path).to_pylist()
    }
    labels = {
        _dt(row["decision_time"]): row for row in pq.read_table(labels_path).to_pylist()
    }
    rows: list[dict[str, Any]] = []
    for ts in sorted(features.keys() & labels.keys()):
        merged = dict(features[ts])
        merged.update(labels[ts])
        merged["decision_time"] = ts
        rows.append(merged)
    return rows


def quantile_thresholds(values: list[float], q: float) -> tuple[float | None, float | None]:
    ordered = sorted(values)
    if not ordered:
        return None, None
    return _percentile(ordered, q), _percentile(ordered, 1.0 - q)


def conditional_bucket_stats(returns: list[float]) -> dict[str, Any]:
    finite = _finite(returns)
    n = len(finite)
    if n == 0:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "win_rate_positive": None,
            "stderr": None,
            "gross_expected_bps": None,
        }
    mean = sum(finite) / n
    ordered = sorted(finite)
    median = ordered[n // 2]
    var = sum((x - mean) ** 2 for x in finite) / max(1, n - 1)
    stderr = math.sqrt(var / n) if n > 1 else None
    return {
        "n": n,
        "mean": mean,
        "median": median,
        "win_rate_positive": sum(1 for x in finite if x > 0) / n,
        "stderr": stderr,
        "gross_expected_bps": mean,
    }


def _signed_returns(feature_values: list[float], fwd_returns: list[float]) -> list[float]:
    signed: list[float] = []
    for feature, fwd in zip(feature_values, fwd_returns, strict=True):
        if feature > 0:
            signed.append(fwd)
        elif feature < 0:
            signed.append(-fwd)
    return signed


def characterize_signal(
    rows: list[dict[str, Any]],
    feature: str,
    horizon: int,
    *,
    min_bucket_n: int = 30,
) -> dict[str, Any]:
    label = f"fwd_ret_{horizon}s_bps"
    pairs = [
        (float(row[feature]), float(row[label]))
        for row in rows
        if row.get(feature) is not None
        and row.get(label) is not None
        and math.isfinite(float(row[feature]))
        and math.isfinite(float(row[label]))
    ]
    if not pairs:
        return {"feature": feature, "horizon_s": horizon, "buckets": {}}
    features = [pair[0] for pair in pairs]
    fwds = [pair[1] for pair in pairs]
    abs_feats = sorted(abs(value) for value in features)
    buckets: dict[str, Any] = {
        "all_signed": conditional_bucket_stats(_signed_returns(features, fwds)),
        "all_raw_fwd": conditional_bucket_stats(fwds),
    }
    for tail in QUANTILE_TAILS:
        cut = _percentile(abs_feats, 1.0 - tail)
        if cut is None:
            continue
        selected_f = [f for f, _ in pairs if abs(f) >= cut]
        selected_r = [r for f, r in pairs if abs(f) >= cut]
        stats = conditional_bucket_stats(_signed_returns(selected_f, selected_r))
        key = f"abs_top_{int(tail * 100)}pct"
        buckets[key] = {
            **stats,
            "threshold_abs": cut,
            "status": (
                "insufficient_sample"
                if stats["n"] < min_bucket_n and tail <= 0.02
                else "ok"
                if stats["n"] >= min_bucket_n
                else "small_sample"
            ),
        }
    return {"feature": feature, "horizon_s": horizon, "buckets": buckets}


def chronological_block_stability(
    rows: list[dict[str, Any]],
    feature: str,
    horizon: int,
    *,
    n_blocks: int = 4,
    min_bucket_n: int = 30,
) -> list[dict[str, Any]]:
    if not rows:
        return []
    ordered = sorted(rows, key=lambda row: row["decision_time"])
    block_size = max(1, len(ordered) // n_blocks)
    out: list[dict[str, Any]] = []
    for index in range(n_blocks):
        start = index * block_size
        end = len(ordered) if index == n_blocks - 1 else (index + 1) * block_size
        block = ordered[start:end]
        summary = characterize_signal(block, feature, horizon, min_bucket_n=min_bucket_n)
        out.append(
            {
                "block": index,
                "rows": len(block),
                "start": block[0]["decision_time"] if block else None,
                "end": block[-1]["decision_time"] if block else None,
                "all_signed": summary["buckets"].get("all_signed"),
                "abs_top_10pct": summary["buckets"].get("abs_top_10pct"),
            }
        )
    return out


def trade_frequency_frontier(
    rows: list[dict[str, Any]],
    feature: str,
    horizon: int,
    thresholds: list[float],
    *,
    friction_bps_round_trip: float,
    seconds_span: float,
) -> list[dict[str, Any]]:
    label = f"fwd_ret_{horizon}s_bps"
    days = max(seconds_span / 86400.0, 1e-9)
    frontier: list[dict[str, Any]] = []
    for threshold in thresholds:
        selected_f: list[float] = []
        selected_r: list[float] = []
        for row in rows:
            if row.get(feature) is None or row.get(label) is None:
                continue
            feature_value = float(row[feature])
            fwd = float(row[label])
            if not (math.isfinite(feature_value) and math.isfinite(fwd)):
                continue
            if abs(feature_value) >= threshold:
                selected_f.append(feature_value)
                selected_r.append(fwd)
        stats = conditional_bucket_stats(_signed_returns(selected_f, selected_r))
        gross = stats["gross_expected_bps"]
        net = None if gross is None else gross - friction_bps_round_trip
        frontier.append(
            {
                "threshold_abs": threshold,
                "trades": stats["n"],
                "trades_per_day": stats["n"] / days,
                "gross_bps_per_trade": gross,
                "friction_bps_round_trip": friction_bps_round_trip,
                "net_bps_per_trade": net,
                "stderr": stats["stderr"],
                "win_rate_positive_signed": stats["win_rate_positive"],
                "economic_status": (
                    "UNKNOWN"
                    if net is None
                    else ("TRADEABLE" if net > 0 else "NOT_TRADEABLE")
                ),
                "break_even_friction_bps": None if gross is None else abs(gross),
            }
        )
    return frontier


def break_even_bps(gross_edge_bps: float) -> float:
    return abs(float(gross_edge_bps))


def predeclared_conjunctions(
    rows: list[dict[str, Any]], horizon: int
) -> list[dict[str, Any]]:
    label = f"fwd_ret_{horizon}s_bps"
    imb = _finite([row.get("imbalance") for row in rows])
    ofi = _finite([row.get("ofi_5s") for row in rows])
    micro = _finite([row.get("microprice_dev_bps") for row in rows])
    spreads = _finite([row.get("spread_bps") for row in rows])
    med_abs_imb = _percentile(sorted(abs(x) for x in imb), 0.5) or 0.0
    med_abs_ofi = _percentile(sorted(abs(x) for x in ofi), 0.5) or 0.0
    p90_abs_micro = _percentile(sorted(abs(x) for x in micro), 0.9) or 0.0
    p90_abs_ofi = _percentile(sorted(abs(x) for x in ofi), 0.9) or 0.0
    med_spread = _percentile(sorted(spreads), 0.5) or 0.0

    defs: list[tuple[str, Any, Any]] = [
        (
            "imbalance_and_ofi5_same_sign_median",
            lambda r: (
                r.get("imbalance") is not None
                and r.get("ofi_5s") is not None
                and abs(float(r["imbalance"])) >= med_abs_imb
                and abs(float(r["ofi_5s"])) >= med_abs_ofi
                and (float(r["imbalance"]) > 0) == (float(r["ofi_5s"]) > 0)
            ),
            lambda r: float(r["imbalance"]),
        ),
        (
            "microprice_extreme_and_narrow_spread",
            lambda r: (
                r.get("microprice_dev_bps") is not None
                and r.get("spread_bps") is not None
                and abs(float(r["microprice_dev_bps"])) >= p90_abs_micro
                and float(r["spread_bps"]) <= med_spread
            ),
            lambda r: float(r["microprice_dev_bps"]),
        ),
        (
            "ofi5_extreme_and_trade_activity",
            lambda r: (
                r.get("ofi_5s") is not None
                and r.get("trade_count") is not None
                and abs(float(r["ofi_5s"])) >= p90_abs_ofi
                and float(r["trade_count"]) >= 1
            ),
            lambda r: float(r["ofi_5s"]),
        ),
    ]
    results: list[dict[str, Any]] = []
    for name, predicate, feature_of in defs:
        selected_f: list[float] = []
        selected_r: list[float] = []
        for row in rows:
            if row.get(label) is None or not predicate(row):
                continue
            fwd = float(row[label])
            if not math.isfinite(fwd):
                continue
            selected_f.append(feature_of(row))
            selected_r.append(fwd)
        results.append(
            {
                "name": name,
                "horizon_s": horizon,
                "stats": conditional_bucket_stats(
                    _signed_returns(selected_f, selected_r)
                ),
            }
        )
    return results


def regime_conditioned_edge(
    rows: list[dict[str, Any]],
    feature: str,
    horizon: int,
    regime_by_time: dict[datetime, list[str]],
) -> dict[str, Any]:
    label = f"fwd_ret_{horizon}s_bps"
    by_regime: dict[str, list[tuple[float, float]]] = {}
    for row in rows:
        ts = _dt(row["decision_time"])
        if row.get(feature) is None or row.get(label) is None:
            continue
        feature_value = float(row[feature])
        fwd = float(row[label])
        if not (math.isfinite(feature_value) and math.isfinite(fwd)):
            continue
        for regime in regime_by_time.get(ts, []):
            by_regime.setdefault(regime, []).append((feature_value, fwd))
    out: dict[str, Any] = {}
    for regime, pairs in sorted(by_regime.items()):
        feats = [pair[0] for pair in pairs]
        fwds = [pair[1] for pair in pairs]
        out[regime] = conditional_bucket_stats(_signed_returns(feats, fwds))
    return out


def characterize_exploratory_corpus(
    features_path: Path,
    labels_path: Path,
    market_state_path: Path,
    *,
    friction_bps_round_trip: float,
) -> dict[str, Any]:
    """Full exploratory characterization; pass exploratory paths only."""

    from trading_bot.research.pipeline.readiness import assign_tertile_regimes

    rows = join_features_labels(features_path, labels_path)
    market_rows = pq.read_table(market_state_path).to_pylist()
    market_by_time = {_dt(row["decision_time"]): row for row in market_rows}
    attach_keys = (
        "spread_bps",
        "trade_count",
        "best_bid",
        "best_ask",
        "bid_size",
        "ask_size",
        "rv_60s_bps",
        "ret_60s_bps",
    )
    for row in rows:
        market = market_by_time.get(_dt(row["decision_time"]))
        if market is None:
            continue
        for key in attach_keys:
            if key not in row and key in market:
                row[key] = market[key]

    regimes = assign_tertile_regimes(market_rows)
    cuts = regimes.get("cuts") or {}
    regime_by_time: dict[datetime, list[str]] = {}
    spread_cut = cuts.get("spread_bps")
    vol_cut = cuts.get("rv_60s_bps")
    trade_cut = cuts.get("trade_count")
    for row in market_rows:
        flags: list[str] = []
        ts = _dt(row["decision_time"])
        if spread_cut and row.get("spread_bps") is not None:
            value = float(row["spread_bps"])
            flags.append(
                "spread_tight"
                if value <= spread_cut[0]
                else "spread_wide"
                if value >= spread_cut[1]
                else "spread_medium"
            )
        if vol_cut and row.get("rv_60s_bps") is not None:
            value = float(row["rv_60s_bps"])
            flags.append(
                "vol_low"
                if value <= vol_cut[0]
                else "vol_high"
                if value >= vol_cut[1]
                else "vol_medium"
            )
        if trade_cut and row.get("trade_count") is not None:
            value = float(row["trade_count"])
            flags.append(
                "activity_low"
                if value <= trade_cut[0]
                else "activity_high"
                if value >= trade_cut[1]
                else "activity_medium"
            )
        if row.get("ret_60s_bps") is not None:
            flags.append("trend_up" if float(row["ret_60s_bps"]) > 0 else "trend_down")
        regime_by_time[ts] = flags

    span = (
        (_dt(rows[-1]["decision_time"]) - _dt(rows[0]["decision_time"])).total_seconds()
        if rows
        else 0.0
    )

    signal_reports: list[dict[str, Any]] = []
    for feature in PRIMARY_SIGNALS:
        values = _finite([row.get(feature) for row in rows])
        abs_vals = sorted(abs(value) for value in values)
        thresholds = [
            thr
            for q in FRONTIER_ABS_QUANTILES
            if (thr := _percentile(abs_vals, q)) is not None
        ]
        for horizon in LABEL_HORIZONS_SECONDS:
            report = characterize_signal(rows, feature, horizon)
            report["stability_blocks"] = chronological_block_stability(
                rows, feature, horizon
            )
            report["frontier"] = trade_frequency_frontier(
                rows,
                feature,
                horizon,
                thresholds,
                friction_bps_round_trip=friction_bps_round_trip,
                seconds_span=span,
            )
            report["regimes"] = regime_conditioned_edge(
                rows, feature, horizon, regime_by_time
            )
            signal_reports.append(report)

    conjunctions = [
        item
        for horizon in LABEL_HORIZONS_SECONDS
        for item in predeclared_conjunctions(rows, horizon)
    ]
    return {
        "scope": "exploratory_only",
        "rows": len(rows),
        "seconds_span": span,
        "friction_bps_round_trip": friction_bps_round_trip,
        "signals": signal_reports,
        "conjunctions": conjunctions,
        "regime_cuts": cuts,
    }
