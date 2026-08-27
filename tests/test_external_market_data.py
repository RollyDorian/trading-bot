"""Tests for isolated Binance USD-M external-ref collector."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading_bot.external_market_data.binance_parser import (
    BinanceParseError,
    parse_binance_usdm_event,
    unwrap_stream_message,
)
from trading_bot.external_market_data.contract import (
    AGG_TRADE_WS_URL,
    BOOK_TICKER_WS_URL,
    INSTRUMENT,
    MARKET_BASE,
    PUBLIC_BASE,
    VENUE,
)
from trading_bot.external_market_data.envelope import ExternalRawEnvelope
from trading_bot.external_market_data.runtime import load_config_from_env
from trading_bot.external_market_data.spool import (
    BoundedNdjsonSpool,
    ExternalCapacityStop,
    SpoolLimits,
)

FIXTURES = Path(__file__).parent / "fixtures" / "external_market_data"


def test_contract_routes_book_public_agg_market() -> None:
    assert BOOK_TICKER_WS_URL.startswith(PUBLIC_BASE + "/ws/")
    assert AGG_TRADE_WS_URL.startswith(MARKET_BASE + "/ws/")
    assert "ethusdt@bookTicker" in BOOK_TICKER_WS_URL
    assert "ethusdt@aggTrade" in AGG_TRADE_WS_URL


def test_parse_official_book_ticker_fixture() -> None:
    payload = json.loads(
        (FIXTURES / "binance_usdm_book_ticker.json").read_text(encoding="utf-8")
    )
    received = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)
    env = parse_binance_usdm_event(
        payload,
        received_at=received,
        connection_id="c1",
        local_sequence=1,
        expected_event="book_ticker",
    )
    assert env.venue == VENUE
    assert env.instrument == INSTRUMENT
    assert env.event_type == "book_ticker"
    assert env.book_update_id == 400900217
    assert env.agg_trade_id is None
    assert env.received_at == received
    assert env.exchange_at is not None


def test_parse_official_agg_trade_and_combined_wrapper() -> None:
    payload = json.loads(
        (FIXTURES / "binance_usdm_agg_trade.json").read_text(encoding="utf-8")
    )
    wrapped = {"stream": "ethusdt@aggTrade", "data": payload}
    stream, data = unwrap_stream_message(wrapped)
    assert stream == "ethusdt@aggTrade"
    env = parse_binance_usdm_event(
        wrapped,
        received_at=datetime(2026, 8, 11, 12, 0, 1, tzinfo=UTC),
        connection_id="c2",
        local_sequence=3,
        expected_event="agg_trade",
    )
    assert env.agg_trade_id == 5933014
    assert env.first_trade_id == 100
    assert env.last_trade_id == 105
    assert env.book_update_id is None
    assert data["e"] == "aggTrade"


def test_malformed_and_wrong_event_rejected() -> None:
    with pytest.raises(BinanceParseError):
        parse_binance_usdm_event(
            {"e": "markPrice", "s": "ETHUSDT"},
            received_at=datetime.now(UTC),
            connection_id="c",
            local_sequence=1,
        )
    book = json.loads(
        (FIXTURES / "binance_usdm_book_ticker.json").read_text(encoding="utf-8")
    )
    with pytest.raises(BinanceParseError):
        parse_binance_usdm_event(
            book,
            received_at=datetime.now(UTC),
            connection_id="c",
            local_sequence=1,
            expected_event="agg_trade",
        )


def test_spool_hard_cap_no_circular_delete(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    free = {"v": 10 * 1024**3}

    def free_fn(_path: Path) -> int:
        return free["v"]

    spool = BoundedNdjsonSpool(
        spool_dir,
        limits=SpoolLimits(hard_cap_bytes=500, filesystem_floor_bytes=5 * 1024**3),
        free_bytes_fn=free_fn,
    )
    spool.open()
    env = ExternalRawEnvelope(
        venue=VENUE,
        instrument=INSTRUMENT,
        event_type="book_ticker",
        received_at=datetime.now(UTC),
        connection_id="c",
        local_sequence=1,
        schema_version=1,
        payload={"e": "bookTicker"},
    )
    spool.append(env)
    with pytest.raises(ExternalCapacityStop):
        for i in range(50):
            spool.append(
                ExternalRawEnvelope(
                    venue=VENUE,
                    instrument=INSTRUMENT,
                    event_type="book_ticker",
                    received_at=datetime.now(UTC),
                    connection_id="c",
                    local_sequence=i + 2,
                    schema_version=1,
                    payload={"e": "bookTicker", "pad": "x" * 80},
                )
            )
    assert spool.path.exists()
    assert len(spool.read_lines()) >= 1
    spool.close()


def test_spool_filesystem_floor(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()

    def free_fn(_path: Path) -> int:
        return 4 * 1024**3

    spool = BoundedNdjsonSpool(
        spool_dir,
        limits=SpoolLimits(
            hard_cap_bytes=128 * 1024**2, filesystem_floor_bytes=5 * 1024**3
        ),
        free_bytes_fn=free_fn,
    )
    with pytest.raises(ExternalCapacityStop):
        spool.open()


def test_feature_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXTERNAL_REF_ENABLED", raising=False)
    cfg = load_config_from_env()
    assert cfg.enabled is False


def test_envelope_ndjson_roundtrip() -> None:
    env = ExternalRawEnvelope(
        venue=VENUE,
        instrument=INSTRUMENT,
        event_type="agg_trade",
        received_at=datetime(2026, 8, 11, tzinfo=UTC),
        connection_id="abc",
        local_sequence=9,
        schema_version=1,
        payload={"e": "aggTrade", "a": 1},
        exchange_at=datetime(2026, 8, 11, tzinfo=UTC),
        agg_trade_id=1,
    )
    loaded = json.loads(env.to_ndjson_line())
    assert loaded["local_sequence"] == 9
    assert loaded["agg_trade_id"] == 1
    assert "book_update_id" in loaded
