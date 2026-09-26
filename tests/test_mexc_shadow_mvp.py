from __future__ import annotations

import json
import threading
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_bot.research.mexc_shadow.mvp import (
    LOCKED_CORPUS_SHA256,
    ShadowMvpRunner,
    build_server,
    fixed_variants,
)
from trading_bot.research.mexc_shadow.safety import (
    extension_source_violations,
    package_import_violations,
    package_source_violations,
)
from trading_bot.research.mexc_shadow.types import Observation

BASE = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[1]


def _observation(
    seconds: float,
    mid: float,
    *,
    mark: float | None,
    index: float | None,
) -> Observation:
    stamp = BASE + timedelta(seconds=seconds)
    return Observation(
        observed_at=stamp,
        received_at=stamp,
        symbol="TAOUSDT",
        bid=mid - 0.005,
        ask=mid + 0.005,
        mark=mark,
        index=index,
    )


def _raw(stamp: datetime, sequence: int, mid: float, *, crossed: bool = False) -> dict:
    bid = mid + 0.005 if crossed else mid - 0.005
    ask = mid - 0.005 if crossed else mid + 0.005

    def field(value: float | str) -> dict:
        return {
            "raw_text": str(value),
            "value": value,
            "selector_id": "test",
            "parse_status": "ok",
            "match_count": 1,
            "age_ms": 0,
        }

    return {
        "schema": "mexc_ui_raw_snapshot",
        "schema_version": 1,
        "capture_id": "test-capture",
        "sequence": sequence,
        "received_at_local": stamp.isoformat(),
        "observed_at_local": stamp.isoformat(),
        "trigger": "interval",
        "selector_catalog_version": "1.2",
        "page_host": "www.mexc.com",
        "page_path": "/futures/TAO_USDT",
        "symbol_hint": "TAOUSDT",
        "observation_valid": not crossed,
        "invalid_reasons": ["crossed_book"] if crossed else [],
        "fields": {
            "symbol": field("TAOUSDT"),
            "bid": field(bid),
            "ask": field(ask),
            "last": field(mid),
            "mark": field(mid + 0.02),
            "index": field(mid + 0.02),
        },
    }


def test_fixed_variant_family_is_exactly_two_and_reuses_thresholds() -> None:
    variants = fixed_variants()
    assert [item.variant_id for item in variants] == [
        "exploratory_mid_mark_h1s",
        "exploratory_mid_index_h1s",
    ]
    assert {item.gap_definition for item in variants} == {"mid_vs_mark", "mid_vs_index"}
    for item in variants:
        assert item.config.signal.momentum_lookback_seconds == 1.0
        assert item.config.signal.mom_abs_min_bps == 3.0
        assert item.config.signal.gap_abs_min_bps == 1.5


def test_causal_warmup_and_event_gap_reset_prevent_backfill() -> None:
    runner = ShadowMvpRunner()
    assert runner.ingest_observation(_observation(0, 100.0, mark=100.02, index=100.02)) == []
    events = runner.ingest_observation(
        _observation(3, 100.04, mark=100.06, index=100.06), sequence=2
    )
    assert events == []
    assert runner.counters["event_gap"] == 1
    assert all(runtime.book.open_count() == 0 for runtime in runner.variants)


def test_executable_open_close_costs_drawdown_and_tail_metrics() -> None:
    runner = ShadowMvpRunner()
    runner.ingest_observation(_observation(0, 100.0, mark=100.02, index=100.02), sequence=1)
    opened = runner.ingest_observation(
        _observation(1, 100.04, mark=100.06, index=100.06), sequence=2
    )
    assert [event["event"] for event in opened] == ["OPEN", "OPEN"]
    assert all(event["entry_price"] == pytest.approx(100.045) for event in opened)
    closed = runner.ingest_observation(
        _observation(2, 100.10, mark=100.10, index=100.10), sequence=3
    )
    closes = [event for event in closed if event["event"] == "CLOSE"]
    assert len(closes) == 2
    assert all(event["exit_reason"] == "GAP_HIT" for event in closes)
    assert all(event["gross_bps"] > 0 for event in closes)
    summary = runner.summary()["variants"][0]
    assert summary["trade_count"] == 1
    assert summary["cost_scenarios"]["maker_6bps_per_side"]["mean_net_bps"] == pytest.approx(
        closes[0]["gross_bps"] - 12.0
    )
    assert len(summary["slippage_sensitivity"]) == 9
    assert summary["maximum_drawdown_gross_bps"] == 0.0
    assert summary["tail_losses_gross_bps"]["loss_count"] == 0


def test_stale_live_observation_fails_closed() -> None:
    runner = ShadowMvpRunner()
    observation = _observation(0, 100.0, mark=100.02, index=100.02)
    events = runner.ingest_observation(
        observation,
        sequence=1,
        arrival_at=observation.received_at + timedelta(milliseconds=2001),
        enforce_live_age=True,
    )
    assert events == []
    assert runner.counters["stale_live"] == 1
    assert runner.counters["observations_valid"] == 0
    assert runner.summary()["data_invalid_counters"]["stale_live"] == 1


def test_invalid_snapshot_resets_history_without_backfill() -> None:
    runner = ShadowMvpRunner()
    runner.ingest_mapping(_raw(BASE, 1, 100.0))
    runner.ingest_mapping(_raw(BASE + timedelta(milliseconds=500), 2, 100.02, crossed=True))
    events = runner.ingest_mapping(_raw(BASE + timedelta(seconds=1), 3, 100.04))
    assert events == []
    assert runner.counters["data_invalid"] == 1
    assert all(runtime.counters["feature_resets"] == 1 for runtime in runner.variants)


def test_loopback_http_channel_accepts_public_snapshot_only() -> None:
    runner = ShadowMvpRunner()
    server = build_server(runner, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        stamp = datetime.now(UTC) - timedelta(milliseconds=100)
        body = json.dumps(_raw(stamp, 1, 100.0)).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/snapshot",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
            reply = json.loads(response.read())
        assert reply == {"ok": True, "events": 0}
        assert server.server_address[0] == "127.0.0.1"
        assert runner.counters["observations_valid"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_no_order_no_credentials_boundary_and_exact_loopback_permission() -> None:
    manifest = json.loads(
        (REPO / "extensions" / "mexc_ui_capture" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["version"] == "1.3.6"
    assert manifest["host_permissions"][-1] == "http://127.0.0.1:8765/*"
    assert extension_source_violations() == []
    assert package_source_violations() == []
    assert package_import_violations() == []
    assert len(LOCKED_CORPUS_SHA256) == 64
