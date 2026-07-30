from dataclasses import dataclass
from math import floor

MIB = 1024**2
GIB = 1024**3
MIN_RAW_HOT_DAYS = 3
DEGRADED_RAW_HOT_DAYS = 2


@dataclass(frozen=True, slots=True)
class CapacityInputs:
    disk_free_bytes: int
    emergency_reserve_bytes: int = 3 * GIB
    raw_mib_per_day: float = 0.0
    normalized_mib_per_day: float = 0.0
    parquet_mib_per_day: float = 0.0
    wal_mib_per_day: float = 0.0
    measured_days: float = 0.0
    requested_raw_hot_days: int = MIN_RAW_HOT_DAYS
    requested_normalized_hot_days: int = 0
    allow_degraded_two_day: bool = False


@dataclass(frozen=True, slots=True)
class CapacityPlan:
    state: str
    raw_hot_days: int
    normalized_hot_days: int
    hot_window_bytes: int
    emergency_reserve_bytes: int
    days_until_pause: float | None
    days_until_hard_stop: float | None
    confidence: str


def plan_capacity(inputs: CapacityInputs) -> CapacityPlan:
    values = (
        inputs.raw_mib_per_day,
        inputs.normalized_mib_per_day,
        inputs.parquet_mib_per_day,
        inputs.wal_mib_per_day,
        inputs.measured_days,
    )
    if inputs.disk_free_bytes < 0 or any(value < 0 for value in values):
        raise ValueError("capacity inputs must be non-negative")
    degraded_requested = (
        inputs.requested_raw_hot_days == DEGRADED_RAW_HOT_DAYS
        and inputs.allow_degraded_two_day
    )
    if (
        inputs.requested_raw_hot_days < MIN_RAW_HOT_DAYS
        and not degraded_requested
    ) or inputs.requested_normalized_hot_days < 0:
        raise ValueError("hot-window days are invalid")
    raw_daily = inputs.raw_mib_per_day * MIB
    normalized_daily = inputs.normalized_mib_per_day * MIB
    wal_daily = inputs.wal_mib_per_day * MIB
    hot_bytes = int(
        raw_daily * inputs.requested_raw_hot_days
        + normalized_daily * inputs.requested_normalized_hot_days
    )
    usable = max(0, inputs.disk_free_bytes - inputs.emergency_reserve_bytes)
    growth = raw_daily + normalized_daily + wal_daily
    hard_days = usable / growth if growth > 0 else None
    pause_reserve = inputs.emergency_reserve_bytes + GIB
    pause_days = max(0, inputs.disk_free_bytes - pause_reserve) / growth if growth > 0 else None
    confidence = "measured" if inputs.measured_days >= 3 else "extrapolated"
    affordable_raw_days = floor(usable / raw_daily) if raw_daily > 0 else 0
    raw_hot = (
        inputs.requested_raw_hot_days
        if affordable_raw_days >= inputs.requested_raw_hot_days
        else 0
    )
    state = "safe"
    if (
        hot_bytes > usable
        or raw_hot == 0
        or inputs.disk_free_bytes <= pause_reserve
    ):
        state = "blocked"
    elif degraded_requested or (hard_days is not None and hard_days <= 7):
        state = "warning"
    return CapacityPlan(
        state=state,
        raw_hot_days=max(0, raw_hot),
        normalized_hot_days=inputs.requested_normalized_hot_days,
        hot_window_bytes=hot_bytes,
        emergency_reserve_bytes=inputs.emergency_reserve_bytes,
        days_until_pause=pause_days,
        days_until_hard_stop=hard_days,
        confidence=confidence,
    )
