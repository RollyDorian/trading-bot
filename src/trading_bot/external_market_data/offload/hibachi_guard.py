"""Hibachi liveness classifier for the isolated external-ref collector.

Docker HEALTH is not the sole definition of Hibachi data-path death. A
single probe timeout while inserts continue is ``HIBACHI_HEALTH_TRANSIENT``.
Hard fail-closed remains for process death, PostgreSQL death, partition
uncoverage, capacity STOP, and stale event progression.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class GuardVerdict(StrEnum):
    CONTINUE = "CONTINUE"
    HIBACHI_HEALTH_TRANSIENT = "HIBACHI_HEALTH_TRANSIENT"
    STOP = "STOP"


class StopReason(StrEnum):
    PROCESS_DEAD = "HIBACHI_PROCESS_DEAD"
    POSTGRES_DEAD = "HIBACHI_POSTGRES_UNHEALTHY"
    PARTITION_UNCOVERED = "HIBACHI_PARTITION_UNCOVERED"
    CAPACITY_STOP = "HIBACHI_CAPACITY_STOP"
    DATA_STALE = "HIBACHI_DATA_PROGRESS_STALE"
    DOCKER_UNHEALTHY_SUSTAINED = "HIBACHI_DOCKER_UNHEALTHY_SUSTAINED"


# Watch samples every 30s; Docker probe interval is 30s with 10s timeout.
# One unhealthy sample (Docker already retried internally) + live ids = transient.
# Two consecutive unhealthy samples (~60s) = sustained, stop external.
DOCKER_UNHEALTHY_STOP_STREAK = 2
# Two samples with no id advance at ~1000 events/min is a dead data path.
DATA_STALE_STOP_SAMPLES = 2


@dataclass(frozen=True, slots=True)
class HibachiGuardSnapshot:
    """One observation of Hibachi production signals."""

    process_live: bool
    postgres_live: bool
    partition_covered: bool
    capacity_safe: bool
    docker_health: str
    docker_unhealthy_streak: int
    data_progress: bool
    stale_progress_samples: int


def classify_hibachi_guard(snapshot: HibachiGuardSnapshot) -> tuple[GuardVerdict, str | None]:
    """Return (verdict, stop_reason). stop_reason is set only for STOP."""

    if not snapshot.process_live:
        return GuardVerdict.STOP, StopReason.PROCESS_DEAD.value
    if not snapshot.postgres_live:
        return GuardVerdict.STOP, StopReason.POSTGRES_DEAD.value
    if not snapshot.partition_covered:
        return GuardVerdict.STOP, StopReason.PARTITION_UNCOVERED.value
    if not snapshot.capacity_safe:
        return GuardVerdict.STOP, StopReason.CAPACITY_STOP.value
    if snapshot.stale_progress_samples >= DATA_STALE_STOP_SAMPLES:
        return GuardVerdict.STOP, StopReason.DATA_STALE.value

    docker = (snapshot.docker_health or "").lower()
    docker_bad = docker in {"unhealthy", "timeout"}
    if docker_bad and snapshot.docker_unhealthy_streak >= DOCKER_UNHEALTHY_STOP_STREAK:
        return GuardVerdict.STOP, StopReason.DOCKER_UNHEALTHY_SUSTAINED.value
    if docker_bad:
        # Isolated probe timeout/unhealthy while process, PG, coverage, and ids live.
        return GuardVerdict.HIBACHI_HEALTH_TRANSIENT, None
    return GuardVerdict.CONTINUE, None
