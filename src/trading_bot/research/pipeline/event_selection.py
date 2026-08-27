"""Causal event selection and required-move framing (exploratory only)."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from trading_bot.research.pipeline.edge import (
    _percentile,
    _signed_returns,
    conditional_bucket_stats,
)

# Predeclared event classes — no combinatorial mining.
EventPredicate = Callable[[dict[str, Any]], bool]


def required_move_bps(
    *,
    entry_fee_bps: float,
    exit_fee_bps: float,
    spread_bps: float,
    slippage_bps: float,
    latency_bps: float,
    funding_bps: float,
    adverse_selection_bps: float = 0.0,
    queue_or_nonfill_penalty_bps: float = 0.0,
) -> float:
    """Minimum expected signed move to break even under an execution style."""

    return (
        entry_fee_bps
        + exit_fee_bps
        + spread_bps
        + slippage_bps
        + latency_bps
        + funding_bps
        + adverse_selection_bps
        + queue_or_nonfill_penalty_bps
    )


def predeclared_event_classes(rows: list[dict[str, Any]]) -> dict[str, EventPredicate]:
    flow = [
        abs(float(row["signed_trade_flow_1s"]))
        for row in rows
        if row.get("signed_trade_flow_1s") is not None
        and math.isfinite(float(row["signed_trade_flow_1s"]))
    ]
    ofi = [
        abs(float(row["ofi_5s"]))
        for row in rows
        if row.get("ofi_5s") is not None and math.isfinite(float(row["ofi_5s"]))
    ]
    micro = [
        abs(float(row["microprice_dev_bps"]))
        for row in rows
        if row.get("microprice_dev_bps") is not None
        and math.isfinite(float(row["microprice_dev_bps"]))
    ]
    imb = [
        abs(float(row["imbalance"]))
        for row in rows
        if row.get("imbalance") is not None and math.isfinite(float(row["imbalance"]))
    ]
    spreads = [
        float(row["spread_bps"])
        for row in rows
        if row.get("spread_bps") is not None and math.isfinite(float(row["spread_bps"]))
    ]
    p99_flow = _percentile(sorted(flow), 0.99) or 0.0
    p90_ofi = _percentile(sorted(ofi), 0.90) or 0.0
    p90_micro = _percentile(sorted(micro), 0.90) or 0.0
    p90_imb = _percentile(sorted(imb), 0.90) or 0.0
    med_spread = _percentile(sorted(spreads), 0.50) or 0.0

    def extreme_signed_trade_flow_p99(row: dict[str, Any], *, thr: float = p99_flow) -> bool:
        return (
            row.get("signed_trade_flow_1s") is not None
            and abs(float(row["signed_trade_flow_1s"])) >= thr
        )

    def ofi5_and_imbalance_agree_p90(
        row: dict[str, Any], *, othr: float = p90_ofi, ithr: float = p90_imb
    ) -> bool:
        return (
            row.get("ofi_5s") is not None
            and row.get("imbalance") is not None
            and abs(float(row["ofi_5s"])) >= othr
            and abs(float(row["imbalance"])) >= ithr
            and (float(row["ofi_5s"]) > 0) == (float(row["imbalance"]) > 0)
        )

    def microprice_extreme_narrow_spread(
        row: dict[str, Any], *, mthr: float = p90_micro, sthr: float = med_spread
    ) -> bool:
        return (
            row.get("microprice_dev_bps") is not None
            and row.get("spread_bps") is not None
            and abs(float(row["microprice_dev_bps"])) >= mthr
            and float(row["spread_bps"]) <= sthr
        )

    def extreme_flow_and_activity(row: dict[str, Any], *, thr: float = p99_flow) -> bool:
        return (
            row.get("signed_trade_flow_1s") is not None
            and row.get("trade_count") is not None
            and abs(float(row["signed_trade_flow_1s"])) >= thr
            and float(row["trade_count"]) >= 1
        )

    return {
        "extreme_signed_trade_flow_p99": extreme_signed_trade_flow_p99,
        "ofi5_and_imbalance_agree_p90": ofi5_and_imbalance_agree_p90,
        "microprice_extreme_narrow_spread": microprice_extreme_narrow_spread,
        "extreme_flow_and_activity": extreme_flow_and_activity,
    }


def evaluate_event_class(
    rows: list[dict[str, Any]],
    *,
    name: str,
    predicate: EventPredicate,
    feature_for_sign: str,
    horizon_s: int,
    required_bps: float,
    seconds_span: float,
) -> dict[str, Any]:
    label = f"fwd_ret_{horizon_s}s_bps"
    selected_f: list[float] = []
    selected_r: list[float] = []
    for row in rows:
        if not predicate(row) or row.get(label) is None or row.get(feature_for_sign) is None:
            continue
        feature_value = float(row[feature_for_sign])
        fwd = float(row[label])
        if not (math.isfinite(feature_value) and math.isfinite(fwd)):
            continue
        selected_f.append(feature_value)
        selected_r.append(fwd)
    stats = conditional_bucket_stats(_signed_returns(selected_f, selected_r))
    gross = stats.get("gross_expected_bps")
    days = max(seconds_span / 86400.0, 1e-9)
    events = int(stats.get("n") or 0)
    return {
        "name": name,
        "horizon_s": horizon_s,
        "events": events,
        "events_per_day": events / days,
        "gross_bps": gross,
        "required_move_bps": required_bps,
        "clears_required_move": (
            None if gross is None else float(gross) > required_bps
        ),
        "stderr": stats.get("stderr"),
        "win_rate_positive_signed": stats.get("win_rate_positive"),
        "sample_status": (
            "insufficient"
            if events < 30
            else "thin"
            if events < 100
            else "ok"
        ),
    }
