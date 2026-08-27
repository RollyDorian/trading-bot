"""Longer-horizon labels, predictive decay, and simple extended baselines."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trading_bot.research.pipeline import RESEARCH_PIPELINE_VERSION
from trading_bot.research.pipeline.edge import (
    PRIMARY_SIGNALS,
    _percentile,
    _signed_returns,
    conditional_bucket_stats,
)

# Existing short horizons plus extended exploratory set.
EXTENDED_LABEL_HORIZONS_SECONDS: tuple[int, ...] = (
    5,
    15,
    30,
    60,
    120,
    300,
    600,
)


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def write_labels_extended(
    market_state_path: Path,
    output_path: Path,
    *,
    horizons: tuple[int, ...] = EXTENDED_LABEL_HORIZONS_SECONDS,
) -> dict[str, Any]:
    """Forward mid returns for short and longer horizons (labels look ahead)."""

    rows = pq.read_table(market_state_path).to_pylist()
    by_time = {_dt(row["decision_time"]): row for row in rows}
    times = sorted(by_time)
    out_rows: list[dict[str, Any]] = []
    for ts in times:
        row = by_time[ts]
        mid = row["mid"]
        label: dict[str, Any] = {
            "decision_time": ts,
            "latest_raw_event_id": row.get("latest_raw_event_id"),
            "research_pipeline_version": RESEARCH_PIPELINE_VERSION,
        }
        for horizon in horizons:
            target = ts + timedelta(seconds=horizon)
            future = by_time.get(target)
            if future is None or mid in (None, 0):
                label[f"fwd_ret_{horizon}s_bps"] = None
            else:
                label[f"fwd_ret_{horizon}s_bps"] = (
                    float(future["mid"]) / float(mid) - 1.0
                ) * 10_000
        out_rows.append(label)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(out_rows), output_path, compression="zstd")
    return {
        "rows": len(out_rows),
        "horizons_seconds": list(horizons),
        "path": str(output_path),
    }


def horizon_decay_report(
    feature_rows: list[dict[str, Any]],
    label_rows: list[dict[str, Any]],
    *,
    features: tuple[str, ...] = PRIMARY_SIGNALS,
    horizons: tuple[int, ...] = EXTENDED_LABEL_HORIZONS_SECONDS,
    extreme_tail: float = 0.01,
) -> dict[str, Any]:
    """IC-like signed mean + extreme-bucket gross across horizons (exploratory)."""

    labels_by_time = {_dt(row["decision_time"]): row for row in label_rows}
    rows: list[dict[str, Any]] = []
    for row in feature_rows:
        ts = _dt(row["decision_time"])
        label = labels_by_time.get(ts)
        if label is None:
            continue
        merged = dict(row)
        merged.update(label)
        merged["decision_time"] = ts
        rows.append(merged)

    curves: dict[str, list[dict[str, Any]]] = {}
    for feature in features:
        curves[feature] = []
        values = [
            float(row[feature])
            for row in rows
            if row.get(feature) is not None and math.isfinite(float(row[feature]))
        ]
        abs_cut = _percentile(sorted(abs(v) for v in values), 1.0 - extreme_tail)
        for horizon in horizons:
            label_key = f"fwd_ret_{horizon}s_bps"
            pairs = [
                (float(row[feature]), float(row[label_key]))
                for row in rows
                if row.get(feature) is not None
                and row.get(label_key) is not None
                and math.isfinite(float(row[feature]))
                and math.isfinite(float(row[label_key]))
            ]
            if not pairs:
                curves[feature].append({"horizon_s": horizon, "n": 0})
                continue
            feats = [p[0] for p in pairs]
            fwds = [p[1] for p in pairs]
            all_stats = conditional_bucket_stats(_signed_returns(feats, fwds))
            if abs_cut is None:
                extreme_stats = {"n": 0, "gross_expected_bps": None}
            else:
                ef = [f for f, _ in pairs if abs(f) >= abs_cut]
                er = [r for f, r in pairs if abs(f) >= abs_cut]
                extreme_stats = conditional_bucket_stats(_signed_returns(ef, er))
            curves[feature].append(
                {
                    "horizon_s": horizon,
                    "all_signed": all_stats,
                    "extreme_tail": extreme_tail,
                    "extreme_threshold_abs": abs_cut,
                    "extreme_signed": extreme_stats,
                }
            )
    return {"rows": len(rows), "curves": curves}


def simple_longer_horizon_baseline(
    rows: list[dict[str, Any]],
    *,
    feature: str,
    horizon_s: int,
    abs_threshold: float,
    friction_bps: float,
) -> dict[str, Any]:
    """Tiny predeclared event baseline: trade when |feature|>=thr; hold horizon."""

    label = f"fwd_ret_{horizon_s}s_bps"
    selected_f: list[float] = []
    selected_r: list[float] = []
    for row in rows:
        if row.get(feature) is None or row.get(label) is None:
            continue
        feature_value = float(row[feature])
        fwd = float(row[label])
        if not (math.isfinite(feature_value) and math.isfinite(fwd)):
            continue
        if abs(feature_value) >= abs_threshold:
            selected_f.append(feature_value)
            selected_r.append(fwd)
    stats = conditional_bucket_stats(_signed_returns(selected_f, selected_r))
    gross = stats.get("gross_expected_bps")
    net = None if gross is None else float(gross) - friction_bps
    return {
        "feature": feature,
        "horizon_s": horizon_s,
        "abs_threshold": abs_threshold,
        "trades": stats.get("n"),
        "gross_bps": gross,
        "friction_bps": friction_bps,
        "net_bps": net,
        "economic_status": (
            "UNKNOWN"
            if net is None
            else ("TRADEABLE" if net > 0 else "NOT_TRADEABLE")
        ),
        "stderr": stats.get("stderr"),
    }
