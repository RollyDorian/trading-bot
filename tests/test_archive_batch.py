"""Tests for bounded multi-window batch archive planning and execution."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from trading_bot import archive_cli
from trading_bot.archive.batch import (
    DEFAULT_BATCH_MAX_WINDOWS,
    DEFAULT_WINDOW_SECONDS,
    HARD_BATCH_MAX_WINDOWS,
    HARD_BATCH_PLAN_DURATION_SECONDS,
    HARD_MAX_BUNDLE_BYTES,
    PLAN_FILENAME,
    PLAN_SHA256_FILENAME,
    RUN_STATUS_FAILED,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_PASS,
    WINDOW_STATE_COMPLETED,
    WINDOW_STATE_COMPLETED_ADMISSIBLE,
    WINDOW_STATE_COMPLETED_QUARANTINED,
    WINDOW_STATE_FAILED_STORAGE,
    WINDOW_STATE_PENDING,
    WINDOW_STATE_SKIPPED_QUARANTINED,
    WINDOW_STATE_SKIPPED_VERIFIED,
    BatchArchiveError,
    BatchPlanLimits,
    BatchRunLimits,
    batch_root_for_plan,
    batch_verification_path,
    build_batch_plan,
    load_batch_plan,
    progress_path,
    reconcile_batch,
    run_batch_plan,
    write_batch_plan,
)
from trading_bot.archive.store import LocalArchiveStore
from trading_bot.archive.window import (
    INCOMPLETE_MARKER_NAME,
    OPERATIONAL_DISK_FLOOR_BYTES,
    QUARANTINE_REGISTRY_KEY,
    WindowExportLimits,
    _completed_key,
    _incomplete_key,
    _write_logical_checksums,
    _write_physical_checksums,
    build_archive_bundle,
    upload_archive_bundle,
)
from trading_bot.research.dataset import generate_dataset_id, sha256_file
from trading_bot.storage.models import MarketEvent

START = datetime(2026, 7, 18, 0, 0, tzinfo=UTC)
WINDOW_SECONDS = DEFAULT_WINDOW_SECONDS
PLAN_END = START + timedelta(seconds=WINDOW_SECONDS * 3)
KNOWN_GIT_SHA = "abc123deadbeef00000000000000000000000001"


def _event(event_id: int, at: datetime, *, price: str = "100") -> MarketEvent:
    return MarketEvent(
        id=event_id,
        received_at=at,
        exchange_at=at,
        source="hibachi_ws",
        event_type="trades",
        symbol="ETH/USDT-P",
        sequence=event_id,
        connection_id="11111111-1111-1111-1111-111111111111",
        local_sequence=event_id,
        exchange_sequence=event_id,
        schema_version=2,
        latency_ms=1.0,
        payload={"topic": "trades", "price": price, "quantity": "1"},
    )


def _events_for_window(index: int, count: int = 2) -> list[MarketEvent]:
    window_start = START + timedelta(seconds=WINDOW_SECONDS * index)
    return [
        _event(
            index * 100 + offset + 1,
            window_start + timedelta(seconds=offset + 1),
            price=str(100 + offset),
        )
        for offset in range(count)
    ]


async def _fixed_count_provider(
    _symbol: str,
    start: datetime,
    _end: datetime,
) -> tuple[int, int]:
    index = int((start - START).total_seconds() // WINDOW_SECONDS)
    events = _events_for_window(index)
    return len(events), len(events)


async def _zero_count_provider(
    _symbol: str,
    _start: datetime,
    _end: datetime,
) -> tuple[int, int]:
    return 0, 0


def _sufficient_free_bytes() -> int:
    return OPERATIONAL_DISK_FLOOR_BYTES + 4 * HARD_MAX_BUNDLE_BYTES


@pytest.fixture(autouse=True)
def _mock_disk_headroom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "trading_bot.archive.window.shutil.disk_usage",
        lambda _path: type("Usage", (), {"free": _sufficient_free_bytes()})(),
    )
    monkeypatch.setattr(
        "trading_bot.archive.batch.shutil.disk_usage",
        lambda _path: type("Usage", (), {"free": _sufficient_free_bytes()})(),
    )


@pytest.fixture(autouse=True)
def _mock_git_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "trading_bot.research.dataset._git_commit",
        lambda: KNOWN_GIT_SHA,
    )
    monkeypatch.setattr(
        "trading_bot.archive.batch._git_commit",
        lambda: KNOWN_GIT_SHA,
    )


async def _build_plan(
  *,
    start: datetime = START,
    end: datetime = PLAN_END,
    window_seconds: int = WINDOW_SECONDS,
    count_provider=_fixed_count_provider,
    limits: BatchPlanLimits | None = None,
) -> dict[str, Any]:
    return await build_batch_plan(
        symbol="ETH/USDT-P",
        start=start,
        end=end,
        window_seconds=window_seconds,
        limits=limits or BatchPlanLimits(window_seconds=window_seconds),
        count_provider=count_provider,
    )


def _write_plan_files(tmp_path: Path, plan: dict[str, Any]) -> Path:
    plan_dir = tmp_path / "plans"
    write_batch_plan(plan, plan_dir)
    return plan_dir / PLAN_FILENAME


def _seed_completed_store(
    store: LocalArchiveStore,
    bundle_dir: Path,
    *,
    verification_root: Path,
    attempt_id: str = "fixed-attempt",
    confirm_quarantine_upload: bool = False,
) -> str:
    dataset_id = bundle_dir.name
    monkeypatch_attempt = attempt_id

    import trading_bot.archive.window as window_mod

    original = window_mod._new_attempt_id
    window_mod._new_attempt_id = lambda: monkeypatch_attempt
    try:
        upload = upload_archive_bundle(
            bundle_dir,
            store,
            confirm_upload=True,
            confirm_quarantine_upload=confirm_quarantine_upload,
            verification_root=verification_root,
        )
    finally:
        window_mod._new_attempt_id = original
    assert upload["status"] == "verified"
    return dataset_id


def _set_rejected_quality(bundle: Path) -> None:
    quality_path = bundle / "quality_report.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality["status"] = "rejected"
    quality_path.write_text(json.dumps(quality), encoding="utf-8")
    metadata_path = bundle / "archive_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["quarantined"] = True
    metadata["research_quality_status"] = "rejected"
    metadata["admission_eligible"] = False
    metadata["quarantine_reasons"] = list(quality.get("findings", []))
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _write_logical_checksums(bundle)
    _write_physical_checksums(bundle)


@pytest.mark.asyncio
async def test_plan_requires_timezone_aware() -> None:
    with pytest.raises(BatchArchiveError, match="timezone-aware"):
        await _build_plan(start=datetime(2026, 7, 18))


@pytest.mark.asyncio
async def test_plan_requires_end_after_start() -> None:
    with pytest.raises(BatchArchiveError, match="end must be after start"):
        await _build_plan(start=PLAN_END, end=START)


@pytest.mark.asyncio
async def test_plan_requires_exact_window_multiple() -> None:
    bad_end = START + timedelta(seconds=WINDOW_SECONDS * 2 + 30)
    with pytest.raises(BatchArchiveError, match="exact multiple"):
        await _build_plan(end=bad_end)


async def _fake_load_for_start(*args: object, **kwargs: object) -> list[MarketEvent]:
    start = cast(datetime, args[2] if len(args) > 2 else kwargs["start"])
    index = int((start - START).total_seconds() // WINDOW_SECONDS)
    return _events_for_window(index)


@pytest.mark.asyncio
async def test_plan_rejects_too_many_windows() -> None:
    end = START + timedelta(seconds=WINDOW_SECONDS * (HARD_BATCH_MAX_WINDOWS + 1))
    limits = BatchPlanLimits(
        window_seconds=WINDOW_SECONDS,
        max_plan_duration_seconds=HARD_BATCH_PLAN_DURATION_SECONDS,
    )
    with pytest.raises(BatchArchiveError, match="window count exceeds"):
        await _build_plan(end=end, limits=limits)


@pytest.mark.asyncio
async def test_plan_rejects_duration_over_cap() -> None:
    limits = BatchPlanLimits(
        window_seconds=WINDOW_SECONDS,
        max_plan_duration_seconds=WINDOW_SECONDS * 2,
    )
    end = START + timedelta(seconds=WINDOW_SECONDS * 3)
    with pytest.raises(BatchArchiveError, match="duration exceeds"):
        await _build_plan(end=end, limits=limits)


@pytest.mark.asyncio
async def test_deterministic_plan_id_and_dataset_ids() -> None:
    first = await _build_plan()
    second = await _build_plan()
    assert first["plan_id"] == second["plan_id"]
    assert first["windows"] == second["windows"]
    for window in first["windows"]:
        expected = generate_dataset_id(
            "ETH/USDT-P",
            datetime.fromisoformat(window["start_utc"]),
            datetime.fromisoformat(window["end_utc"]),
        )
        assert window["dataset_id"] == expected


@pytest.mark.asyncio
async def test_plan_includes_git_commit_and_counts() -> None:
    plan = await _build_plan()
    assert plan["created_with_git_commit"] == KNOWN_GIT_SHA
    assert plan["range_expected_event_count"] == 6
    assert plan["range_expected_trade_count"] == 6
    assert len(plan["windows"]) == 3


def test_immutable_plan_refuses_second_write(tmp_path: Path) -> None:
    plan = {
        "plan_id": "batch_test",
        "schema_version": 1,
        "windows": [],
    }
    output = tmp_path / "plans"
    write_batch_plan(plan, output)
    with pytest.raises(BatchArchiveError, match="already exists"):
        write_batch_plan(plan, output)


def test_batch_root_defaults_to_plan_directory(tmp_path: Path) -> None:
    plan_dir = tmp_path / "pilot-out"
    plan_path = plan_dir / PLAN_FILENAME
    plan_dir.mkdir(parents=True)
    plan_path.write_text("{}", encoding="utf-8")
    assert batch_root_for_plan(plan_path, None) == plan_dir.resolve()
    override = tmp_path / "other-root"
    assert batch_root_for_plan(plan_path, override) == override.resolve()


def test_load_plan_verifies_sha256(tmp_path: Path) -> None:
    plan_dir = tmp_path / "plans"
    plan = {"plan_id": "batch_test", "schema_version": 1, "windows": []}
    write_batch_plan(plan, plan_dir)
    plan_path = plan_dir / PLAN_FILENAME
    loaded, digest = load_batch_plan(plan_path)
    assert loaded["plan_id"] == "batch_test"
    assert digest == sha256_file(plan_path)
    plan_path.write_text(plan_path.read_text() + " ", encoding="utf-8")
    with pytest.raises(BatchArchiveError, match="checksum mismatch"):
        load_batch_plan(plan_path)


@pytest.mark.asyncio
async def test_resumed_run_processes_only_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = await _build_plan()
    plan_path = _write_plan_files(tmp_path, plan)
    batch_root = tmp_path / "batch"
    store = LocalArchiveStore(batch_root / "remote")
    session_factory = AsyncMock()

    monkeypatch.setattr("trading_bot.archive.batch.load_window_events", _fake_load_for_start)

    progress_file = progress_path(batch_root, str(plan["plan_id"]))
    progress_file.parent.mkdir(parents=True, exist_ok=True)
    progress = {
        "plan_id": plan["plan_id"],
        "schema_version": 1,
        "windows": {
            "0": {"status": WINDOW_STATE_COMPLETED, "dataset_id": plan["windows"][0]["dataset_id"]},
            "1": {"status": WINDOW_STATE_PENDING, "dataset_id": plan["windows"][1]["dataset_id"]},
            "2": {"status": WINDOW_STATE_PENDING, "dataset_id": plan["windows"][2]["dataset_id"]},
        },
        "upload_bytes_this_run": 0,
        "status": "running",
    }
    progress_file.write_text(json.dumps(progress), encoding="utf-8")

    result = await run_batch_plan(
        plan_path,
        batch_root=batch_root,
        store=store,
        session_factory=session_factory,
        confirm_upload=True,
        run_limits=BatchRunLimits(max_windows=2),
    )
    assert result["status"] in {RUN_STATUS_PARTIAL, RUN_STATUS_PASS}
    updated = json.loads(progress_file.read_text(encoding="utf-8"))
    assert updated["windows"]["0"]["status"] == WINDOW_STATE_COMPLETED_ADMISSIBLE
    assert updated["windows"]["1"]["status"] in {
        WINDOW_STATE_COMPLETED_ADMISSIBLE,
        WINDOW_STATE_SKIPPED_VERIFIED,
    }


@pytest.mark.asyncio
async def test_existing_completed_is_skipped_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = await _build_plan()
    plan["windows"] = [plan["windows"][0]]
    plan["range_expected_event_count"] = plan["windows"][0]["expected_event_count"]
    plan["range_expected_trade_count"] = plan["windows"][0]["expected_trade_count"]
    plan_dir = tmp_path / "plans"
    write_batch_plan(plan, plan_dir)
    plan_path = plan_dir / PLAN_FILENAME

    batch_root = tmp_path / "batch"
    store = LocalArchiveStore(batch_root / "remote")
    bundle = build_archive_bundle(
        symbol="ETH/USDT-P",
        start=datetime.fromisoformat(plan["windows"][0]["start_utc"]),
        end=datetime.fromisoformat(plan["windows"][0]["end_utc"]),
        output_dir=tmp_path / "bundles",
        events=_events_for_window(0),
        limits=WindowExportLimits(max_duration_seconds=WINDOW_SECONDS),
    )

    monkeypatch.setattr("trading_bot.archive.window._new_attempt_id", lambda: "fixed-attempt")
    upload = upload_archive_bundle(
        bundle,
        store,
        confirm_upload=True,
        verification_root=batch_root / "_batch" / plan["plan_id"] / "_verification",
    )
    assert upload["status"] == "verified"

    session_factory = AsyncMock()
    result = await run_batch_plan(
        plan_path,
        batch_root=batch_root,
        store=store,
        session_factory=session_factory,
        confirm_upload=True,
        run_limits=BatchRunLimits(max_windows=1),
    )
    assert result["status"] == RUN_STATUS_PASS
    progress = json.loads(progress_path(batch_root, plan["plan_id"]).read_text(encoding="utf-8"))
    assert progress["windows"]["0"]["status"] == WINDOW_STATE_SKIPPED_VERIFIED


@pytest.mark.asyncio
async def test_mismatched_completed_event_count_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = await _build_plan(count_provider=_zero_count_provider)
    plan["windows"] = [plan["windows"][0]]
    plan["range_expected_event_count"] = 0
    plan_dir = tmp_path / "plans"
    write_batch_plan(plan, plan_dir)
    plan_path = plan_dir / PLAN_FILENAME
    batch_root = tmp_path / "batch"
    store = LocalArchiveStore(batch_root / "remote")

    events = _events_for_window(0, count=1)
    bundle = build_archive_bundle(
        symbol="ETH/USDT-P",
        start=datetime.fromisoformat(plan["windows"][0]["start_utc"]),
        end=datetime.fromisoformat(plan["windows"][0]["end_utc"]),
        output_dir=tmp_path / "bundles",
        events=events,
        limits=WindowExportLimits(max_duration_seconds=WINDOW_SECONDS),
    )
    monkeypatch.setattr("trading_bot.archive.window._new_attempt_id", lambda: "fixed-attempt")
    assert (
        upload_archive_bundle(
            bundle,
            store,
            confirm_upload=True,
            verification_root=batch_root / "_verification",
        )["status"]
        == "verified"
    )

    result = await run_batch_plan(
        plan_path,
        batch_root=batch_root,
        store=store,
        session_factory=AsyncMock(),
        confirm_upload=True,
        run_limits=BatchRunLimits(max_windows=1),
    )
    assert result["status"] == RUN_STATUS_FAILED
    progress = json.loads(progress_path(batch_root, plan["plan_id"]).read_text(encoding="utf-8"))
    assert progress["windows"]["0"]["status"] == WINDOW_STATE_FAILED_STORAGE


@pytest.mark.asyncio
async def test_incomplete_attempt_fail_closed_by_default(
    tmp_path: Path,
) -> None:
    plan = await _build_plan()
    plan["windows"] = [plan["windows"][0]]
    plan_dir = tmp_path / "plans"
    write_batch_plan(plan, plan_dir)
    plan_path = plan_dir / PLAN_FILENAME
    batch_root = tmp_path / "batch"
    store = LocalArchiveStore(batch_root / "remote")
    dataset_id = plan["windows"][0]["dataset_id"]
    incomplete_key = _incomplete_key(dataset_id, "stale-attempt")
    store.publish_bytes(
        incomplete_key,
        json.dumps({"status": INCOMPLETE_MARKER_NAME}).encode("utf-8"),
    )

    result = await run_batch_plan(
        plan_path,
        batch_root=batch_root,
        store=store,
        session_factory=AsyncMock(),
        confirm_upload=True,
        run_limits=BatchRunLimits(max_windows=1),
    )
    assert result["status"] == RUN_STATUS_FAILED
    assert "incomplete" in str(result.get("error", "")).lower()


@pytest.mark.asyncio
async def test_incomplete_attempt_allows_new_attempt_with_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = await _build_plan()
    plan["windows"] = [plan["windows"][0]]
    plan["range_expected_event_count"] = plan["windows"][0]["expected_event_count"]
    plan["range_expected_trade_count"] = plan["windows"][0]["expected_trade_count"]
    plan_dir = tmp_path / "plans"
    write_batch_plan(plan, plan_dir)
    plan_path = plan_dir / PLAN_FILENAME
    batch_root = tmp_path / "batch"
    store = LocalArchiveStore(batch_root / "remote")
    dataset_id = plan["windows"][0]["dataset_id"]
    store.publish_bytes(
        _incomplete_key(dataset_id, "stale-attempt"),
        json.dumps({"status": INCOMPLETE_MARKER_NAME}).encode("utf-8"),
    )

    monkeypatch.setattr("trading_bot.archive.batch.load_window_events", _fake_load_for_start)
    monkeypatch.setattr("trading_bot.archive.window._new_attempt_id", lambda: "new-attempt")

    result = await run_batch_plan(
        plan_path,
        batch_root=batch_root,
        store=store,
        session_factory=AsyncMock(),
        confirm_upload=True,
        run_limits=BatchRunLimits(
            max_windows=1,
            allow_new_attempt_after_incomplete=True,
        ),
    )
    assert result["status"] == RUN_STATUS_PASS
    assert store.exists(_completed_key(dataset_id))


@pytest.mark.asyncio
async def test_failure_on_middle_window_stops_later_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = await _build_plan(count_provider=_zero_count_provider)
    plan_dir = tmp_path / "plans"
    write_batch_plan(plan, plan_dir)
    plan_path = plan_dir / PLAN_FILENAME
    batch_root = tmp_path / "batch"
    store = LocalArchiveStore(batch_root / "remote")

    call_count = {"n": 0}

    async def flaky_load(*args: object, **kwargs: object) -> list[MarketEvent]:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise BatchArchiveError("simulated export failure")
        return await _fake_load_for_start(*args, **kwargs)

    monkeypatch.setattr("trading_bot.archive.batch.load_window_events", flaky_load)
    monkeypatch.setattr("trading_bot.archive.window._new_attempt_id", lambda: "attempt")

    result = await run_batch_plan(
        plan_path,
        batch_root=batch_root,
        store=store,
        session_factory=AsyncMock(),
        confirm_upload=True,
        run_limits=BatchRunLimits(max_windows=3),
    )
    assert result["status"] == RUN_STATUS_FAILED
    progress = json.loads(progress_path(batch_root, plan["plan_id"]).read_text(encoding="utf-8"))
    assert progress["windows"]["1"]["status"] == WINDOW_STATE_FAILED_STORAGE
    assert progress["windows"]["2"]["status"] == WINDOW_STATE_PENDING


def test_progress_atomic_write(tmp_path: Path) -> None:
    from trading_bot.archive.batch import _atomic_write_json, _initial_progress

    plan = {"plan_id": "batch_atomic", "windows": [{"index": 0, "dataset_id": "ds0"}]}
    progress = _initial_progress(plan)
    path = progress_path(tmp_path, "batch_atomic")
    _atomic_write_json(path, progress)
    assert path.is_file()
    assert not any(path.parent.glob(f".{path.name}.partial-*"))


@pytest.mark.asyncio
async def test_reconciliation_pass_writes_batch_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = await _build_plan()
    plan["windows"] = plan["windows"][:1]
    plan["range_expected_event_count"] = plan["windows"][0]["expected_event_count"]
    plan["range_expected_trade_count"] = plan["windows"][0]["expected_trade_count"]
    plan_dir = tmp_path / "plans"
    write_batch_plan(plan, plan_dir)
    plan_path = plan_dir / PLAN_FILENAME
    batch_root = tmp_path / "batch"
    store = LocalArchiveStore(batch_root / "remote")

    monkeypatch.setattr("trading_bot.archive.batch.load_window_events", _fake_load_for_start)
    monkeypatch.setattr("trading_bot.archive.window._new_attempt_id", lambda: "attempt")

    result = await run_batch_plan(
        plan_path,
        batch_root=batch_root,
        store=store,
        session_factory=AsyncMock(),
        confirm_upload=True,
        run_limits=BatchRunLimits(max_windows=1),
    )
    assert result["status"] == RUN_STATUS_PASS
    verification = batch_verification_path(batch_root, plan["plan_id"])
    assert verification.is_file()
    payload = json.loads(verification.read_text(encoding="utf-8"))
    assert payload["status"] == RUN_STATUS_PASS
    assert payload["retention_authorized"] is False
    assert payload["event_total_reconciled"] is True
    assert payload["admissible_coverage_continuous"] is True


@pytest.mark.asyncio
async def test_batch_rejected_upload_blocked_without_quarantine_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = await _build_plan(count_provider=_zero_count_provider)
    plan["windows"] = [plan["windows"][0]]
    plan["range_expected_event_count"] = 2
    plan["range_expected_trade_count"] = 2
    plan["windows"][0]["expected_event_count"] = 2
    plan["windows"][0]["expected_trade_count"] = 2
    plan_dir = tmp_path / "plans"
    write_batch_plan(plan, plan_dir)
    plan_path = plan_dir / PLAN_FILENAME
    batch_root = tmp_path / "batch"
    store = LocalArchiveStore(batch_root / "remote")

    original_build = build_archive_bundle

    def build_with_rejected(*args: object, **kwargs: object) -> Path:
        bundle = original_build(*args, **kwargs)
        _set_rejected_quality(bundle)
        return bundle

    monkeypatch.setattr("trading_bot.archive.batch.build_archive_bundle", build_with_rejected)
    monkeypatch.setattr("trading_bot.archive.batch.load_window_events", _fake_load_for_start)
    monkeypatch.setattr("trading_bot.archive.window._new_attempt_id", lambda: "attempt")

    result = await run_batch_plan(
        plan_path,
        batch_root=batch_root,
        store=store,
        session_factory=AsyncMock(),
        confirm_upload=True,
        run_limits=BatchRunLimits(max_windows=1, confirm_quarantine_upload=False),
    )
    assert result["status"] == RUN_STATUS_FAILED
    progress = json.loads(progress_path(batch_root, plan["plan_id"]).read_text(encoding="utf-8"))
    assert progress["windows"]["0"]["status"] == WINDOW_STATE_FAILED_STORAGE


@pytest.mark.asyncio
async def test_existing_quarantined_completed_is_skipped_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = await _build_plan()
    plan["windows"] = [plan["windows"][0]]
    plan["range_expected_event_count"] = plan["windows"][0]["expected_event_count"]
    plan["range_expected_trade_count"] = plan["windows"][0]["expected_trade_count"]
    plan_dir = tmp_path / "plans"
    write_batch_plan(plan, plan_dir)
    plan_path = plan_dir / PLAN_FILENAME

    batch_root = tmp_path / "batch"
    store = LocalArchiveStore(batch_root / "remote")
    bundle = build_archive_bundle(
        symbol="ETH/USDT-P",
        start=datetime.fromisoformat(plan["windows"][0]["start_utc"]),
        end=datetime.fromisoformat(plan["windows"][0]["end_utc"]),
        output_dir=tmp_path / "bundles",
        events=_events_for_window(0),
        limits=WindowExportLimits(max_duration_seconds=WINDOW_SECONDS),
    )
    _set_rejected_quality(bundle)

    monkeypatch.setattr("trading_bot.archive.window._new_attempt_id", lambda: "fixed-attempt")
    upload = upload_archive_bundle(
        bundle,
        store,
        confirm_upload=True,
        confirm_quarantine_upload=True,
        verification_root=batch_root / "_batch" / plan["plan_id"] / "_verification",
    )
    assert upload["status"] == "verified"

    session_factory = AsyncMock()
    result = await run_batch_plan(
        plan_path,
        batch_root=batch_root,
        store=store,
        session_factory=session_factory,
        confirm_upload=True,
        run_limits=BatchRunLimits(max_windows=1),
    )
    assert result["status"] == RUN_STATUS_PASS
    progress = json.loads(progress_path(batch_root, plan["plan_id"]).read_text(encoding="utf-8"))
    assert progress["windows"]["0"]["status"] == WINDOW_STATE_SKIPPED_QUARANTINED
    reconciliation = result["reconciliation"]
    assert reconciliation["quarantined_event_total"] == plan["windows"][0]["expected_event_count"]
    assert reconciliation["admissible_event_total"] == 0
    assert reconciliation["admissible_coverage_continuous"] is False
    assert reconciliation["retention_authorized"] is False
    assert reconciliation["all_storage_complete"] is True


@pytest.mark.asyncio
async def test_batch_quarantined_upload_via_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = await _build_plan(count_provider=_zero_count_provider)
    plan["windows"] = [plan["windows"][0]]
    plan["range_expected_event_count"] = 2
    plan["range_expected_trade_count"] = 2
    plan["windows"][0]["expected_event_count"] = 2
    plan["windows"][0]["expected_trade_count"] = 2
    plan_dir = tmp_path / "plans"
    write_batch_plan(plan, plan_dir)
    plan_path = plan_dir / PLAN_FILENAME
    batch_root = tmp_path / "batch"
    store = LocalArchiveStore(batch_root / "remote")

    original_build = build_archive_bundle

    def build_with_rejected(*args: object, **kwargs: object) -> Path:
        bundle = original_build(*args, **kwargs)
        _set_rejected_quality(bundle)
        return bundle

    monkeypatch.setattr("trading_bot.archive.batch.build_archive_bundle", build_with_rejected)
    monkeypatch.setattr("trading_bot.archive.batch.load_window_events", _fake_load_for_start)
    monkeypatch.setattr("trading_bot.archive.window._new_attempt_id", lambda: "attempt")

    result = await run_batch_plan(
        plan_path,
        batch_root=batch_root,
        store=store,
        session_factory=AsyncMock(),
        confirm_upload=True,
        run_limits=BatchRunLimits(max_windows=1, confirm_quarantine_upload=True),
    )
    assert result["status"] == RUN_STATUS_PASS
    progress = json.loads(progress_path(batch_root, plan["plan_id"]).read_text(encoding="utf-8"))
    assert progress["windows"]["0"]["status"] == WINDOW_STATE_COMPLETED_QUARANTINED
    reconciliation = result["reconciliation"]
    assert reconciliation["quarantined_event_total"] == 2
    assert reconciliation["admissible_coverage_continuous"] is False
    assert store.exists(QUARANTINE_REGISTRY_KEY)


@pytest.mark.asyncio
async def test_max_windows_stops_with_partial_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = await _build_plan()
    plan_dir = tmp_path / "plans"
    write_batch_plan(plan, plan_dir)
    plan_path = plan_dir / PLAN_FILENAME
    batch_root = tmp_path / "batch"
    store = LocalArchiveStore(batch_root / "remote")

    monkeypatch.setattr("trading_bot.archive.batch.load_window_events", _fake_load_for_start)
    monkeypatch.setattr("trading_bot.archive.window._new_attempt_id", lambda: "attempt")

    result = await run_batch_plan(
        plan_path,
        batch_root=batch_root,
        store=store,
        session_factory=AsyncMock(),
        confirm_upload=True,
        run_limits=BatchRunLimits(max_windows=1),
    )
    assert result["status"] == RUN_STATUS_PARTIAL
    assert result["processed_this_run"] == 1


@pytest.mark.asyncio
async def test_max_upload_bytes_stops_before_next_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = await _build_plan()
    plan_dir = tmp_path / "plans"
    write_batch_plan(plan, plan_dir)
    plan_path = plan_dir / PLAN_FILENAME
    batch_root = tmp_path / "batch"
    store = LocalArchiveStore(batch_root / "remote")

    monkeypatch.setattr("trading_bot.archive.batch.load_window_events", _fake_load_for_start)
    monkeypatch.setattr("trading_bot.archive.window._new_attempt_id", lambda: "attempt")

    result = await run_batch_plan(
        plan_path,
        batch_root=batch_root,
        store=store,
        session_factory=AsyncMock(),
        confirm_upload=True,
        run_limits=BatchRunLimits(
            max_windows=DEFAULT_BATCH_MAX_WINDOWS,
            max_upload_bytes=HARD_MAX_BUNDLE_BYTES,
        ),
    )
    assert result["status"] == RUN_STATUS_PARTIAL
    assert "max_upload_bytes" in str(result.get("reason", ""))


@pytest.mark.asyncio
async def test_disk_bounds_stop_run_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = await _build_plan()
    plan["windows"] = [plan["windows"][0]]
    plan_dir = tmp_path / "plans"
    write_batch_plan(plan, plan_dir)
    plan_path = plan_dir / PLAN_FILENAME
    batch_root = tmp_path / "batch"
    store = LocalArchiveStore(batch_root / "remote")

    monkeypatch.setattr(
        "trading_bot.archive.batch._disk_free_bytes",
        lambda _path: OPERATIONAL_DISK_FLOOR_BYTES,
    )

    result = await run_batch_plan(
        plan_path,
        batch_root=batch_root,
        store=store,
        session_factory=AsyncMock(),
        confirm_upload=True,
        run_limits=BatchRunLimits(max_windows=1),
    )
    assert result["status"] == RUN_STATUS_PARTIAL
    assert "disk" in str(result.get("reason", "")).lower()


def test_reconcile_detects_boundary_continuity() -> None:
    plan = {
        "plan_id": "p",
        "range_expected_event_count": 2,
        "windows": [
            {
                "index": 0,
                "dataset_id": "a",
                "start_utc": START.isoformat(),
                "end_utc": (START + timedelta(hours=1)).isoformat(),
                "expected_event_count": 1,
                "expected_trade_count": 1,
            },
            {
                "index": 1,
                "dataset_id": "b",
                "start_utc": (START + timedelta(hours=2)).isoformat(),
                "end_utc": (START + timedelta(hours=3)).isoformat(),
                "expected_event_count": 1,
                "expected_trade_count": 1,
            },
        ],
    }
    progress = {
        "windows": {
            "0": {"status": WINDOW_STATE_COMPLETED},
            "1": {"status": WINDOW_STATE_COMPLETED},
        }
    }
    report = reconcile_batch(plan, progress, plan_sha256="abc")
    assert report["boundary_continuity"] is False
    assert report["status"] == RUN_STATUS_FAILED


def test_local_store_list_keys_finds_incomplete_marker(tmp_path: Path) -> None:
    store = LocalArchiveStore(tmp_path / "remote")
    dataset_id = "eth-usdt-p_test"
    key = _incomplete_key(dataset_id, "attempt-1")
    store.publish_bytes(key, b"{}")
    keys = store.list_keys(f"archives/{dataset_id}/attempts/")
    assert key in keys


class _DeleteTrackingStore(LocalArchiveStore):
    def delete(self, key: str) -> None:  # pragma: no cover - spy only
        raise AssertionError("delete must not be called")


@pytest.mark.asyncio
async def test_batch_run_never_calls_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = await _build_plan()
    plan["windows"] = [plan["windows"][0]]
    plan_dir = tmp_path / "plans"
    write_batch_plan(plan, plan_dir)
    plan_path = plan_dir / PLAN_FILENAME
    batch_root = tmp_path / "batch"
    store = _DeleteTrackingStore(batch_root / "remote")

    monkeypatch.setattr("trading_bot.archive.batch.load_window_events", _fake_load_for_start)
    monkeypatch.setattr("trading_bot.archive.window._new_attempt_id", lambda: "attempt")

    await run_batch_plan(
        plan_path,
        batch_root=batch_root,
        store=store,
        session_factory=AsyncMock(),
        confirm_upload=True,
        run_limits=BatchRunLimits(max_windows=1),
    )


def test_plan_summary_redacts_secrets(tmp_path: Path) -> None:
    from trading_bot.archive.batch import redacted_plan_summary

    plan = {
        "plan_id": "batch_test",
        "windows": [{"index": 0}],
        "range_expected_event_count": 1,
        "range_expected_trade_count": 1,
    }
    plan_dir = tmp_path / "plans"
    plan_path, sha_path = write_batch_plan(plan, plan_dir)
    summary = redacted_plan_summary(plan, plan_path, sha_path)
    dumped = json.dumps(summary)
    assert "secret" not in dumped.lower()
    assert "access_key" not in dumped.lower()


def test_batch_plan_cli_writes_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _FakeEngine:
        async def dispose(self) -> None:
            return None

    async def fake_build(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return await _build_plan(count_provider=_zero_count_provider)

    monkeypatch.setattr(archive_cli, "create_engine", lambda _url: _FakeEngine())
    monkeypatch.setattr(archive_cli, "build_batch_plan", fake_build)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hibachi-archive",
            "archive-batch-plan",
            "--start",
            START.isoformat(),
            "--end",
            PLAN_END.isoformat(),
            "--output-dir",
            str(tmp_path / "plans"),
        ],
    )
    archive_cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "planned"
    assert (tmp_path / "plans" / PLAN_FILENAME).is_file()
    assert (tmp_path / "plans" / PLAN_SHA256_FILENAME).is_file()


def test_batch_run_cli_requires_confirm_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_dir = tmp_path / "plans"
    plan = {"plan_id": "batch_test", "schema_version": 1, "windows": []}
    write_batch_plan(plan, plan_dir)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hibachi-archive",
            "archive-batch-run",
            "--plan",
            str(plan_dir / PLAN_FILENAME),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        archive_cli.main()
    assert exc.value.code == 2
    assert "confirm-upload" in capsys.readouterr().err.lower()
