"""First-passage opportunity protocol v1 tests (synthetic paths only)."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading_bot.archive.store import LocalArchiveStore
from trading_bot.archive.window import COMPLETED_MARKER_NAME
from trading_bot.research.collection_gaps import CollectionGap
from trading_bot.research.pipeline.first_passage_corpus import (
    freeze_discovery_oos,
    freeze_series_oos,
    list_verified_eth_completed,
    parse_dataset_id_window,
)
from trading_bot.research.pipeline.first_passage_opportunity import (
    analyze_first_passage,
    executable_prices_ok,
    filter_known_gap_rows,
    non_overlap_offsets,
    non_overlap_starts,
    split_contiguous_1s_segments,
)
from trading_bot.research.pipeline.opportunity_base_rate import (
    absolute_executable_move_bps,
    opportunity_base_rate_report,
)


def _series(
    mids: list[float],
    *,
    spread: float = 0.0002,
    start: datetime | None = None,
) -> tuple[list[int], list[float], list[float], list[float]]:
    base = start or datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    epoch: list[int] = []
    bid: list[float] = []
    ask: list[float] = []
    mid_out: list[float] = []
    half = spread / 2.0
    for i, mid in enumerate(mids):
        ts = base + timedelta(seconds=i)
        epoch.append(int(ts.timestamp()))
        bid.append(mid - half)
        ask.append(mid + half)
        mid_out.append(mid)
    return epoch, bid, ask, mid_out


def test_old_opportunity_is_endpoint_not_excursion() -> None:
    """Endpoint mid[t+h]/mid[t] misses an intra-window spike that returns to 0."""

    mids = [100.0] * 11
    mids[3] = 100.2  # +20 bps at lag 3, back to 100 at endpoint
    base = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    rows = []
    for i, mid in enumerate(mids):
        rows.append(
            {
                "decision_time": base + timedelta(seconds=i),
                "mid": mid,
                "best_bid": mid,
                "best_ask": mid,
            }
        )
    report = opportunity_base_rate_report(rows, horizons=(10,))
    ov = report["overlapping_1s"]["10s"]
    assert ov["n"] >= 1
    # Endpoint move is 0, so the +20 bps spike is invisible to the old screen.
    assert absolute_executable_move_bps(100.0, 100.0) == 0.0
    assert (ov["p50_bps"] or 0.0) < 1.0


def test_first_passage_registers_intra_window_spike_returning_to_zero() -> None:
    mids = [100.0] * 11
    mids[3] = 100.2  # +20 bps inside the 10s window, endpoint unchanged
    epoch, bid, ask, mid = _series(mids)
    report = analyze_first_passage(
        epoch, bid, ask, mid, horizons=(10,), thresholds=(20.0,)
    )
    cell = report["mid"]["rolling_1s"]["10s"]["thresholds"]["20"]
    assert cell["n_valid_starts"] >= 1
    assert cell["long_hit_count"] >= 1
    assert cell["long_hit_fraction"] and cell["long_hit_fraction"] > 0.0
    # First valid start t=0 should hit at lag 3.
    assert cell["first_hit_time_s"]["long"]["p50"] == 3.0


def test_threshold_never_reached() -> None:
    epoch, bid, ask, mid = _series([100.0] * 20)
    report = analyze_first_passage(
        epoch, bid, ask, mid, horizons=(10,), thresholds=(20.0,)
    )
    cell = report["mid"]["rolling_1s"]["10s"]["thresholds"]["20"]
    assert cell["n_valid_starts"] > 0
    assert cell["long_hit_count"] == 0
    assert cell["short_hit_count"] == 0
    assert cell["either_side_hit_count"] == 0


def test_long_executable_tp_differs_from_mid_due_to_spread() -> None:
    # Mid +20 bps is not enough to pay a 20 bps half-spread on the long TOB path.
    mids = [100.0] * 11
    mids[4] = 100.2
    epoch, bid, ask, mid = _series(mids, spread=0.4)  # 20 bps each side of mid at 100
    assert executable_prices_ok(bid[0], ask[0], mid[0])
    report = analyze_first_passage(
        epoch, bid, ask, mid, horizons=(10,), thresholds=(20.0,)
    )
    mid_cell = report["mid"]["rolling_1s"]["10s"]["thresholds"]["20"]
    ex_cell = report["executable_tob"]["rolling_1s"]["10s"]["thresholds"]["20"]
    assert mid_cell["long_hit_count"] >= 1
    assert ex_cell["long_hit_count"] == 0


def test_mae_before_tp_is_max_adverse_until_hit() -> None:
    # Mid: 100 → 99.85 (-15 bps) → 100.25 (+25 bps). Long TP 20 hits at lag 2.
    mids = [100.0, 99.85, 100.25] + [100.25] * 8
    epoch, bid, ask, mid = _series(mids)
    report = analyze_first_passage(
        epoch, bid, ask, mid, horizons=(10,), thresholds=(20.0,)
    )
    cell = report["mid"]["rolling_1s"]["10s"]["thresholds"]["20"]
    assert cell["long_hit_count"] >= 1
    mae = cell["mae_before_first_tp_bps"]["long"]["p50"]
    assert mae is not None
    assert abs(mae - 15.0) < 0.05


def test_gap_inside_window_makes_start_invalid() -> None:
    # 6 contiguous seconds, a hole, then 6 more. Horizon 10 cannot span the hole.
    first = [100.0 + 0.01 * i for i in range(6)]
    second = [100.2 + 0.01 * i for i in range(6)]
    epoch_a, bid_a, ask_a, mid_a = _series(
        first, start=datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    )
    epoch_b, bid_b, ask_b, mid_b = _series(
        second, start=datetime(2026, 8, 6, 12, 0, 20, tzinfo=UTC)
    )
    epoch = epoch_a + epoch_b
    bid = bid_a + bid_b
    ask = ask_a + ask_b
    mid = mid_a + mid_b
    assert split_contiguous_1s_segments(epoch) == [(0, 6), (6, 12)]
    report = analyze_first_passage(
        epoch, bid, ask, mid, horizons=(10,), thresholds=(5.0,)
    )
    cell = report["mid"]["rolling_1s"]["10s"]["thresholds"]["5"]
    # Each segment has only 6 points; 10s windows need 11 samples.
    assert cell["n_valid_starts"] == 0


def test_known_collection_gap_drops_rows_and_does_not_count_as_zero_move() -> None:
    epoch, bid, ask, mid = _series([100.0] * 5)
    times = [datetime.fromtimestamp(ts, tz=UTC) for ts in epoch]
    gap = CollectionGap(
        gap_id="synthetic",
        kind="COLLECTION_OUTAGE",
        start_utc=times[2],
        end_utc=times[4],
        id_start_inclusive=None,
        id_end_inclusive=None,
        classification="TEST",
        synthesize=False,
        bridge_normalization=False,
        notes="test",
    )
    out_epoch, out_bid, out_ask, out_mid, dropped = filter_known_gap_rows(
        times, bid, ask, mid, (gap,)
    )
    assert dropped >= 2
    report = analyze_first_passage(
        out_epoch, out_bid, out_ask, out_mid, horizons=(5,), thresholds=(20.0,)
    )
    # Too few contiguous seconds for a 5s window after the hole is punched.
    assert report["mid"]["rolling_1s"]["5s"]["n_valid_starts"] == 0


def test_first_touch_order_plus_before_minus() -> None:
    # +25 bps at lag 2, then -40 bps at lag 4. For 20 bps, plus happens first.
    mids = [100.0, 100.0, 100.25, 100.25, 99.60] + [99.60] * 6
    epoch, bid, ask, mid = _series(mids)
    report = analyze_first_passage(
        epoch, bid, ask, mid, horizons=(10,), thresholds=(20.0,)
    )
    diag = report["mid"]["rolling_1s"]["10s"]["thresholds"]["20"]["first_touch_diagnostic"]
    assert diag["plus_before_minus"] >= 1
    assert diag["minus_before_plus"] == 0 or diag["plus_before_minus"] > diag[
        "minus_before_plus"
    ]


def test_first_touch_order_minus_before_plus() -> None:
    mids = [100.0, 100.0, 99.75, 99.75, 100.40] + [100.40] * 6
    epoch, bid, ask, mid = _series(mids)
    report = analyze_first_passage(
        epoch, bid, ask, mid, horizons=(10,), thresholds=(20.0,)
    )
    diag = report["mid"]["rolling_1s"]["10s"]["thresholds"]["20"]["first_touch_diagnostic"]
    assert diag["minus_before_plus"] >= 1


def test_non_overlap_offset_calculation() -> None:
    assert non_overlap_offsets(8) == (0, 2, 4, 6)
    assert non_overlap_offsets(10) == (0, 2, 5, 7)
    # 20 points (indices 0..19), H=8 requires i+8 <= 19 → i <= 11.
    assert non_overlap_starts(20, 8, 0) == [0, 8]
    assert non_overlap_starts(20, 8, 2) == [2, 10]
    assert non_overlap_starts(20, 8, 4) == [4]
    assert non_overlap_starts(20, 8, 6) == [6]
    epoch, bid, ask, mid = _series([100.0 + 0.001 * i for i in range(20)])
    report = analyze_first_passage(
        epoch, bid, ask, mid, horizons=(8,), thresholds=(5.0,)
    )
    rolling_n = report["mid"]["rolling_1s"]["8s"]["n_valid_starts"]
    off0 = report["mid"]["non_overlapping"]["8s"]["thresholds"]["5"]["per_offset"]["0"]
    assert rolling_n == 12  # indices 0..11
    assert off0["n_valid_starts"] == 2
    assert off0["n_valid_starts"] < rolling_n


def test_list_verified_eth_completed_uses_completed_marker() -> None:
    root = Path(tempfile.mkdtemp(prefix="first-passage-store-"))
    store = LocalArchiveStore(root)
    dataset_id = "eth-usdt-p_20260806T000000000000Z_20260806T010000000000Z_v2"
    payload = {
        "status": COMPLETED_MARKER_NAME,
        "dataset_id": dataset_id,
        "attempt_id": "attempt-1",
        "quarantined": False,
        "research_quality_status": "pass",
        "admission_eligible": True,
    }
    store.publish_bytes(
        f"archives/{dataset_id}/COMPLETED",
        (json.dumps(payload) + "\n").encode("utf-8"),
    )
    store.publish_bytes("archives/quarantine/registry.jsonl", b"{}\n")
    other = "btc-usdt-p_20260806T000000000000Z_20260806T010000000000Z_v2"
    store.publish_bytes(
        f"archives/{other}/COMPLETED",
        json.dumps({**payload, "dataset_id": other}).encode(),
    )
    rows = list_verified_eth_completed(store)
    assert len(rows) == 1
    assert rows[0]["dataset_id"] == dataset_id
    start, end = parse_dataset_id_window(dataset_id)
    assert start == datetime(2026, 8, 6, tzinfo=UTC)
    assert end == datetime(2026, 8, 6, 1, tzinfo=UTC)


def test_freeze_discovery_oos_uses_last_utc_dates_without_price_stats() -> None:
    windows = []
    for day in (6, 7, 8, 9):
        start = datetime(2026, 8, day, tzinfo=UTC)
        end = start + timedelta(hours=1)
        windows.append(
            {
                "dataset_id": f"d{day}",
                "start_utc": start.isoformat(),
                "end_utc": end.isoformat(),
                "quarantined": False,
            }
        )
    frozen = freeze_discovery_oos(windows, oos_utc_days=3)
    assert frozen["oos_utc_dates"] == ["2026-08-07", "2026-08-08", "2026-08-09"]
    assert frozen["discovery_window_count"] == 1
    assert frozen["oos_window_count"] == 3
    assert frozen["price_movement_inspected"] is False


def test_freeze_series_oos_does_not_shrink_oos_when_discovery_is_thin() -> None:
    times = [datetime(2026, 8, d, 12, 0, tzinfo=UTC) for d in (6, 7, 8)]
    frozen = freeze_series_oos(times, oos_utc_days=3)
    assert frozen["discovery_row_count"] == 0
    assert "DISCOVERY_EMPTY_AFTER_OOS_RESERVE" in frozen["lead_alerts"]
    assert frozen["oos_row_count"] == 3
