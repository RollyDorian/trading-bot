from __future__ import annotations

from pathlib import Path

import pytest

from trading_bot.research.mexc_shadow.ui_capture import mom_gap_protocol_v2_execution as subject


def _row(ordinal: int, *, mid: float = 100.0, segment: int = 0) -> subject.GridRow:
    return subject.GridRow(
        ordinal=ordinal,
        segment=segment,
        t_ms=ordinal * 500,
        session_id="session",
        bid=mid - 0.01,
        ask=mid + 0.01,
        last=mid,
        mark=100.0,
        index=100.0,
    )


def _feature(ordinal: int, mom: float, gap: float) -> subject.FeatureRow:
    return subject.FeatureRow(row=_row(ordinal), mom=mom, gap=gap)


def test_registered_order_is_exactly_21_cells() -> None:
    ids = [
        f"{family.id}__H{lookback}S"
        for family in subject.FAMILIES
        for lookback in subject.LOOKBACKS_S
    ]
    assert len(ids) == 21
    assert ids[:3] == [
        "F01_MID_RET_MID_MARK__H1S",
        "F01_MID_RET_MID_MARK__H2S",
        "F01_MID_RET_MID_MARK__H5S",
    ]
    assert ids[-1] == "F07_MID_SMA_MID_MARK__H5S"


def test_formula_orientation_and_quadrants_are_not_flipped() -> None:
    rows = [_row(0, mid=100.0), _row(1, mid=100.1), _row(2, mid=100.2)]
    features = subject._feature_rows(rows, subject.FAMILIES[0], 1)
    assert features[-1].mom == pytest.approx(subject.bps(100.2, 100.0))
    assert features[-1].gap == pytest.approx(subject.bps(100.2, 100.0))
    counts = subject.quadrant_counts(
        [
            _feature(0, 1, 1),
            _feature(1, 1, -1),
            _feature(2, -1, 1),
            _feature(3, -1, -1),
            _feature(4, 0, 1),
        ]
    )
    assert counts == {
        "mom_pos_gap_pos": 1,
        "mom_pos_gap_neg": 1,
        "mom_neg_gap_pos": 1,
        "mom_neg_gap_neg": 1,
        "zero_mom_or_gap": 1,
        "unavailable": 0,
    }


def test_episode_rearm_is_strictly_greater_than_2000ms() -> None:
    features = [_feature(0, 4.0, -2.0)]
    features.extend(_feature(index, 0.0, 0.0) for index in range(1, 6))
    features.append(_feature(6, 4.0, -2.0))
    episodes = subject.construct_episodes(features)
    assert [episode.row.ordinal for episode in episodes] == [0, 6]

    exactly_2000 = [_feature(0, 4.0, -2.0)]
    exactly_2000.extend(_feature(index, 0.0, 0.0) for index in range(1, 5))
    exactly_2000.append(_feature(5, 4.0, -2.0))
    assert [episode.row.ordinal for episode in subject.construct_episodes(exactly_2000)] == [0]


def test_non_overlap_keeps_representative_at_exactly_ten_seconds() -> None:
    episodes = [
        subject.Episode(_row(0), 4.0, -2.0, "long"),
        subject.Episode(_row(19), 4.0, -2.0, "long"),
        subject.Episode(_row(20), 4.0, -2.0, "long"),
    ]
    assert [episode.row.ordinal for episode in subject.non_overlapping(episodes)] == [0, 20]


def test_wilson_99_is_deterministic() -> None:
    interval = subject.wilson_99(80, 100)
    assert interval["estimate"] == 0.8
    assert interval["lower"] == pytest.approx(0.679826467385)
    assert interval["upper"] == pytest.approx(0.882841119986)


def test_f06_requires_executable_mid_gate() -> None:
    passing = {
        horizon: {
            "wilson_99": {
                "follower_positive": {"lower": 0.8},
                "gap_closure_positive": {"lower": 0.8},
                "momentum_continuation_positive": {"lower": 0.8},
                "momentum_reversal_negative": {"lower": 0.0},
                "executable_mid_positive": {"lower": 0.8 if horizon == 2 else 0.49},
            },
            "median_follower_share_on_closure": 0.9,
        }
        for horizon in (2, 5)
    }
    assert subject.classify_mechanism(subject.FAMILIES[5], passing) == "NON_EXECUTABLE_PRINT_ONLY"


def test_protocol_conclusion_uses_frozen_order() -> None:
    one_plausible = [
        {"final_cell_status": "PLAUSIBLE_B", "family_id": "F01"},
        {"final_cell_status": "MECHANISM_UNRESOLVED", "family_id": "F02"},
    ]
    conclusion, trace = subject.protocol_conclusion(one_plausible)
    assert conclusion == "PROVISIONAL_UNIQUE_FREQUENCY_UNAVAILABLE"
    assert trace[3]["matched"] is True
    assert trace[4]["evaluated"] is False


def test_lock_mismatch_stops_before_grid_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = tmp_path / "corpus"
    grid = tmp_path / "grid"
    manifest = tmp_path / "manifest.json"
    corpus.write_bytes(b"wrong")
    grid.write_bytes(b"wrong")
    manifest.write_text("{}", encoding="utf-8")
    called = False

    def fail_if_called(path: Path) -> list[subject.GridRow]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(subject, "load_grid", fail_if_called)
    with pytest.raises(subject.LockMismatchError, match="corpus SHA mismatch"):
        subject.build_result(
            repo=tmp_path,
            corpus=corpus,
            grid=grid,
            manifest=manifest,
            code_commit_sha="deadbeef",
        )
    assert called is False
