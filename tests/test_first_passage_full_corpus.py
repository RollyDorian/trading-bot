"""Full-corpus expansion: freeze, exact counts, episodes, day stability."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from trading_bot.archive.store import LocalArchiveStore
from trading_bot.archive.window import COMPLETED_MARKER_NAME
from trading_bot.research.pipeline.first_passage_corpus import (
    V1_UNTOUCHED_OOS_UTC_DATES,
    concat_parquet_files,
    download_completed_events_parquet,
    freeze_full_corpus_expansion,
    inventory_utc_day_coverage,
)
from trading_bot.research.pipeline.first_passage_opportunity import (
    analyze_first_passage,
    cluster_adjacent_1s_starts,
    day_block_stability,
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


def _win(
    day: int,
    *,
    month: int = 8,
    hours: float = 24.0,
    quarantined: bool = False,
    dataset_suffix: str = "",
) -> dict[str, object]:
    start = datetime(2026, month, day, tzinfo=UTC)
    end = start + timedelta(hours=hours)
    stamp = start.strftime("%Y%m%dT%H%M%S")
    return {
        "dataset_id": f"eth-usdt-p_{stamp}000000Z_{end:%Y%m%dT%H%M%S}000000Z_v2{dataset_suffix}",
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
        "quarantined": quarantined,
        "research_quality_status": "pass",
    }


def test_cluster_adjacent_1s_starts_merges_only_exact_one_second_gaps() -> None:
    merged = cluster_adjacent_1s_starts([10, 12, 11, 20])
    assert len(merged) == 2
    assert merged[0]["first_start_epoch_s"] == 10
    assert merged[0]["last_start_epoch_s"] == 12
    assert merged[0]["n_rolling_starts"] == 3
    assert merged[1]["first_start_epoch_s"] == 20
    assert merged[1]["n_rolling_starts"] == 1
    # Two starts 2 seconds apart are two episodes (no fitted cooldown).
    split = cluster_adjacent_1s_starts([100, 102])
    assert len(split) == 2


def test_movement_episode_counts_one_run_of_adjacent_rolling_hits() -> None:
    # +3 bps/s so a 10s window reaches +20 bps; consecutive starts all hit long.
    mids = [100.0 + 0.03 * i for i in range(25)]
    epoch, bid, ask, mid = _series(mids)
    report = analyze_first_passage(
        epoch, bid, ask, mid, horizons=(10,), thresholds=(20.0,)
    )
    cell = report["mid"]["rolling_1s"]["10s"]["thresholds"]["20"]
    assert cell["long_hit_count"] >= 2
    episodes = report["movement_episode_v1"]["mid"]["10s"]["thresholds"]["20"]["long"]
    assert episodes["episode_count"] == 1
    assert episodes["n_rolling_hit_starts"] == cell["long_hit_count"]


def test_nonoverlap_per_offset_exposes_exact_hit_and_n_counts() -> None:
    epoch, bid, ask, mid = _series([100.0 + 0.001 * i for i in range(20)])
    report = analyze_first_passage(
        epoch, bid, ask, mid, horizons=(8,), thresholds=(5.0,)
    )
    cell = report["mid"]["non_overlapping"]["8s"]["thresholds"]["5"]
    off0 = cell["per_offset"]["0"]
    assert off0["n_valid_starts"] == 2
    assert "either_side_hit_count" in off0
    assert "long_hit_count" in off0
    pooled = cell["pooled_descriptive_dependent"]
    assert pooled["n_valid_starts_sum"] == sum(
        row["n_valid_starts"] for row in cell["per_offset"].values()
    )
    assert "not independent" in pooled["note"].lower() or "dependent" in pooled["note"].lower()


def test_freeze_keeps_v1_oos_and_does_not_auto_oos_later_partial_days() -> None:
    windows = [
        _win(6),
        _win(7),
        _win(9),
        _win(10),
        _win(11, hours=12.0),
        _win(12, hours=9.0),
        _win(19, hours=6.0),
        _win(20, hours=4.0),
    ]
    frozen = freeze_full_corpus_expansion(windows)
    assert frozen["v1_untouched_oos_utc_dates"] == list(V1_UNTOUCHED_OOS_UTC_DATES)
    assert frozen["new_holdout_applied"] is False
    assert frozen["new_holdout_utc_dates"] == []
    assert "NEW_FINAL_HOLDOUT_UNAVAILABLE_NO_TWO_FULL_LATER_UTC_DAYS" in frozen["lead_alerts"]
    discovery = set(frozen["discovery_utc_dates"])
    assert "2026-08-06" in discovery
    assert "2026-08-11" in discovery
    assert "2026-08-20" in discovery
    assert "2026-08-07" not in discovery
    assert "2026-08-09" not in discovery
    assert "2026-08-10" not in discovery
    oos_ids = {row["dataset_id"] for row in frozen["untouched_oos_windows"]}
    assert any("20260807" in item for item in oos_ids)
    assert frozen["price_movement_inspected"] is False


def test_freeze_reserves_last_two_full_later_days_as_holdout() -> None:
    windows = [
        _win(6),
        _win(7),
        _win(9),
        _win(10),
        _win(11),
        _win(12),
        _win(13),
        _win(14),
    ]
    frozen = freeze_full_corpus_expansion(windows)
    assert frozen["new_holdout_applied"] is True
    assert frozen["new_holdout_utc_dates"] == ["2026-08-13", "2026-08-14"]
    discovery = set(frozen["discovery_utc_dates"])
    assert "2026-08-11" in discovery
    assert "2026-08-12" in discovery
    assert "2026-08-13" not in discovery
    assert "2026-08-14" not in discovery
    assert "2026-08-07" not in discovery
    assert frozen["new_holdout_window_count"] == 2


def test_inventory_full_day_requires_23h_eligible_not_quarantined() -> None:
    short = inventory_utc_day_coverage([_win(1, hours=22.0)])
    assert short["days"][0]["full_utc_day"] is False
    full = inventory_utc_day_coverage([_win(2, hours=24.0)])
    assert full["days"][0]["full_utc_day"] is True
    mixed = inventory_utc_day_coverage(
        [_win(3, hours=24.0), _win(3, hours=1.0, quarantined=True, dataset_suffix="_q")]
    )
    assert mixed["days"][0]["full_utc_day"] is True
    assert mixed["days"][0]["eligible_hours"] >= 23.0


def test_download_completed_events_verifies_logical_sha256(tmp_path: Path) -> None:
    store = LocalArchiveStore(tmp_path / "store")
    dataset_id = "eth-usdt-p_20260806T000000000000Z_20260806T010000000000Z_v2"
    payload = b"raw-events-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    store.publish_bytes(
        f"archives/{dataset_id}/COMPLETED",
        json.dumps(
            {
                "status": COMPLETED_MARKER_NAME,
                "attempt_id": "attempt-1",
                "quarantined": False,
                "logical_artifacts": {"events.parquet": digest},
            }
        ).encode("utf-8"),
    )
    store.publish_bytes(
        f"archives/{dataset_id}/attempts/attempt-1/events.parquet",
        payload,
    )
    dest = tmp_path / "events.parquet"
    result = download_completed_events_parquet(store, dataset_id, dest)
    assert result["status"] == "downloaded"
    assert dest.read_bytes() == payload
    again = download_completed_events_parquet(store, dataset_id, dest)
    assert again["status"] == "cache_hit"


def test_concat_parquet_files_preserves_row_count(tmp_path: Path) -> None:
    first = tmp_path / "a.parquet"
    second = tmp_path / "b.parquet"
    pq.write_table(pa.table({"x": [1, 2]}), first)
    pq.write_table(pa.table({"x": [3]}), second)
    out = tmp_path / "c.parquet"
    concat_parquet_files([first, second], out)
    assert pq.read_table(out).column("x").to_pylist() == [1, 2, 3]


def test_day_block_stability_reports_min_median_max_across_days() -> None:
    quiet = [100.0] * 700
    move = [100.0 + 0.05 * min(i, 20) for i in range(700)]  # +50 bps then flat
    epoch_a, bid_a, ask_a, mid_a = _series(
        quiet, start=datetime(2026, 8, 5, 0, 0, tzinfo=UTC)
    )
    epoch_b, bid_b, ask_b, mid_b = _series(
        move, start=datetime(2026, 8, 6, 0, 0, tzinfo=UTC)
    )
    times = [datetime.fromtimestamp(ts, tz=UTC) for ts in epoch_a + epoch_b]
    report = day_block_stability(
        times,
        bid_a + bid_b,
        ask_a + ask_b,
        mid_a + mid_b,
        horizons=(60,),
        thresholds=(20.0,),
    )
    dist = report["distribution_across_days"]["60s_20bps"]
    assert dist["n_days"] >= 1
    assert dist["min"] is not None and dist["max"] is not None
    assert dist["min"] <= dist["median"] <= dist["max"]
    dates = {row["utc_date"] for row in report["per_utc_day"]}
    assert dates == {"2026-08-05", "2026-08-06"}
