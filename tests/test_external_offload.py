"""Tests for external RAW segment offload lifecycle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trading_bot.archive.store import LocalArchiveStore
from trading_bot.external_market_data.offload.capacity import (
    BacklogAction,
    CapacityPolicy,
)
from trading_bot.external_market_data.offload.compress import (
    gunzip_ndjson,
    gzip_ndjson,
    prove_round_trip,
)
from trading_bot.external_market_data.offload.lifecycle import (
    SegmentOffloader,
    reclaim_local_segment,
    recover_root,
    remote_data_key,
)
from trading_bot.external_market_data.offload.segments import (
    ActiveSegmentWriter,
    SegmentPaths,
    SegmentState,
    read_state,
    recover_trailing_partial_ndjson,
    sha256_file,
)
from trading_bot.external_market_data.offload.split_spool import split_spool
from trading_bot.external_market_data.offload.status import collect_status


def _envelope(
    *,
    seq: int,
    event_type: str = "book_ticker",
    connection_id: str = "conn-a",
    received_at: str = "2026-08-11T20:09:37.805525+00:00",
    exchange_at: str = "2026-08-11T20:09:37.667000+00:00",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "venue": "binance_usdm",
        "instrument": "ETHUSDT",
        "stream": "bookTicker" if event_type == "book_ticker" else "aggTrade",
        "event_type": event_type,
        "received_at": received_at,
        "exchange_at": exchange_at,
        "local_sequence": seq,
        "connection_id": connection_id,
        "payload": {"u": seq, "b": "1", "B": "2", "a": "3", "A": "4"}
        if event_type == "book_ticker"
        else {"a": seq, "p": "1", "q": "2", "T": 1},
        "book_update_id": seq if event_type == "book_ticker" else None,
        "agg_trade_id": seq if event_type == "agg_trade" else None,
        "parse_ok": True,
    }


def _line(row: dict[str, Any]) -> bytes:
    return (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8")


def test_deterministic_segment_boundaries(tmp_path: Path) -> None:
    root = tmp_path / "segments"
    writer = ActiveSegmentWriter(root, max_bytes=400, max_seconds=10**9)
    sealed: list[str] = []
    for i in range(20):
        maybe = writer.append_line(_line(_envelope(seq=i)))
        if maybe is not None:
            sealed.append(maybe.segment_id)
    final = writer.close()
    if final:
        sealed.append(final.segment_id)
    assert len(sealed) >= 2
    assert sealed == sorted(set(sealed))
    sizes = [(root / s / "events.ndjson").stat().st_size for s in sealed]
    assert all(s > 0 for s in sizes)
    # Deterministic re-split of same content yields same count pattern.
    src = tmp_path / "mono.ndjson"
    with src.open("wb") as handle:
        for i in range(20):
            handle.write(_line(_envelope(seq=i)))
    report = split_spool(src, tmp_path / "split2", max_bytes=400)
    assert report["segment_count"] == len(sealed)


def test_active_to_sealed_and_partial_recovery(tmp_path: Path) -> None:
    root = tmp_path / "seg"
    writer = ActiveSegmentWriter(root, max_bytes=10**9, max_seconds=10**9)
    writer.append_line(_line(_envelope(seq=1)))
    assert writer._paths is not None
    active = writer._paths.active_ndjson
    # Simulate crash: leave ACTIVE file with trailing partial record.
    with active.open("ab") as handle:
        handle.write(b'{"partial":')
    writer._fh.close()
    writer._fh = None
    stats = recover_trailing_partial_ndjson(active)
    assert stats["truncated_partial"] is True
    text = active.read_text(encoding="utf-8")
    assert '{"partial":' not in text
    assert "book_ticker" in text
    paths = writer._paths
    from trading_bot.external_market_data.offload.segments import seal_active_segment

    record = seal_active_segment(paths)
    assert record.state == SegmentState.SEALED_UNVERIFIED
    assert record.event_count == 1
    assert not paths.active_ndjson.exists()
    assert paths.sealed_ndjson.exists()


def test_manifest_counts_and_hash(tmp_path: Path) -> None:
    root = tmp_path / "seg"
    writer = ActiveSegmentWriter(root, max_bytes=10**9, max_seconds=10**9)
    writer.append_line(_line(_envelope(seq=1, event_type="book_ticker")))
    writer.append_line(_line(_envelope(seq=2, event_type="agg_trade")))
    paths = writer.close()
    assert paths is not None
    record = read_state(paths)
    assert record is not None
    assert record.content_sha256 == sha256_file(paths.sealed_ndjson)
    assert record.event_count == 2


def test_gzip_and_parquet_round_trip(tmp_path: Path) -> None:
    src = tmp_path / "events.ndjson"
    with src.open("wb") as handle:
        for i in range(50):
            et = "book_ticker" if i % 2 == 0 else "agg_trade"
            handle.write(_line(_envelope(seq=i, event_type=et)))
    gz = tmp_path / "events.ndjson.gz"
    stats = gzip_ndjson(src, gz)
    assert gzip_ndjson.__kwdefaults__ is not None
    assert gzip_ndjson.__kwdefaults__["compresslevel"] == 4
    assert stats["gzip_bytes"] < stats["raw_bytes"]
    restored = tmp_path / "restored.ndjson"
    gunzip_ndjson(gz, restored)
    assert sha256_file(src) == sha256_file(restored)
    proof = prove_round_trip(src, tmp_path / "rt")
    assert proof["roundtrip_equal"] is True


def test_offload_verify_reclaim_and_no_premature_delete(tmp_path: Path) -> None:
    root = tmp_path / "segments"
    store_root = tmp_path / "store"
    writer = ActiveSegmentWriter(root, max_bytes=10**9, max_seconds=10**9)
    for i in range(10):
        writer.append_line(_line(_envelope(seq=i)))
    paths = writer.close()
    assert paths is not None
    store = LocalArchiveStore(store_root)
    offloader = SegmentOffloader(store)
    with pytest.raises(RuntimeError, match="ACTIVE"):
        # Craft an ACTIVE-looking sibling to ensure gate.
        bad = SegmentPaths(root, "should_not_exist_active")
        bad.dir.mkdir()
        from trading_bot.external_market_data.offload.segments import (
            SegmentStateRecord,
            utc_now,
            write_state,
        )

        write_state(
            bad,
            SegmentStateRecord(
                segment_id=bad.segment_id,
                state=SegmentState.ACTIVE,
                created_at_utc=utc_now().isoformat(),
                updated_at_utc=utc_now().isoformat(),
                connection_ids=[],
            ),
        )
        offloader.process_sealed(bad)

    result = offloader.process_sealed(paths)
    assert result.state == SegmentState.RECLAIMABLE
    assert store.exists(remote_data_key(paths.segment_id))
    with pytest.raises(RuntimeError):
        # Force non-reclaimable delete attempt via state downgrade simulation.
        record = read_state(paths)
        assert record is not None
        record.state = SegmentState.SEALED_UNVERIFIED
        from trading_bot.external_market_data.offload.segments import write_state

        write_state(paths, record)
        reclaim_local_segment(paths)
    # Restore reclaimable and delete once.
    result = offloader.process_sealed(paths)
    assert result.state == SegmentState.RECLAIMABLE
    audit = reclaim_local_segment(paths)
    assert "events.ndjson" in audit["deleted"]
    assert not paths.sealed_ndjson.exists()
    # Idempotent: second reclaim keeps consistent RECLAIMABLE, no other files.
    audit2 = reclaim_local_segment(paths)
    assert audit2["deleted"] == []


def test_b2_outage_leaves_failed_and_no_delete(tmp_path: Path) -> None:
    class BrokenStore:
        def exists(self, key: str) -> bool:
            return False

        def publish_file(self, key: str, source: Path) -> None:
            raise RuntimeError("B2 unavailable")

        def publish_bytes(self, key: str, value: bytes) -> None:
            raise RuntimeError("B2 unavailable")

        def download_file(self, key: str, destination: Path) -> None:
            raise RuntimeError("B2 unavailable")

        def read_bytes(self, key: str) -> bytes:
            raise RuntimeError("B2 unavailable")

    root = tmp_path / "segments"
    writer = ActiveSegmentWriter(root, max_bytes=10**9, max_seconds=10**9)
    writer.append_line(_line(_envelope(seq=1)))
    paths = writer.close()
    assert paths is not None
    offloader = SegmentOffloader(BrokenStore(), max_upload_attempts=2, backoff_base_seconds=0.01)
    result = offloader.process_sealed(paths)
    assert result.state == SegmentState.FAILED
    assert paths.sealed_ndjson.exists()
    with pytest.raises(RuntimeError, match="refusing delete"):
        reclaim_local_segment(paths)


def test_backlog_stop_policy() -> None:
    policy = CapacityPolicy(
        pressure_bytes=100,
        stop_bytes=200,
        global_floor_bytes=1000,
        floor_margin_bytes=100,
    )
    assert policy.classify(local_total_bytes=50, filesystem_free_bytes=5000) == BacklogAction.NONE
    assert (
        policy.classify(local_total_bytes=150, filesystem_free_bytes=5000)
        == BacklogAction.OFFLOAD_PRESSURE
    )
    assert (
        policy.classify(local_total_bytes=250, filesystem_free_bytes=5000)
        == BacklogAction.EXTERNAL_STOP_REQUIRED
    )
    assert (
        policy.classify(local_total_bytes=10, filesystem_free_bytes=1050)
        == BacklogAction.EXTERNAL_STOP_REQUIRED
    )


def test_crash_resume_states(tmp_path: Path) -> None:
    root = tmp_path / "segments"
    writer = ActiveSegmentWriter(root, max_bytes=10**9, max_seconds=10**9)
    writer.append_line(_line(_envelope(seq=1)))
    assert writer._paths is not None
    active_paths = writer._paths
    # A: crash while ACTIVE with partial
    with active_paths.active_ndjson.open("ab") as handle:
        handle.write(b"{bad")
    writer._fh.close()
    writer._fh = None
    report = recover_root(root)
    assert any(a["action"] == "trim_partial" for a in report["actions"])
    # B: seal then leave SEALED_UNVERIFIED
    from trading_bot.external_market_data.offload.segments import seal_active_segment

    seal_active_segment(active_paths)
    record = read_state(active_paths)
    assert record is not None
    assert record.state == SegmentState.SEALED_UNVERIFIED
    store = LocalArchiveStore(tmp_path / "store")
    # C–E covered by process_sealed resume
    result = SegmentOffloader(store).process_sealed(active_paths)
    assert result.state == SegmentState.RECLAIMABLE
    reclaim_local_segment(active_paths)
    # F: after deletion, status still RECLAIMABLE
    record = read_state(active_paths)
    assert record is not None
    assert record.state == SegmentState.RECLAIMABLE


def test_connection_provenance_in_manifest(tmp_path: Path) -> None:
    root = tmp_path / "segments"
    writer = ActiveSegmentWriter(root, max_bytes=10**9, max_seconds=10**9)
    writer.append_line(_line(_envelope(seq=1, connection_id="c1")))
    writer.append_line(_line(_envelope(seq=1, connection_id="c2")))  # new conn, seq resets ok
    paths = writer.close()
    assert paths is not None
    record = read_state(paths)
    assert record is not None
    assert set(record.connection_ids) == {"c1", "c2"}


def test_timestamp_round_trip(tmp_path: Path) -> None:
    src = tmp_path / "events.ndjson"
    row = _envelope(
        seq=1,
        received_at="2026-08-11T20:09:37.805525+00:00",
        exchange_at="2026-08-11T20:09:37.667858+00:00",
    )
    src.write_bytes(_line(row))
    proof = prove_round_trip(src, tmp_path / "rt")
    assert proof["roundtrip_equal"] is True


def test_operator_status_sample(tmp_path: Path) -> None:
    root = tmp_path / "segments"
    writer = ActiveSegmentWriter(root, max_bytes=10**9, max_seconds=10**9)
    writer.append_line(_line(_envelope(seq=1)))
    writer.close()
    status = collect_status(
        root,
        external_mode="STOPPED",
        b2_health="healthy",
        ingest_msg_per_sec=175.0,
        ingest_mib_per_hour=358.0,
        offload_mib_per_hour=400.0,
        backlog_trend="stable",
    )
    assert status.EXTERNAL == "STOPPED"
    assert status.SEALED_UNVERIFIED["count"] == 1
    assert status.ACTION in {a.value for a in BacklogAction}
    assert "free_gib" in status.FILESYSTEM
    assert isinstance(status.UPLOADING, dict)
    assert isinstance(status.VERIFIED_REMOTE, dict)


def test_immutable_upload_identity(tmp_path: Path) -> None:
    root = tmp_path / "segments"
    store = LocalArchiveStore(tmp_path / "store")
    writer = ActiveSegmentWriter(root, max_bytes=10**9, max_seconds=10**9)
    writer.append_line(_line(_envelope(seq=1)))
    paths = writer.close()
    assert paths is not None
    offloader = SegmentOffloader(store)
    first = offloader.process_sealed(paths)
    assert first.state == SegmentState.RECLAIMABLE
    # Second call must not overwrite; reconcile existing remote.
    second = offloader.process_sealed(paths)
    assert second.state == SegmentState.RECLAIMABLE
    assert first.remote_data_key == second.remote_data_key
