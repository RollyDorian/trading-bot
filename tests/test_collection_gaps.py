"""Production collection-gap registry must stay fail-closed and non-synthesizing."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading_bot.research.collection_gaps import (
    DEFAULT_GAPS_PATH,
    load_collection_gaps,
    raw_id_in_known_gap,
    reconcile_closed_generation_persisted_ids,
    timestamp_in_known_outage,
)


def test_capacity_recovery_pause_gap_is_registered() -> None:
    gaps = load_collection_gaps()
    pause = next(
        g
        for g in gaps
        if g.gap_id == "capacity_recovery_collection_pause_20260814"
    )
    assert pause.kind == "COLLECTION_OUTAGE"
    assert pause.classification == "CAPACITY_RECOVERY_COLLECTION_PAUSE"
    assert pause.synthesize is False
    assert pause.bridge_normalization is False
    assert pause.id_start_inclusive is None
    assert pause.id_end_inclusive is None
    # Sequence is contiguous; only wall-clock time is missing.
    assert raw_id_in_known_gap(11_902_151, gaps) is None
    assert raw_id_in_known_gap(11_902_152, gaps) is None
    last_before = datetime(2026, 8, 14, 8, 42, 22, 972216, tzinfo=UTC)
    first_after = datetime(2026, 8, 17, 17, 18, 16, 226260, tzinfo=UTC)
    assert timestamp_in_known_outage(last_before, gaps) is None
    assert timestamp_in_known_outage(first_after, gaps) is None
    assert (
        timestamp_in_known_outage(
            datetime(2026, 8, 15, 12, 0, tzinfo=UTC), gaps
        )
        is pause
    )


def test_partition_incident_gap_is_registered() -> None:
    gaps = load_collection_gaps()
    incident = next(
        g
        for g in gaps
        if g.gap_id == "partition_miss_20260812_g_9071913_lead"
    )
    assert incident.classification == "ALLOCATED_NOT_PERSISTED_DURING_PARTITION_FAILURE"
    assert incident.id_start_inclusive == 9_071_913
    assert incident.id_end_inclusive == 9_072_163
    assert incident.id_end_inclusive - incident.id_start_inclusive + 1 == 251
    assert incident.synthesize is False
    assert incident.bridge_normalization is False
    assert raw_id_in_known_gap(9_071_913, gaps) is incident
    assert raw_id_in_known_gap(9_072_163, gaps) is incident
    assert raw_id_in_known_gap(9_071_912, gaps) is None
    assert raw_id_in_known_gap(9_072_164, gaps) is None
    assert (
        timestamp_in_known_outage(
            datetime(2026, 8, 12, 8, 0, tzinfo=UTC), gaps
        )
        is incident
    )
    assert (
        timestamp_in_known_outage(
            datetime(2026, 8, 12, 9, 14, 40, 860289, tzinfo=UTC), gaps
        )
        is None
    )


def test_g_9071913_incident_reconciliation_accepts_399749() -> None:
    reconcile_closed_generation_persisted_ids(
        generation_key="g_9071913_9471913",
        id_start=9_071_913,
        id_end=9_471_913,
        persisted_count=399_749,
        min_persisted_id=9_072_164,
        max_persisted_id=9_471_912,
    )


def test_g_9071913_incident_reconciliation_rejects_fabricated_400k() -> None:
    with pytest.raises(ValueError, match="399749"):
        reconcile_closed_generation_persisted_ids(
            generation_key="g_9071913_9471913",
            id_start=9_071_913,
            id_end=9_471_913,
            persisted_count=400_000,
            min_persisted_id=9_071_913,
            max_persisted_id=9_471_912,
        )


def test_normal_generation_requires_full_contiguous_span() -> None:
    reconcile_closed_generation_persisted_ids(
        generation_key="g_7871913_8271913",
        id_start=7_871_913,
        id_end=8_271_913,
        persisted_count=400_000,
        min_persisted_id=7_871_913,
        max_persisted_id=8_271_912,
    )
    with pytest.raises(ValueError, match="expected 400000"):
        reconcile_closed_generation_persisted_ids(
            generation_key="g_7871913_8271913",
            id_start=7_871_913,
            id_end=8_271_913,
            persisted_count=399_999,
            min_persisted_id=7_871_913,
            max_persisted_id=8_271_911,
        )


def test_closed_capacity_stop_pause_gap_is_registered() -> None:
    gaps = load_collection_gaps()
    pause = next(
        g
        for g in gaps
        if g.gap_id == "capacity_stop_collection_pause_20260818"
    )
    assert pause.kind == "COLLECTION_OUTAGE"
    assert pause.classification == "CAPACITY_STOP_COLLECTION_PAUSE"
    assert pause.synthesize is False
    assert pause.bridge_normalization is False
    assert pause.end_utc == datetime(2026, 8, 19, 18, 4, 20, 698871, tzinfo=UTC)
    assert pause.id_start_inclusive is None
    last_before = datetime(2026, 8, 18, 23, 10, 2, 783378, tzinfo=UTC)
    first_after = datetime(2026, 8, 19, 18, 4, 20, 698871, tzinfo=UTC)
    # Exclusive bounds: last persisted before and first persisted after are not in the hole.
    assert timestamp_in_known_outage(last_before, gaps) is None
    assert timestamp_in_known_outage(first_after, gaps) is None
    assert (
        timestamp_in_known_outage(
            datetime(2026, 8, 19, 12, 0, tzinfo=UTC), gaps
        )
        is pause
    )
    # Time-only hole: sequence 13682172 then 13682173 are persisted; do not treat as missing.
    assert raw_id_in_known_gap(13_682_172, gaps) is None
    assert raw_id_in_known_gap(13_682_173, gaps) is None


def test_registry_accepts_open_ended_outage(tmp_path: Path) -> None:
    path = tmp_path / "gaps.json"
    path.write_text(
        """
        {"gaps": [{
          "gap_id": "open",
          "kind": "COLLECTION_OUTAGE",
          "classification": "CAPACITY_STOP_COLLECTION_PAUSE",
          "start_utc": "2026-08-18T23:10:02.783379Z",
          "end_utc": null,
          "synthesize": false,
          "bridge_normalization": false
        }]}
        """,
        encoding="utf-8",
    )
    loaded = load_collection_gaps(path)
    assert loaded[0].end_utc is None
    assert timestamp_in_known_outage(
        datetime(2026, 8, 19, 1, 0, tzinfo=UTC), loaded
    ) is loaded[0]


def test_default_registry_path_is_committed() -> None:
    assert DEFAULT_GAPS_PATH.is_file()
    assert DEFAULT_GAPS_PATH.name == "hibachi_collection_gaps_v1.json"


def test_registry_rejects_synthesize_true(tmp_path: Path) -> None:
    bad = tmp_path / "gaps.json"
    bad.write_text(
        """
        {"gaps": [{
          "gap_id": "x",
          "kind": "COLLECTION_OUTAGE",
          "classification": "COLLECTION_OUTAGE",
          "start_utc": "2026-08-12T00:00:00Z",
          "end_utc": "2026-08-12T01:00:00Z",
          "synthesize": true,
          "bridge_normalization": false
        }]}
        """,
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="forbid synthesize/bridge"):
        load_collection_gaps(bad)
