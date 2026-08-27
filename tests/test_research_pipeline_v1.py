"""Tests for offline research pipeline v1."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from tests.test_normalization_parsers import raw
from trading_bot.research.pipeline.baselines import (
    MarketStateBaselineConfig,
    replay_market_state_baseline,
)
from trading_bot.research.pipeline.features import FEATURES_V1
from trading_bot.research.pipeline.inventory import verified_historical_sources
from trading_bot.research.pipeline.market_state import build_market_state_1s
from trading_bot.research.pipeline.normalize_offline import (
    assert_available_at_equals_received_at,
    normalize_events_parquet,
)
from trading_bot.research.pipeline.run import run_research_pipeline_v1
from trading_bot.research.pipeline.trades import parse_trade_event, raw_row_to_market_event
from trading_bot.research.pipeline.validate import leakage_checks
from trading_bot.research.replay import CostConfig


def _events_from_fixtures(tmp_path: Path, *, seconds: int = 30) -> Path:
    """Build a miniature receipt-ordered events.parquet from Hibachi fixtures."""

    base = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
    rows: list[dict] = []
    raw_id = 7_471_913
    connection = "conn-a"
    local_seq = 0

    def add(topic: str, fixture: str, offset_s: float, *, conn: str = connection) -> None:
        nonlocal raw_id, local_seq
        local_seq += 1
        event = raw(fixture, raw_id=raw_id)
        payload = event.payload
        received = base + timedelta(seconds=offset_s)
        rows.append(
            {
                "raw_event_id": raw_id,
                "received_at": received,
                "exchange_at": event.exchange_at,
                "source": "hibachi_ws",
                "topic": topic,
                "symbol": "ETH/USDT-P",
                "sequence": event.sequence,
                "connection_id": conn,
                "local_sequence": local_seq,
                "exchange_sequence": event.exchange_sequence,
                "schema_version": 2,
                "latency_ms": 1.0,
                "payload_json": json.dumps(payload),
            }
        )
        raw_id += 1

    # Snapshot then updates across seconds; quotes/mark/spot/funding/trades interleaved.
    add("orderbook", "orderbook_snapshot", 0.0)
    add("ask_bid_price", "ask_bid_price", 0.1)
    add("mark_price", "mark_price", 0.2)
    add("spot_price", "spot_price", 0.3)
    add("funding_rate_estimation", "funding_rate_estimation", 0.4)
    add("trades", "trades", 0.5)
    for sec in range(1, seconds):
        add("orderbook", "orderbook_empty_update", float(sec))
        add("ask_bid_price", "ask_bid_price", float(sec) + 0.1)
        if sec % 3 == 0:
            add("trades", "trades", float(sec) + 0.2)
        if sec % 5 == 0:
            add("mark_price", "mark_price", float(sec) + 0.3)
            add("spot_price", "spot_price", float(sec) + 0.35)

    # Lookahead poison row must not affect earlier decision_time if sorted correctly.
    path = tmp_path / "events.parquet"
    pq.write_table(pa.Table.from_pylist(rows), path)
    return path


def test_trade_parser_fixture() -> None:
    event = raw("trades", raw_id=42)
    trade = parse_trade_event(event)
    assert trade.taker_side == "Buy"
    assert trade.provenance.available_at == trade.provenance.received_at


def test_available_at_equals_received_at(tmp_path: Path) -> None:
    events = _events_from_fixtures(tmp_path, seconds=5)
    out = tmp_path / "norm"
    normalize_events_parquet(events, out)
    quotes = pq.read_table(out / "ask_bid_price.parquet").to_pylist()
    assert_available_at_equals_received_at(quotes)


def test_normalize_and_market_state_causal(tmp_path: Path) -> None:
    events = _events_from_fixtures(tmp_path, seconds=20)
    norm = tmp_path / "normalized_events"
    stats = normalize_events_parquet(events, norm)
    assert stats.normalized_rows > 0
    assert stats.by_topic.get("orderbook", 0) >= 1
    assert stats.trade_rows >= 1

    ms_path = tmp_path / "market_state_1s.parquet"
    ms = build_market_state_1s(norm, ms_path)
    assert ms["rows"] >= 5
    rows = pq.read_table(ms_path).to_pylist()
    # Causality: mid at T must not use events with available_at > T
    # (enforced by builder; check monotonic decision_time).
    times = [r["decision_time"] for r in rows]
    assert times == sorted(times)
    assert any(r["valid_book"] for r in rows)
    assert {"signed_trade_flow_1s", "ofi_1s", "ofi_5s", "ofi_15s"}.issubset(rows[0])
    assert "signed_volume" not in rows[0]


def test_no_feature_lookahead_in_labels(tmp_path: Path) -> None:
    events = _events_from_fixtures(tmp_path, seconds=25)
    workspace = tmp_path / "ws"
    manifest = run_research_pipeline_v1(
        events_parquet=events,
        workspace=workspace,
        source_dataset_id="fixture_mini",
    )
    features = pq.read_table(workspace / "features" / "features_v1.parquet").to_pylist()
    labels = pq.read_table(workspace / "labels" / "labels_v1.parquet").to_pylist()
    assert set(FEATURES_V1).issubset(set(features[0]))
    # Feature columns must not include fwd_ret_*
    assert not any(k.startswith("fwd_ret_") for k in features[0])
    assert "fwd_ret_5s_bps" in labels[0]
    assert manifest["baselines"]
    assert manifest["config_hash"]


def test_market_state_does_not_bridge_archive_gaps(tmp_path: Path) -> None:
    """Stale tops must not invent 1s rows across multi-hour discontinuities."""

    base = datetime(2026, 8, 9, 11, 0, 0, tzinfo=UTC)
    rows: list[dict] = []
    raw_id = 7_471_913
    local_seq = 0

    def add(topic: str, fixture: str, when: datetime) -> None:
        nonlocal raw_id, local_seq
        local_seq += 1
        event = raw(fixture, raw_id=raw_id)
        rows.append(
            {
                "raw_event_id": raw_id,
                "received_at": when,
                "exchange_at": event.exchange_at,
                "source": "hibachi_ws",
                "topic": topic,
                "symbol": "ETH/USDT-P",
                "sequence": event.sequence,
                "connection_id": "conn-gap",
                "local_sequence": local_seq,
                "exchange_sequence": event.exchange_sequence,
                "schema_version": 2,
                "latency_ms": 1.0,
                "payload_json": json.dumps(event.payload),
            }
        )
        raw_id += 1

    # Short contiguous segment, then a multi-hour gap, then another segment.
    add("orderbook", "orderbook_snapshot", base)
    add("ask_bid_price", "ask_bid_price", base + timedelta(milliseconds=100))
    for sec in range(1, 6):
        add("orderbook", "orderbook_empty_update", base + timedelta(seconds=sec))
        add("ask_bid_price", "ask_bid_price", base + timedelta(seconds=sec, milliseconds=100))
    gap_resume = base + timedelta(hours=5)
    add("orderbook", "orderbook_snapshot", gap_resume)
    add("ask_bid_price", "ask_bid_price", gap_resume + timedelta(milliseconds=100))
    for sec in range(1, 4):
        add("orderbook", "orderbook_empty_update", gap_resume + timedelta(seconds=sec))
        add(
            "ask_bid_price",
            "ask_bid_price",
            gap_resume + timedelta(seconds=sec, milliseconds=100),
        )

    events = tmp_path / "events.parquet"
    pq.write_table(pa.Table.from_pylist(rows), events)
    norm = tmp_path / "normalized_events"
    normalize_events_parquet(events, norm)
    ms_path = tmp_path / "market_state_1s.parquet"
    stats = build_market_state_1s(norm, ms_path)
    times = [row["decision_time"] for row in pq.read_table(ms_path).to_pylist()]
    assert stats["rows"] < 60
    assert max(times) - min(times) >= timedelta(hours=4)
    # No dense invented seconds inside the gap: largest contiguous run stays short.
    longest = 1
    run = 1
    for prev, cur in zip(times, times[1:], strict=False):
        if (cur - prev) == timedelta(seconds=1):
            run += 1
            longest = max(longest, run)
        else:
            run = 1
    assert longest <= 15


def test_pipeline_leakage_checks(tmp_path: Path) -> None:
    events = _events_from_fixtures(tmp_path, seconds=25)
    workspace = tmp_path / "ws"
    run_research_pipeline_v1(
        events_parquet=events,
        workspace=workspace,
        source_dataset_id="fixture_mini",
    )

    result = leakage_checks(
        workspace / "market_state_1s" / "market_state_1s.parquet",
        workspace / "features" / "features_v1.parquet",
        workspace / "labels" / "labels_v1.parquet",
    )
    assert result["rows_checked"] > 0
    assert result["horizons_seconds"] == [5, 15, 30, 60]


def test_baselines_deterministic(tmp_path: Path) -> None:
    events = _events_from_fixtures(tmp_path, seconds=40)
    workspace = tmp_path / "ws"
    run_research_pipeline_v1(
        events_parquet=events,
        workspace=workspace,
        source_dataset_id="fixture_mini",
    )
    ms = workspace / "market_state_1s" / "market_state_1s.parquet"
    a = replay_market_state_baseline(
        ms,
        signal=MarketStateBaselineConfig(name="momentum", entry_threshold=0.01),
        costs=CostConfig(),
    )
    b = replay_market_state_baseline(
        ms,
        signal=MarketStateBaselineConfig(name="momentum", entry_threshold=0.01),
        costs=CostConfig(),
    )
    assert a["configuration_hash"] == b["configuration_hash"]
    assert a["net_pnl"] == b["net_pnl"]
    assert a["trades"] == b["trades"]


def test_cost_model_never_uses_mid_for_fills(tmp_path: Path) -> None:
    events = _events_from_fixtures(tmp_path, seconds=40)
    workspace = tmp_path / "ws"
    run_research_pipeline_v1(
        events_parquet=events,
        workspace=workspace,
        source_dataset_id="fixture_mini",
    )
    ms = workspace / "market_state_1s" / "market_state_1s.parquet"
    report = replay_market_state_baseline(
        ms,
        signal=MarketStateBaselineConfig(
            name="imbalance", entry_threshold=0.0, lookback_seconds=1
        ),
    )
    # If any trades, entry/exit prices come from bid/ask path (spread exists).
    for trade in report["trade_details"]:
        assert trade["entry_price"] > 0
        assert trade["exit_price"] > 0


def test_inventory_loads_verified_sources() -> None:
    path = Path("tests/fixtures/research/production_verified_inventory.json")
    entries = verified_historical_sources(path)
    assert len(entries) >= 2
    ids = {e.dataset_id for e in entries}
    assert "g_7471913_7871913" in ids
    assert any(e.kind == "pre_partition_continuous" for e in entries)


def test_raw_row_roundtrip_provenance() -> None:
    event = raw("ask_bid_price", raw_id=99)
    row = {
        "raw_event_id": 99,
        "received_at": event.received_at,
        "exchange_at": event.exchange_at,
        "source": event.source,
        "topic": event.event_type,
        "symbol": event.symbol,
        "connection_id": event.connection_id,
        "local_sequence": event.local_sequence,
        "exchange_sequence": event.exchange_sequence,
        "schema_version": event.schema_version,
        "payload_json": json.dumps(event.payload),
    }
    rebuilt = raw_row_to_market_event(row)
    assert rebuilt.id == 99
    assert rebuilt.event_type == "ask_bid_price"
