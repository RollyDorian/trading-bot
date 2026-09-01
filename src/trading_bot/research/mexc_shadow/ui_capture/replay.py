"""Deterministic replay from append-only capture files."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from trading_bot.research.mexc_shadow.engine import run_shadow_replay
from trading_bot.research.mexc_shadow.profiles import load_profile
from trading_bot.research.mexc_shadow.source import MemorySource
from trading_bot.research.mexc_shadow.types import Observation, ReplayReport
from trading_bot.research.mexc_shadow.ui_capture.normalize import (
    observation_from_snapshot,
    snapshot_from_mapping,
)
from trading_bot.research.mexc_shadow.ui_capture.schema import NormalizedCapture
from trading_bot.research.mexc_shadow.ui_capture.store import iter_raw_mappings

SMOKE_NOTE = (
    "PIPELINE_SMOKE_ONLY: frozen placeholder profiles; not strategy results. "
    "Do not tune mom/gap/thresholds against this replay."
)


def iter_normalized_records(path: Path) -> Iterator[NormalizedCapture]:
    for payload in iter_raw_mappings(path):
        yield observation_from_snapshot(snapshot_from_mapping(payload))


def iter_replay_observations(path: Path) -> Iterator[Observation]:
    for record in iter_normalized_records(path):
        if record.observation is not None:
            yield record.observation


class CaptureNdjsonSource:
    """Read-only adapter: valid capture rows become MexcUiObserver-compatible prints."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def iter_observations(self) -> Iterator[Observation]:
        yield from iter_replay_observations(self._path)


def replay_capture_smoke(path: Path, profile_id: str = "author_observed_v0") -> ReplayReport:
    """Run a frozen profile as pipeline smoke. Not an edge evaluation."""

    config = load_profile(profile_id)
    report = run_shadow_replay(MemorySource(list(iter_replay_observations(path))), config)
    report.notes = (*report.notes, SMOKE_NOTE)
    return report
