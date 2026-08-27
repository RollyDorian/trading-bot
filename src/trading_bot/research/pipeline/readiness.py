"""ML data-readiness gate based on temporal and regime coverage.

RAW row count is intentionally not the primary criterion: millions of events
in few calendar days are not many independent regime observations.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pyarrow.parquet as pq  # type: ignore[import-untyped]

CoverageVerdict = Literal["sufficient", "insufficient", "unknown"]


# Conservative initial target: ~2 calendar weeks of usable 1s market state with
# multi-regime coverage. Reasoning: short-horizon microstructure edges need
# multiple independent UTC days to separate luck from stability; 4 days proved
# insufficient in full-corpus validation. Prefer 14–28 days when continuous.
DEFAULT_TARGET_CALENDAR_DAYS = 14
DEFAULT_TARGET_USABLE_HOURS = 14 * 18  # allow overnight thinness; not 24×14
DEFAULT_TARGET_VERIFIED_GENERATIONS = 3
DEFAULT_MIN_REGIME_SHARE = 0.10  # each tertile regime should cover ≥10% of rows
DEFAULT_MIN_VALID_BOOK_PCT = 95.0


@dataclass(frozen=True, slots=True)
class ReadinessTargets:
    calendar_days: int = DEFAULT_TARGET_CALENDAR_DAYS
    usable_hours: float = float(DEFAULT_TARGET_USABLE_HOURS)
    verified_generations: int = DEFAULT_TARGET_VERIFIED_GENERATIONS
    min_regime_share: float = DEFAULT_MIN_REGIME_SHARE
    min_valid_book_pct: float = DEFAULT_MIN_VALID_BOOK_PCT


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def assign_tertile_regimes(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Causal regime labels from contemporaneous market_state fields only."""

    def _tertiles(values: list[float]) -> tuple[float, float] | None:
        if len(values) < 30:
            return None
        ordered = sorted(values)
        return ordered[len(ordered) // 3], ordered[(2 * len(ordered)) // 3]

    spreads = [float(r["spread_bps"]) for r in rows if r.get("spread_bps") is not None]
    vols = [float(r["rv_60s_bps"]) for r in rows if r.get("rv_60s_bps") is not None]
    trades = [float(r["trade_count"]) for r in rows if r.get("trade_count") is not None]
    spread_cut = _tertiles(spreads)
    vol_cut = _tertiles(vols)
    trade_cut = _tertiles(trades)

    counts: Counter[str] = Counter()
    for row in rows:
        flags: list[str] = []
        if spread_cut is not None and row.get("spread_bps") is not None:
            value = float(row["spread_bps"])
            if value <= spread_cut[0]:
                flags.append("spread_tight")
            elif value >= spread_cut[1]:
                flags.append("spread_wide")
            else:
                flags.append("spread_medium")
        if vol_cut is not None and row.get("rv_60s_bps") is not None:
            value = float(row["rv_60s_bps"])
            if value <= vol_cut[0]:
                flags.append("vol_low")
            elif value >= vol_cut[1]:
                flags.append("vol_high")
            else:
                flags.append("vol_medium")
        if trade_cut is not None and row.get("trade_count") is not None:
            value = float(row["trade_count"])
            if value <= trade_cut[0]:
                flags.append("activity_low")
            elif value >= trade_cut[1]:
                flags.append("activity_high")
            else:
                flags.append("activity_medium")
        if row.get("ret_60s_bps") is not None:
            flags.append("trend_up" if float(row["ret_60s_bps"]) > 0 else "trend_down")
        for flag in flags:
            counts[flag] += 1
    total = max(1, len(rows))
    shares = {name: count / total for name, count in sorted(counts.items())}
    return {
        "rows": len(rows),
        "cuts": {
            "spread_bps": spread_cut,
            "rv_60s_bps": vol_cut,
            "trade_count": trade_cut,
        },
        "shares": shares,
        "counts": dict(counts),
    }


def continuous_intervals(
    times: list[datetime], *, max_gap_seconds: float = 5.0
) -> list[dict[str, Any]]:
    """Collapse sorted decision times into contiguous intervals."""

    if not times:
        return []
    ordered = sorted(times)
    intervals: list[dict[str, Any]] = []
    start = ordered[0]
    prev = ordered[0]
    count = 1
    for current in ordered[1:]:
        gap = (current - prev).total_seconds()
        if gap > max_gap_seconds:
            intervals.append(
                {
                    "start": start,
                    "end": prev,
                    "rows": count,
                    "hours": (prev - start).total_seconds() / 3600.0,
                }
            )
            start = current
            count = 0
        prev = current
        count += 1
    intervals.append(
        {
            "start": start,
            "end": prev,
            "rows": count,
            "hours": (prev - start).total_seconds() / 3600.0,
        }
    )
    return intervals


def summarize_market_state_coverage(market_state_path: Path) -> dict[str, Any]:
    rows = pq.read_table(market_state_path).to_pylist()
    if not rows:
        return {
            "rows": 0,
            "calendar_days": 0,
            "usable_hours": 0.0,
            "valid_book_pct": None,
            "intervals": [],
            "regimes": {"shares": {}, "counts": {}},
        }
    times = [_dt(row["decision_time"]) for row in rows]
    by_day = Counter(ts.astimezone(UTC).strftime("%Y-%m-%d") for ts in times)
    valid = sum(1 for row in rows if row.get("valid_book"))
    return {
        "rows": len(rows),
        "calendar_days": len(by_day),
        "rows_per_utc_day": dict(sorted(by_day.items())),
        "usable_hours": len(rows) / 3600.0,
        "span_hours": (max(times) - min(times)).total_seconds() / 3600.0,
        "valid_book_rows": valid,
        "valid_book_pct": 100.0 * valid / len(rows),
        "intervals": continuous_intervals(times),
        "regimes": assign_tertile_regimes(rows),
    }


def _regime_verdict(
    shares: dict[str, float],
    names: tuple[str, ...],
    *,
    min_share: float,
) -> CoverageVerdict:
    present = [shares.get(name, 0.0) for name in names]
    if not any(present):
        return "unknown"
    return "sufficient" if all(share >= min_share for share in present) else "insufficient"


def evaluate_data_readiness(
    *,
    exploratory_coverages: list[dict[str, Any]],
    verified_generation_ids: list[str],
    targets: ReadinessTargets | None = None,
    oos_holdout_clean: bool,
) -> dict[str, Any]:
    """Aggregate coverage into a fail-closed DATA_READY_FOR_ML status."""

    targets = targets or ReadinessTargets()
    calendar_days = sorted(
        {
            day
            for coverage in exploratory_coverages
            for day in (coverage.get("rows_per_utc_day") or {})
        }
    )
    usable_hours = sum(
        float(coverage.get("usable_hours") or 0.0) for coverage in exploratory_coverages
    )
    valid_book_pcts = [
        float(coverage["valid_book_pct"])
        for coverage in exploratory_coverages
        if coverage.get("valid_book_pct") is not None
    ]
    valid_book_pct = min(valid_book_pcts) if valid_book_pcts else None

    merged_shares: Counter[str] = Counter()
    total_rows = 0
    for coverage in exploratory_coverages:
        regimes = coverage.get("regimes") or {}
        counts = regimes.get("counts") or {}
        merged_shares.update(counts)
        total_rows += int(coverage.get("rows") or 0)
    shares = (
        {name: count / total_rows for name, count in merged_shares.items()}
        if total_rows
        else {}
    )

    regime_status = {
        "vol": _regime_verdict(
            shares, ("vol_low", "vol_medium", "vol_high"), min_share=targets.min_regime_share
        ),
        "spread": _regime_verdict(
            shares,
            ("spread_tight", "spread_medium", "spread_wide"),
            min_share=targets.min_regime_share,
        ),
        "activity": _regime_verdict(
            shares,
            ("activity_low", "activity_medium", "activity_high"),
            min_share=targets.min_regime_share,
        ),
        "trend": _regime_verdict(
            shares, ("trend_up", "trend_down"), min_share=targets.min_regime_share
        ),
    }

    checks = {
        "calendar_days": len(calendar_days) >= targets.calendar_days,
        "usable_hours": usable_hours >= targets.usable_hours,
        "verified_generations": len(verified_generation_ids)
        >= targets.verified_generations,
        "valid_book_pct": (
            valid_book_pct is not None and valid_book_pct >= targets.min_valid_book_pct
        ),
        "regimes_vol": regime_status["vol"] == "sufficient",
        "regimes_spread": regime_status["spread"] == "sufficient",
        "regimes_activity": regime_status["activity"] == "sufficient",
        "clean_oos_holdout": oos_holdout_clean,
    }
    ready = all(checks.values())
    return {
        "DATA_READY_FOR_ML": ready,
        "ACTION": "READY_FOR_ML_REVIEW" if ready else "CONTINUE_COLLECTION",
        "targets": asdict(targets),
        "calendar_days": {
            "observed": len(calendar_days),
            "target": targets.calendar_days,
            "days": calendar_days,
        },
        "usable_hours": {"observed": usable_hours, "target": targets.usable_hours},
        "verified_generations": {
            "observed": len(verified_generation_ids),
            "target": targets.verified_generations,
            "ids": list(verified_generation_ids),
        },
        "valid_book_pct": valid_book_pct,
        "regimes": {"shares": shares, "status": regime_status},
        "checks": checks,
        "reasoning": (
            "Primary gate is temporal/regime coverage, not RAW row count. "
            f"Initial target {targets.calendar_days} UTC days / "
            f"~{targets.usable_hours:.0f} usable hours / "
            f"{targets.verified_generations} verified generations."
        ),
        "evaluated_at_utc": datetime.now(UTC).isoformat(),
    }
