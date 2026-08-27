"""Feature / label v1 specifications and writers for market_state_1s."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trading_bot.research.pipeline import LABEL_HORIZONS_SECONDS, RESEARCH_PIPELINE_VERSION

FEATURES_V1 = (
    "spread_bps",
    "imbalance",
    "microprice_dev_bps",
    "signed_trade_flow_1s",
    "ofi_1s",
    "ofi_5s",
    "ofi_15s",
    "buy_volume",
    "sell_volume",
    "trade_count",
    "ret_1s_bps",
    "ret_5s_bps",
    "ret_15s_bps",
    "ret_60s_bps",
    "rv_5s_bps",
    "rv_15s_bps",
    "rv_60s_bps",
    "basis_mark_bps",
    "basis_spot_bps",
    "funding_rate",
    "book_age_seconds",
    "quote_age_seconds",
    "mark_age_seconds",
    "valid_book",
)


def write_features_v1(market_state_path: Path, output_path: Path) -> dict[str, Any]:
    table = pq.read_table(market_state_path)
    cols = ["decision_time", "latest_raw_event_id", *FEATURES_V1]
    missing = [c for c in cols if c not in table.column_names]
    if missing:
        raise ValueError(f"market_state missing feature columns: {missing}")
    out = table.select(cols)
    # Attach pipeline version as metadata-only column for reproducibility.
    n = out.num_rows
    out = out.append_column(
        "research_pipeline_version",
        pa.array([RESEARCH_PIPELINE_VERSION] * n, type=pa.int32()),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(out, output_path, compression="zstd")
    return {"rows": n, "features": list(FEATURES_V1), "path": str(output_path)}


def write_labels_v1(market_state_path: Path, output_path: Path) -> dict[str, Any]:
    """Future mid returns (bps). Labels may look ahead; never join into features."""

    rows = pq.read_table(market_state_path).to_pylist()
    by_time = {r["decision_time"]: r for r in rows}
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
        for horizon in LABEL_HORIZONS_SECONDS:
            # decision_time is second-aligned; target is mid at ts+horizon.
            from datetime import timedelta

            target = ts + timedelta(seconds=horizon)
            future = by_time.get(target)
            if future is None or mid in (None, 0):
                label[f"fwd_ret_{horizon}s_bps"] = None
            else:
                label[f"fwd_ret_{horizon}s_bps"] = (future["mid"] / mid - 1.0) * 10_000
        out_rows.append(label)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(out_rows), output_path, compression="zstd")
    return {
        "rows": len(out_rows),
        "horizons_seconds": list(LABEL_HORIZONS_SECONDS),
        "definition": "forward mid-price return in bps at decision_time + horizon",
        "path": str(output_path),
        "overlap_note": (
            "adjacent labels overlap in time; use purge/embargo in validation splits"
        ),
    }
