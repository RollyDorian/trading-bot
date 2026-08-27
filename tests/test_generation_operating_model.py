"""Unit tests for continuous generation rotation operating model."""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_bot.archive.window import (
    DEFAULT_MAX_ROWS,
    GENERATION_ARCHIVE_DEFAULT_MAX_ROWS,
    HARD_MAX_ROWS,
    WindowExportLimits,
)
from trading_bot.archive.workdir import ArchiveWorkdirError, ensure_archive_workdir
from trading_bot.storage.capacity import (
    HARD_ARCHIVE_FLOOR_BYTES,
    MAX_DROP_ELIGIBLE_GENERATIONS,
    MEASURED_BYTES_PER_EVENT,
    MEASURED_GENERATION_BYTES,
    OPERATOR_EMERGENCY_FLOOR_BYTES,
    WAL_AND_TRANSIENT_BUFFER_BYTES,
    CapacityInputs,
    CapacityState,
    assess_capacity,
    collector_may_resume_after_capacity,
    collector_must_pause_for_capacity,
    drop_backlog_blocks_new_archive,
    emergency_archive_allowed,
    estimate_generation_bytes,
    hours_per_generation,
    normal_ready_target_bytes,
    safe_coexisting_generation_budget,
)
from trading_bot.storage.generation_transitions import (
    ALLOWED_TRANSITIONS,
    assert_transition_allowed,
)
from trading_bot.storage.operator_status import OperatorAction, recommend_action
from trading_bot.storage.partitions import (
    COVER_EXHAUSTION_STOP_ROWS,
    DEFAULT_GENERATION_ROW_SPAN,
    LATE_PROVISION_ROWS,
    PRE_BOUNDARY_PROVISION_ROWS,
    GenerationState,
    PartitionLifecycleError,
    ProvisionUrgency,
    assess_provision_urgency,
)


def test_transition_matrix_rejects_drop_from_active() -> None:
    with pytest.raises(PartitionLifecycleError, match="invalid generation transition"):
        assert_transition_allowed(GenerationState.ACTIVE, GenerationState.DROPPED)


def test_transition_matrix_allows_happy_path() -> None:
    path = [
        GenerationState.PROVISIONED,
        GenerationState.ACTIVE,
        GenerationState.CLOSED_UNARCHIVED,
        GenerationState.ARCHIVING,
        GenerationState.VERIFIED,
        GenerationState.DROP_ELIGIBLE,
        GenerationState.DROPPED,
    ]
    for cur, nxt in zip(path, path[1:], strict=False):
        assert_transition_allowed(cur, nxt)
    assert GenerationState.DROPPED not in ALLOWED_TRANSITIONS[GenerationState.DROPPED]


def test_default_max_rows_covers_production_hourly_window() -> None:
    # Production peak hour observed ~59500–59630 rows; 50k falsely failed.
    assert DEFAULT_MAX_ROWS == GENERATION_ARCHIVE_DEFAULT_MAX_ROWS == 100_000
    assert DEFAULT_MAX_ROWS < HARD_MAX_ROWS
    limits = WindowExportLimits(max_rows=DEFAULT_MAX_ROWS)
    assert 59_630 <= limits.max_rows <= HARD_MAX_ROWS


def test_archive_workdir_writable_and_rejects_world_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "archive-work"
    ensured = ensure_archive_workdir(work)
    assert ensured.is_dir()
    probe = ensured / "ok.txt"
    probe.write_text("x", encoding="utf-8")

    if hasattr(__import__("os"), "geteuid"):
        bad = tmp_path / "world"
        bad.mkdir()
        bad.chmod(0o777)
        with pytest.raises(ArchiveWorkdirError, match="world-writable"):
            ensure_archive_workdir(bad)


def test_capacity_ready_requires_full_closed_generation_headroom() -> None:
    gen = estimate_generation_bytes(bytes_per_event=MEASURED_BYTES_PER_EVENT)
    free = OPERATOR_EMERGENCY_FLOOR_BYTES + gen + 128 * 1024 * 1024
    assessment = assess_capacity(
        CapacityInputs(free_disk_bytes=free, bytes_per_event=MEASURED_BYTES_PER_EVENT)
    )
    assert assessment.state == CapacityState.READY
    assert assessment.continuous_operation_feasible is True
    assert assessment.estimated_generation_bytes == MEASURED_GENERATION_BYTES


def test_capacity_at_production_5_06_gib_is_not_continuously_feasible() -> None:
    # ~5.06 GiB free with 5 GiB emergency floor → ~60 MiB headroom << ~194 MiB gen.
    free = int(5.06 * 1024**3)
    assessment = assess_capacity(
        CapacityInputs(free_disk_bytes=free, bytes_per_event=MEASURED_BYTES_PER_EVENT)
    )
    assert assessment.state in {
        CapacityState.ARCHIVE_PRESSURE,
        CapacityState.STOP_REQUIRED,
    }
    assert assessment.continuous_operation_feasible is False
    assert assessment.headroom_above_emergency_floor_bytes < assessment.estimated_generation_bytes


def test_capacity_at_6_20_gib_allows_limited_backlog() -> None:
    free = int(6.20 * 1024**3)
    assessment = assess_capacity(CapacityInputs(free_disk_bytes=free))
    assert assessment.state == CapacityState.READY
    assert assessment.continuous_operation_feasible is True
    # Headroom ~1.2 GiB → ACTIVE slot + WAL reserved → about 4 backlog gens max.
    assert safe_coexisting_generation_budget(free_disk_bytes=free) >= 2
    with_drop = assess_capacity(
        CapacityInputs(
            free_disk_bytes=free,
            drop_eligible_count=1,
            drop_eligible_bytes=MEASURED_GENERATION_BYTES,
        )
    )
    assert with_drop.state == CapacityState.DROP_APPROVAL_REQUIRED
    assert with_drop.continuous_operation_feasible is True
    over_backlog = assess_capacity(
        CapacityInputs(
            free_disk_bytes=free,
            drop_eligible_count=3,
            drop_eligible_bytes=3 * MEASURED_GENERATION_BYTES,
        )
    )
    assert over_backlog.state == CapacityState.STOP_REQUIRED


def test_capacity_stop_required_below_emergency_floor() -> None:
    assessment = assess_capacity(
        CapacityInputs(free_disk_bytes=OPERATOR_EMERGENCY_FLOOR_BYTES - 1)
    )
    assert assessment.state == CapacityState.STOP_REQUIRED


def test_hours_per_generation_from_canary_rate() -> None:
    hours = hours_per_generation(events_per_hour=59_000.0)
    assert 6.5 <= hours <= 7.0
    assert DEFAULT_GENERATION_ROW_SPAN == 400_000


def test_assess_provision_urgency_thresholds() -> None:
    assert (
        assess_provision_urgency(remaining_ids=60_000, has_successor=False)
        == ProvisionUrgency.NORMAL
    )
    assert (
        assess_provision_urgency(remaining_ids=50_000, has_successor=False)
        == ProvisionUrgency.PROVISION_REQUIRED
    )
    assert (
        assess_provision_urgency(remaining_ids=LATE_PROVISION_ROWS, has_successor=False)
        == ProvisionUrgency.PROVISION_LATE
    )
    assert (
        assess_provision_urgency(
            remaining_ids=COVER_EXHAUSTION_STOP_ROWS, has_successor=False
        )
        == ProvisionUrgency.COVER_STOP_REQUIRED
    )
    assert (
        assess_provision_urgency(remaining_ids=-10, has_successor=False)
        == ProvisionUrgency.COVER_STOP_REQUIRED
    )
    assert (
        assess_provision_urgency(remaining_ids=0, has_successor=True)
        == ProvisionUrgency.NORMAL
    )
    assert PRE_BOUNDARY_PROVISION_ROWS == 50_000
    assert LATE_PROVISION_ROWS == 10_000
    assert COVER_EXHAUSTION_STOP_ROWS == 1_000


def test_recommend_action_missing_successor_outranks_capacity_stop() -> None:
    # Regression: closed-archive STOP must not hide provision lead.
    assert (
        recommend_action(
            capacity_state=CapacityState.STOP_REQUIRED,
            active=object(),  # type: ignore[arg-type]
            remaining_ids=40_000,
            provision_threshold=50_000,
            has_successor=False,
            closed_pending_archive=True,
            drop_eligible=False,
        )
        == OperatorAction.PROVISION
    )
    assert (
        recommend_action(
            capacity_state=CapacityState.STOP_REQUIRED,
            active=object(),  # type: ignore[arg-type]
            remaining_ids=5_000,
            provision_threshold=50_000,
            has_successor=False,
            closed_pending_archive=True,
            drop_eligible=False,
        )
        == OperatorAction.PROVISION_LATE
    )
    assert (
        recommend_action(
            capacity_state=CapacityState.READY,
            active=object(),  # type: ignore[arg-type]
            remaining_ids=500,
            provision_threshold=50_000,
            has_successor=False,
            closed_pending_archive=False,
            drop_eligible=False,
        )
        == OperatorAction.COVER_STOP_REQUIRED
    )


def test_recommend_action_priorities() -> None:
    assert (
        recommend_action(
            capacity_state=CapacityState.STOP_REQUIRED,
            active=None,
            remaining_ids=None,
            provision_threshold=50_000,
            has_successor=True,
            closed_pending_archive=False,
            drop_eligible=True,
        )
        == OperatorAction.STOP_REQUIRED
    )
    assert (
        recommend_action(
            capacity_state=CapacityState.READY,
            active=object(),  # type: ignore[arg-type]
            remaining_ids=40_000,
            provision_threshold=50_000,
            has_successor=False,
            closed_pending_archive=False,
            drop_eligible=False,
        )
        == OperatorAction.PROVISION
    )
    assert (
        recommend_action(
            capacity_state=CapacityState.ARCHIVE_PRESSURE,
            active=None,
            remaining_ids=None,
            provision_threshold=50_000,
            has_successor=True,
            closed_pending_archive=True,
            drop_eligible=False,
        )
        == OperatorAction.ARCHIVE
    )
    assert (
        recommend_action(
            capacity_state=CapacityState.READY,
            active=None,
            remaining_ids=None,
            provision_threshold=50_000,
            has_successor=True,
            closed_pending_archive=False,
            drop_eligible=True,
        )
        == OperatorAction.DROP_APPROVAL_REQUIRED
    )
    assert (
        recommend_action(
            capacity_state=CapacityState.DROP_APPROVAL_REQUIRED,
            active=None,
            remaining_ids=None,
            provision_threshold=50_000,
            has_successor=True,
            closed_pending_archive=False,
            drop_eligible=True,
        )
        == OperatorAction.DROP_APPROVAL_REQUIRED
    )


def test_capacity_stop_pauses_collect_but_hold_blocks_resume() -> None:
    assert collector_must_pause_for_capacity(CapacityState.STOP_REQUIRED) is True
    assert collector_must_pause_for_capacity(CapacityState.READY) is False
    target = normal_ready_target_bytes()
    assert target == (
        OPERATOR_EMERGENCY_FLOOR_BYTES
        + MEASURED_GENERATION_BYTES
        + WAL_AND_TRANSIENT_BUFFER_BYTES
    )
    assert (
        collector_may_resume_after_capacity(
            state=CapacityState.READY,
            free_disk_bytes=target,
            collect_hold=False,
        )
        is True
    )
    assert (
        collector_may_resume_after_capacity(
            state=CapacityState.READY,
            free_disk_bytes=target,
            collect_hold=True,
        )
        is False
    )
    # Just above 5 GiB is not READY margin.
    assert (
        collector_may_resume_after_capacity(
            state=CapacityState.READY,
            free_disk_bytes=OPERATOR_EMERGENCY_FLOOR_BYTES + 1,
            collect_hold=False,
        )
        is False
    )


def test_drop_backlog_blocks_third_archive_without_auto_drop() -> None:
    assert MAX_DROP_ELIGIBLE_GENERATIONS == 2
    assert drop_backlog_blocks_new_archive(0) is False
    assert drop_backlog_blocks_new_archive(1) is False
    assert drop_backlog_blocks_new_archive(2) is True
    assert drop_backlog_blocks_new_archive(3) is True
    # After a human DROP lowers the queue, archive may start again.
    assert drop_backlog_blocks_new_archive(1) is False
    with pytest.raises(ValueError, match="drop_eligible_count"):
        drop_backlog_blocks_new_archive(-1)
    script = Path("scripts/hibachi_emergency_archive_one.sh").read_text(encoding="utf-8")
    gate_at = script.find("DROP_BACKLOG_LIMIT")
    mutate_at = script.find("SET state='ARCHIVING'")
    drop_at = script.find("drop_verified_market_event_generation")
    skip_drop_at = script.find("skip_physical_drop")
    assert 0 < gate_at < mutate_at < skip_drop_at < drop_at
    # Auto tick never passes --drop; DROP remains a separate explicit flag.
    tick = Path("scripts/hibachi-auto-archive-tick.sh").read_text(encoding="utf-8")
    assert "--drop" not in tick
    assert "DROP_VERIFIED_GENERATION" not in tick


def test_emergency_archive_floor_keeps_remainder_above_3gib() -> None:
    floor = HARD_ARCHIVE_FLOOR_BYTES
    bundle = 64 * 1024 * 1024
    assert emergency_archive_allowed(floor) is False
    assert emergency_archive_allowed(floor + 1) is False
    assert emergency_archive_allowed(floor + 2 * bundle) is False
    assert emergency_archive_allowed(floor + 2 * bundle + 1) is True
    # Current production ~4.28 GiB must be allowed while COLLECT is paused.
    assert emergency_archive_allowed(int(4.28 * 1024**3)) is True


def test_emergency_archive_script_oldest_first_and_no_implicit_drop() -> None:
    script = Path("scripts/hibachi_emergency_archive_one.sh").read_text(encoding="utf-8")
    assert "ARCHIVING" in script
    assert "ORDER BY id_start LIMIT 1" in script
    assert "--drop) DO_DROP=1" in script
    assert "skip_physical_drop" in script
    assert "drop_verified_market_event_generation" in script
    assert "DO_DROP=0" in script
    assert "overlay-emergency" in script
    assert "hibachi_emergency_archive_window.py" in script
    assert "EMERGENCY_ARCHIVE_CAPACITY_BLOCKED" in script
    assert "DROP_BACKLOG_LIMIT" in script
    assert "MAX_DROP_ELIGIBLE=2" in script
    assert "skipped_new_archive" in script
    tick = Path("scripts/hibachi-auto-archive-tick.sh").read_text(encoding="utf-8")
    assert "--require-normal-floor" in tick
    assert "--drop" not in tick
    assert "collect_hold_present" in tick
    assert "archive_one_rc=" in tick
    assert "archive_status:" in tick
    loop = Path("scripts/hibachi_emergency_recovery_loop.sh").read_text(encoding="utf-8")
    assert "--drop" in loop
    helper = Path("scripts/hibachi_emergency_archive_window.py").read_text(encoding="utf-8")
    assert "reuse_completed" in helper
    assert "reuse_local_evidence" in helper
    assert "upload_builtin_verify" in helper
    assert "verify_restore_archive" in helper
    assert "b2_retry" in helper
    assert "drop_verified" not in helper
    maintain = Path("scripts/hibachi_generation_maintain.sh").read_text(encoding="utf-8")
    assert "CAPACITY_STOP_REQUIRED" in maintain
    assert "COLLECTOR_STOPPED_CAPACITY_STOP" in maintain
    assert "ARCHIVE_BACKLOG" in maintain
    assert "DROP_BACKLOG_LIMIT" in maintain
    assert "human_drop_approval" in maintain
    assert "PROVISION_LEAD=50000" in maintain
    overlay = Path("compose.collector-healthcheck.yaml").read_text(encoding="utf-8")
    assert "HIBACHI_HEALTHCHECK_OVERLAY" in overlay
    assert "healthcheck.py" in overlay
    assert "compose.collector-healthcheck.yaml" in maintain
    assert "HIBACHI_HEALTHCHECK_OVERLAY" in maintain
    assert "id=$(compose ps -q postgres)" in maintain
    assert "id=$(compose ps -q postgres))" not in maintain
    # Provision still happens before the disk-stop block.
    assert maintain.find("Provision BEFORE capacity STOP") < maintain.find(
        "CAPACITY_STOP_REQUIRED disk_below_floor"
    )
