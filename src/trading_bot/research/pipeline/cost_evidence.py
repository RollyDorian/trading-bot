"""Decision-grade cost-model evidence for research (public sources only)."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped]

# Public Hibachi fee docs (no private account API).
HIBACHI_FEES_URL = "https://docs.hibachi.xyz/hibachi-docs/trading/fees"
HIBACHI_FEES_RETRIEVED_AT_UTC = "2026-08-11T14:00:00+00:00"


def hibachi_public_fee_schedule() -> dict[str, Any]:
    """Tier schedule from public docs; account tier remains unknown without private data."""

    return {
        "source_url": HIBACHI_FEES_URL,
        "retrieved_at_utc": HIBACHI_FEES_RETRIEVED_AT_UTC,
        "classification": "VERIFIED_CURRENT",
        "note": (
            "Public perpetual fee tiers. Research does not know operator account "
            "tier without private data; default scenario uses Tier 1, with a "
            "documented taker-fee range across tiers."
        ),
        "maker_fee_rate": 0.0,
        "tier1_taker_fee_rate": 0.00045,
        "taker_fee_rate_range": {
            "min_tier7": 0.00020,
            "max_tier1": 0.00045,
        },
        "tiers": [
            {"tier": 1, "volume_usd": 0, "maker": 0.0, "taker": 0.00045},
            {"tier": 2, "volume_usd": 5_000_000, "maker": 0.0, "taker": 0.00038},
            {"tier": 3, "volume_usd": 10_000_000, "maker": 0.0, "taker": 0.00036},
            {"tier": 4, "volume_usd": 25_000_000, "maker": 0.0, "taker": 0.00034},
            {"tier": 5, "volume_usd": 50_000_000, "maker": 0.0, "taker": 0.00032},
            {"tier": 6, "volume_usd": 250_000_000, "maker": 0.0, "taker": 0.00025},
            {"tier": 7, "volume_usd": 500_000_000, "maker": 0.0, "taker": 0.00020},
        ],
    }


def funding_contribution_bps(
    holding_seconds: float, *, funding_rate_per_8h: float = 0.0001
) -> float:
    """Approximate funding cost in bps of notional over a short hold."""

    return abs(funding_rate_per_8h) * holding_seconds / 28_800.0 * 10_000.0


def round_trip_friction_bps(
    *,
    taker_fee_rate: float,
    slippage_bps_per_side: float,
    latency_bps_per_side: float,
    spread_bps_round_trip: float,
    funding_bps: float,
) -> float:
    """All-in round-trip friction in bps (fees charged on both entry and exit)."""

    fee_bps = 2.0 * taker_fee_rate * 10_000.0
    return (
        fee_bps
        + 2.0 * slippage_bps_per_side
        + 2.0 * latency_bps_per_side
        + spread_bps_round_trip
        + funding_bps
    )


def classify_cost_components(
    *,
    median_spread_bps: float | None,
    top_of_book_fit_for_notional: bool,
) -> dict[str, dict[str, Any]]:
    fees = hibachi_public_fee_schedule()
    return {
        "fee": {
            "class": "VERIFIED_CURRENT",
            "value_taker_tier1": fees["tier1_taker_fee_rate"],
            "value_maker": fees["maker_fee_rate"],
            "range_taker": fees["taker_fee_rate_range"],
            "source": fees["source_url"],
            "retrieved_at_utc": fees["retrieved_at_utc"],
            "note": fees["note"],
        },
        "spread": {
            "class": "OBSERVED_FROM_DATA",
            "median_spread_bps": median_spread_bps,
            "note": (
                "Round-trip crossing pays approximately the full observed spread "
                "(enter at ask, exit at bid or reverse)."
            ),
        },
        "slippage": {
            "class": (
                "OBSERVED_FROM_DATA" if top_of_book_fit_for_notional else "MODELED"
            ),
            "note": (
                "For notionals that fit visible top-of-book size, extra depth "
                "slippage beyond the spread is 0. Larger notionals exceed visible "
                "top and remain modeled/stress until full-depth walk is wired."
            ),
        },
        "delay_latency": {
            "class": "MODELED",
            "supported_delays_seconds": [0, 1, 2],
            "note": (
                "market_state_1s cannot support trustworthy 0.25/0.5s claims; "
                "0s is a theoretical upper bound only."
            ),
        },
        "funding": {
            "class": "PLACEHOLDER",
            "note": (
                "Rate placeholder until verified funding series is used; "
                "contribution at 5–60s is negligible vs fees/spread."
            ),
            "example_15s_bps": funding_contribution_bps(15.0),
        },
    }


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    weight = pos - lo
    return sorted_vals[lo] * (1.0 - weight) + sorted_vals[hi] * weight


def top_of_book_capacity_usd(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ask_caps: list[float] = []
    bid_caps: list[float] = []
    for row in rows:
        ask = row.get("best_ask")
        bid = row.get("best_bid")
        ask_size = row.get("ask_size")
        bid_size = row.get("bid_size")
        if ask is not None and ask_size is not None:
            ask_caps.append(float(ask) * float(ask_size))
        if bid is not None and bid_size is not None:
            bid_caps.append(float(bid) * float(bid_size))
    ask_sorted = sorted(ask_caps)
    bid_sorted = sorted(bid_caps)
    return {
        "ask_top_usd": {
            "n": len(ask_sorted),
            "p50": _percentile(ask_sorted, 0.50),
            "p05": _percentile(ask_sorted, 0.05),
            "p01": _percentile(ask_sorted, 0.01),
        },
        "bid_top_usd": {
            "n": len(bid_sorted),
            "p50": _percentile(bid_sorted, 0.50),
            "p05": _percentile(bid_sorted, 0.05),
            "p01": _percentile(bid_sorted, 0.01),
        },
    }


def depth_slippage_estimate(
    *,
    notional_usd: float,
    top_size: float | None,
    best_price: float | None,
) -> dict[str, Any]:
    """Top-of-book capacity check; no invented deeper book walk."""

    if top_size is None or best_price is None or best_price <= 0 or top_size < 0:
        return {
            "notional_usd": notional_usd,
            "fits_top_of_book": None,
            "extra_slippage_bps": None,
            "status": "missing_top_size",
        }
    capacity = float(top_size) * float(best_price)
    fits = notional_usd <= capacity
    return {
        "notional_usd": notional_usd,
        "top_capacity_usd": capacity,
        "fits_top_of_book": fits,
        "extra_slippage_bps": 0.0 if fits else None,
        "status": "fits_top" if fits else "exceeds_visible_top",
        "classification": "OBSERVED_FROM_DATA" if fits else "MODELED",
    }


def break_even_matrix(
    gross_edges: list[dict[str, Any]],
    *,
    current_plausible_friction_bps: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in gross_edges:
        gross = item.get("gross_bps")
        if gross is None:
            status = "UNKNOWN"
            max_tol = None
        else:
            max_tol = abs(float(gross))
            status = (
                "TRADEABLE"
                if float(gross) > current_plausible_friction_bps
                else "NOT_TRADEABLE"
            )
        out.append(
            {
                **item,
                "max_tolerable_friction_bps": max_tol,
                "current_plausible_friction_bps": current_plausible_friction_bps,
                "economic_status": status,
            }
        )
    return out


def build_cost_evidence_report(
    market_state_path: Path,
    *,
    notionals: tuple[float, ...] = (100.0, 500.0, 1000.0, 5000.0),
    latency_bps_per_side: float = 1.0,
    holding_seconds: float = 15.0,
) -> dict[str, Any]:
    rows = pq.read_table(market_state_path).to_pylist()
    spreads = sorted(
        float(row["spread_bps"])
        for row in rows
        if row.get("spread_bps") is not None and math.isfinite(float(row["spread_bps"]))
    )
    median_spread = _percentile(spreads, 0.50)
    capacity = top_of_book_capacity_usd(rows)
    ask_p50 = (capacity.get("ask_top_usd") or {}).get("p50")
    # Representative top size from median capacity / median mid if available.
    mids = [
        float(row["mid"])
        for row in rows
        if row.get("mid") is not None and float(row["mid"]) > 0
    ]
    mid_p50 = _percentile(sorted(mids), 0.50) or 1.0
    top_size_proxy = (ask_p50 / mid_p50) if ask_p50 else None

    notional_checks = [
        depth_slippage_estimate(
            notional_usd=notional,
            top_size=top_size_proxy,
            best_price=mid_p50,
        )
        for notional in notionals
    ]
    small_fit = all(
        item.get("fits_top_of_book") for item in notional_checks if item["notional_usd"] <= 1000
    )
    fees = hibachi_public_fee_schedule()
    funding_bps = funding_contribution_bps(holding_seconds)
    slip_side = 0.0 if small_fit else 2.0
    friction = round_trip_friction_bps(
        taker_fee_rate=float(fees["tier1_taker_fee_rate"]),
        slippage_bps_per_side=slip_side,
        latency_bps_per_side=latency_bps_per_side,
        spread_bps_round_trip=float(median_spread or 0.0),
        funding_bps=funding_bps,
    )
    friction_fee_only_plus_spread = round_trip_friction_bps(
        taker_fee_rate=float(fees["tier1_taker_fee_rate"]),
        slippage_bps_per_side=0.0,
        latency_bps_per_side=0.0,
        spread_bps_round_trip=float(median_spread or 0.0),
        funding_bps=funding_bps,
    )
    return {
        "created_at_utc": datetime.now(UTC).isoformat(),
        "fees": fees,
        "components": classify_cost_components(
            median_spread_bps=median_spread,
            top_of_book_fit_for_notional=bool(small_fit),
        ),
        "spread_distribution_bps": {
            "n": len(spreads),
            "p50": median_spread,
            "p90": _percentile(spreads, 0.90),
            "p99": _percentile(spreads, 0.99),
        },
        "top_of_book_capacity_usd": capacity,
        "notional_depth_checks": notional_checks,
        "plausible_friction_bps": {
            "tier1_taker_plus_median_spread_plus_modeled_latency": friction,
            "tier1_taker_plus_median_spread_only": friction_fee_only_plus_spread,
            "assumptions": {
                "latency_bps_per_side": latency_bps_per_side,
                "slippage_bps_per_side": slip_side,
                "holding_seconds_for_funding": holding_seconds,
                "funding_bps": funding_bps,
            },
        },
    }
