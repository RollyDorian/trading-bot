"""Read-only diagnostics for full-corpus research pipeline artifacts."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped]

_PERCENTILES = (0.01, 0.05, 0.50, 0.95, 0.99)
_LABEL_RE = re.compile(r"^fwd_ret_(\d+)s_bps$")


def _percentile(sorted_values: Sequence[float], quantile: float) -> float | None:
    if not sorted_values:
        return None
    position = (len(sorted_values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _numeric_summary(values: Iterable[Any]) -> dict[str, Any]:
    materialized = list(values)
    finite = sorted(
        float(value)
        for value in materialized
        if value is not None and math.isfinite(float(value))
    )
    return {
        "count": len(materialized),
        "null_count": sum(value is None for value in materialized),
        "finite_count": len(finite),
        "non_finite_count": sum(
            value is not None and not math.isfinite(float(value)) for value in materialized
        ),
        "percentiles": {
            f"p{int(quantile * 100):02d}": _percentile(finite, quantile)
            for quantile in _PERCENTILES
        },
    }


def summarize_market_state(path: Path) -> dict[str, Any]:
    """Summarize temporal coverage, valid books, and as-of source ages."""

    rows = pq.read_table(path).to_pylist()
    times = [row["decision_time"] for row in rows]
    start = min(times) if times else None
    end = max(times) if times else None
    expected_rows = int((end - start).total_seconds()) + 1 if start and end else 0
    ages = {
        source: _numeric_summary(row.get(f"{source}_age_seconds") for row in rows)
        for source in ("book", "quote", "mark", "spot", "funding")
    }
    valid_count = sum(bool(row.get("valid_book")) for row in rows)
    return {
        "rows": len(rows),
        "coverage": {
            "start": start,
            "end": end,
            "expected_1s_rows": expected_rows,
            "observed_pct": (len(rows) / expected_rows * 100.0) if expected_rows else None,
        },
        "valid_book_pct": (valid_count / len(rows) * 100.0) if rows else None,
        "age_seconds": ages,
    }


def summarize_features(path: Path) -> dict[str, Any]:
    """Return null, finite, and distribution diagnostics for every feature."""

    table = pq.read_table(path)
    excluded = {"decision_time", "latest_raw_event_id", "research_pipeline_version"}
    return {
        column: _numeric_summary(table[column].to_pylist())
        for column in table.column_names
        if column not in excluded
    }


def summarize_labels(path: Path) -> dict[str, Any]:
    """Return usable label counts and forward-return distributions by horizon."""

    table = pq.read_table(path)
    summaries: dict[str, Any] = {}
    for column in table.column_names:
        match = _LABEL_RE.match(column)
        if match:
            summary = _numeric_summary(table[column].to_pylist())
            summaries[match.group(1)] = {
                "usable_count": summary["finite_count"],
                "return_bps": summary,
            }
    return {"rows": table.num_rows, "horizons_seconds": summaries}


def _average_ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        average_rank = (cursor + end - 1) / 2.0 + 1.0
        for index in order[cursor:end]:
            ranks[index] = average_rank
        cursor = end
    return ranks


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 2:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    covariance = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right, strict=True)
    )
    left_ss = sum((value - left_mean) ** 2 for value in left)
    right_ss = sum((value - right_mean) ** 2 for value in right)
    denominator = math.sqrt(left_ss * right_ss)
    return covariance / denominator if denominator else None


def exploratory_ic(
    features_path: Path,
    labels_path: Path,
    feature_cols: Iterable[str],
    horizons: Iterable[int],
) -> dict[str, dict[int, dict[str, Any]]]:
    """Calculate exploratory Spearman IC using only finite timestamp-aligned rows."""

    feature_rows = {
        row["decision_time"]: row for row in pq.read_table(features_path).to_pylist()
    }
    label_rows = {
        row["decision_time"]: row for row in pq.read_table(labels_path).to_pylist()
    }
    common_times = sorted(feature_rows.keys() & label_rows.keys())
    result: dict[str, dict[int, dict[str, Any]]] = {}
    for feature in feature_cols:
        result[feature] = {}
        for horizon in horizons:
            label_column = f"fwd_ret_{horizon}s_bps"
            pairs = [
                (float(feature_rows[ts][feature]), float(label_rows[ts][label_column]))
                for ts in common_times
                if feature_rows[ts].get(feature) is not None
                and label_rows[ts].get(label_column) is not None
                and math.isfinite(float(feature_rows[ts][feature]))
                and math.isfinite(float(label_rows[ts][label_column]))
            ]
            left = [pair[0] for pair in pairs]
            right = [pair[1] for pair in pairs]
            result[feature][horizon] = {
                "rows": len(pairs),
                "spearman_ic": _pearson(_average_ranks(left), _average_ranks(right)),
            }
    return result


def leakage_checks(
    market_state_path: Path,
    features_path: Path,
    labels_path: Path,
) -> dict[str, Any]:
    """Assert basic temporal and schema invariants that prevent feature leakage."""

    market_rows = pq.read_table(market_state_path).to_pylist()
    feature_table = pq.read_table(features_path)
    feature_rows = feature_table.to_pylist()
    label_table = pq.read_table(labels_path)
    label_rows = label_table.to_pylist()

    assert not any(column.startswith("fwd_ret_") for column in feature_table.column_names)
    for name, rows in (
        ("market_state", market_rows),
        ("features", feature_rows),
        ("labels", label_rows),
    ):
        times = [row["decision_time"] for row in rows]
        assert times == sorted(times), f"{name} decision_time is not sorted"
        assert len(times) == len(set(times)), f"{name} decision_time contains duplicates"

    market_by_time = {row["decision_time"]: row for row in market_rows}
    label_by_time = {row["decision_time"]: row for row in label_rows}
    horizons = sorted(
        int(match.group(1))
        for column in label_table.column_names
        if (match := _LABEL_RE.match(column))
    )
    assert set(label_by_time) == set(market_by_time), "labels must cover market-state rows"
    for decision_time, market_row in market_by_time.items():
        current_mid = market_row["mid"]
        for horizon in horizons:
            future = market_by_time.get(decision_time + timedelta(seconds=horizon))
            expected = (
                None
                if future is None or current_mid in (None, 0)
                else (future["mid"] / current_mid - 1.0) * 10_000
            )
            actual = label_by_time[decision_time][f"fwd_ret_{horizon}s_bps"]
            if expected is None:
                assert actual is None, (
                    f"label at {decision_time!s} horizon {horizon}s has no future mid"
                )
            else:
                assert actual is not None and math.isclose(
                    float(actual), float(expected), rel_tol=1e-12, abs_tol=1e-12
                ), f"label at {decision_time!s} horizon {horizon}s mismatches future mid"

    return {
        "rows_checked": len(market_rows),
        "horizons_seconds": horizons,
        "feature_columns_checked": len(feature_table.column_names),
    }
