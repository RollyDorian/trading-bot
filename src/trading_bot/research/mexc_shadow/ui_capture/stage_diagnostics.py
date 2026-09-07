"""Bounded MEXC UI capture stage diagnostics. No market prices, no private DOM.

Python contract mirrored by ``extensions/mexc_ui_capture/stage_diagnostics.js``.
Observation timestamps stay extract-time; expected timer deadlines are diagnostic
only and must never overwrite ``received_at_local`` / ``observed_at_local`` /
``monotonic_ms``.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

DIAGNOSTIC_FORMAT_VERSION = 1
EXTENSION_VERSION = "1.3.4"
DIAGNOSTIC_RECORD_SCHEMA = "mexc_ui_stage_diagnostics"
DIAGNOSTIC_RECORD_TYPES = frozenset({"stage_diagnostics_sidecar"})
ID_MAX_CHARS = 64
ENUM_MAX_CHARS = 32
INTERVAL_DETAIL_CAP = 2048
MUTATION_DETAIL_CAP = 512
MUTATION_DETAIL_FIRST = 128
MUTATION_DETAIL_EVERY = 16
SLOW_EXAMPLE_CAP = 256
LIFECYCLE_CAP = 128
SLOW_STAGE_MS = 500.0
CHECKPOINT_MIN_GAP_MS = 5000
HISTOGRAM_BOUNDS_MS: tuple[int, ...] = (
    1,
    2,
    4,
    8,
    16,
    32,
    64,
    128,
    250,
    500,
    1000,
    2000,
    4000,
    8000,
    16000,
    32000,
)
ALLOWED_TRIGGERS = frozenset({"interval", "mutation", "manual"})
ALLOWED_VISIBILITY = frozenset({"visible", "hidden", "prerender", "unloaded", "unknown"})
ALLOWED_ACK = frozenset({"ok", "fail", "timeout", "unknown", "abandoned"})
ALLOWED_LIFECYCLE = frozenset(
    {
        "visibilitychange",
        "freeze",
        "resume",
        "pagehide",
        "pageshow",
        "worker_boot",
        "timer_registered",
        "session_start",
        "session_stop",
    }
)

# Drop accidental private/account/order/HTML smuggling. Diagnostics are numeric/enum only.
_PRIVATE_KEY = re.compile(
    r"account|balance|wallet|position|\borders?\b|html|inner_?text|text_content|"
    r"raw_text|selector|cookie|credential|password|email|\buid\b|token|authorization",
    re.IGNORECASE,
)

ALLOWED_STAGE_KEYS = frozenset(
    {
        "diagnostic_format_version",
        "extension_version",
        "request_ordinal",
        "trigger",
        "producer_epoch",
        "session_generation",
        "session_generation_now",
        "session_id",
        "stale_generation",
        "session_id_mismatch",
        "active_session_id",
        "worker_boot_id",
        "interval_callback_ordinal",
        "mutation_callback_ordinal",
        "expected_deadline_mono",
        "elapsed_ideal_slot_ordinal",
        "callback_mono",
        "callback_delay_ms",
        "content_enqueue_mono",
        "content_queue_depth",
        "content_oldest_wait_ms",
        "content_queue_wait_ms",
        "extract_start_mono",
        "extract_end_mono",
        "extract_duration_ms",
        "send_start_mono",
        "ack_end_mono",
        "total_ack_latency_ms",
        "visibility_state",
        "visibility_state_at_extract",
        "background_receive_mono",
        "background_queue_depth",
        "background_queue_wait_ms",
        "append_start_mono",
        "append_end_mono",
        "append_duration_ms",
        "payload_bytes",
        "chunk_index",
        "chunk_occupancy",
        "ack_outcome",
        "append_timings",
    }
)
ALLOWED_APPEND_TIMING_KEYS = frozenset(
    {
        "meta_lookup_ms",
        "db_open_ms",
        "session_read_ms",
        "chunk_read_ms",
        "stringify_put_ms",
        "tx_wait_ms",
        "close_ms",
    }
)

TriggerName = Literal["interval", "mutation", "manual"]


def is_stage_diagnostic_record(payload: dict[str, Any]) -> bool:
    record_type = str(payload.get("record_type") or "")
    if record_type in DIAGNOSTIC_RECORD_TYPES:
        return True
    return str(payload.get("schema") or "") == DIAGNOSTIC_RECORD_SCHEMA


def clip_id(value: Any, *, limit: int = ID_MAX_CHARS) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def clip_enum(value: Any, allowed: frozenset[str], *, fallback: str = "unknown") -> str:
    text = clip_id(value, limit=ENUM_MAX_CHARS) or fallback
    return text if text in allowed else fallback


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    return None


def finite_int(value: Any) -> int | None:
    number = finite_number(value)
    if number is None:
        return None
    return int(number)


def expected_deadline_mono(
    timer_registered_mono: float,
    interval_callback_ordinal: int,
    interval_ms: int,
) -> float:
    """Ideal deadline ``t0 + n * interval``. Never derived from a late previous callback."""

    return float(timer_registered_mono) + int(interval_callback_ordinal) * float(interval_ms)


def elapsed_ideal_slot_ordinal(
    timer_registered_mono: float,
    callback_mono: float,
    interval_ms: int,
) -> int:
    if interval_ms <= 0:
        return 0
    return int((float(callback_mono) - float(timer_registered_mono)) // float(interval_ms))


def expected_interval_opportunities(
    timer_registered_mono: float | None,
    stop_mono: float | None,
    interval_ms: int,
) -> int:
    """Elapsed ideal ``setInterval`` slots from registration until Stop.

    ``setInterval`` does not fire immediately, so this is the count of deadlines
    that should have been delivered, not ``duration / interval`` from session start.
    """

    if interval_ms <= 0 or timer_registered_mono is None or stop_mono is None:
        return 0
    return max(0, int((float(stop_mono) - float(timer_registered_mono)) // float(interval_ms)))


def queue_wait_ms(start_mono: float | None, callback_mono: float | None) -> float | None:
    if start_mono is None or callback_mono is None:
        return None
    return float(start_mono) - float(callback_mono)


def duration_ms(end_mono: float | None, start_mono: float | None) -> float | None:
    if end_mono is None or start_mono is None:
        return None
    return float(end_mono) - float(start_mono)


def should_sample_mutation(mutation_callback_ordinal: int) -> bool:
    if mutation_callback_ordinal <= MUTATION_DETAIL_FIRST:
        return True
    return mutation_callback_ordinal % MUTATION_DETAIL_EVERY == 0


def empty_trigger_counts() -> dict[str, int]:
    return {"interval": 0, "mutation": 0, "manual": 0}


def empty_counters() -> dict[str, Any]:
    return {
        "expected_interval_opportunities": 0,
        "timer_callbacks": 0,
        "mutation_callbacks": 0,
        "manual_callbacks": 0,
        "callbacks_enqueued": empty_trigger_counts(),
        "tasks_started": empty_trigger_counts(),
        "extracted": empty_trigger_counts(),
        "sent": empty_trigger_counts(),
        "received": empty_trigger_counts(),
        "append_started": empty_trigger_counts(),
        "snapshots_persisted": empty_trigger_counts(),
        "acked": empty_trigger_counts(),
        "failed": empty_trigger_counts(),
        "abandoned": empty_trigger_counts(),
        "stale_generation_detected": 0,
        "session_id_mismatch_detected": 0,
        "producer_epochs_seen": 0,
        "outstanding_at_stop": 0,
        "diagnostic_truncated": False,
        "suppressed_interval_details": 0,
        "suppressed_mutation_details": 0,
        "suppressed_lifecycle": 0,
        "suppressed_slow_examples": 0,
        "suppressed_completions": 0,
    }


def empty_histogram() -> dict[str, Any]:
    return {
        "bounds_ms": list(HISTOGRAM_BOUNDS_MS),
        "counts": [0] * (len(HISTOGRAM_BOUNDS_MS) + 1),
        "min_ms": None,
        "max_ms": None,
        "sum_ms": 0.0,
        "n": 0,
    }


def observe_histogram(hist: dict[str, Any], value: float | None) -> None:
    number = finite_number(value)
    if number is None:
        return
    hist["n"] = int(hist["n"]) + 1
    hist["sum_ms"] = float(hist["sum_ms"]) + number
    hist["min_ms"] = number if hist["min_ms"] is None else min(float(hist["min_ms"]), number)
    hist["max_ms"] = number if hist["max_ms"] is None else max(float(hist["max_ms"]), number)
    bucket = len(HISTOGRAM_BOUNDS_MS)
    for index, bound in enumerate(HISTOGRAM_BOUNDS_MS):
        if number <= bound:
            bucket = index
            break
    counts = list(hist["counts"])
    counts[bucket] += 1
    hist["counts"] = counts


def _sanitize_append_timings(raw: Any) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, float] = {}
    for key, value in raw.items():
        name = str(key)
        if name not in ALLOWED_APPEND_TIMING_KEYS or _PRIVATE_KEY.search(name):
            continue
        number = finite_number(value)
        if number is None:
            continue
        out[name] = number
    return out or None


def sanitize_stage_diagnostics(raw: Any) -> dict[str, Any] | None:
    """Keep a bounded numeric/enum diagnostic object. Never keep HTML or account DOM."""

    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for key, value in raw.items():
        name = str(key)
        if name not in ALLOWED_STAGE_KEYS or _PRIVATE_KEY.search(name):
            continue
        if name in {
            "producer_epoch",
            "session_id",
            "active_session_id",
            "worker_boot_id",
            "extension_version",
        }:
            clipped = clip_id(value)
            if clipped is not None:
                out[name] = clipped
            continue
        if name == "trigger":
            out[name] = clip_enum(value, ALLOWED_TRIGGERS, fallback="manual")
            continue
        if name in {"visibility_state", "visibility_state_at_extract"}:
            out[name] = clip_enum(value, ALLOWED_VISIBILITY)
            continue
        if name == "ack_outcome":
            out[name] = clip_enum(value, ALLOWED_ACK)
            continue
        if name in {"stale_generation", "session_id_mismatch"}:
            out[name] = bool(value)
            continue
        if name == "append_timings":
            timings = _sanitize_append_timings(value)
            if timings:
                out[name] = timings
            continue
        if name == "diagnostic_format_version":
            version = finite_int(value)
            if version is not None:
                out[name] = version
            continue
        number = finite_number(value)
        if number is None:
            continue
        if name.endswith("_ordinal") or name in {
            "session_generation",
            "session_generation_now",
            "content_queue_depth",
            "background_queue_depth",
            "payload_bytes",
            "chunk_index",
            "chunk_occupancy",
            "request_ordinal",
        }:
            out[name] = int(number)
        else:
            out[name] = number
    if not out:
        return None
    out.setdefault("diagnostic_format_version", DIAGNOSTIC_FORMAT_VERSION)
    return out


def sanitize_lifecycle_event(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = clip_enum(raw.get("kind"), ALLOWED_LIFECYCLE, fallback="")
    if not kind:
        return None
    event = {
        "kind": kind,
        "mono": finite_number(raw.get("mono")),
        "from_state": clip_enum(raw.get("from_state") or raw.get("from"), ALLOWED_VISIBILITY)
        if raw.get("from_state") is not None or raw.get("from") is not None
        else None,
        "to_state": clip_enum(raw.get("to_state") or raw.get("to"), ALLOWED_VISIBILITY)
        if raw.get("to_state") is not None or raw.get("to") is not None
        else None,
        "producer_epoch": clip_id(raw.get("producer_epoch")),
        "session_generation": finite_int(raw.get("session_generation")),
        "worker_boot_id": clip_id(raw.get("worker_boot_id")),
    }
    return {key: value for key, value in event.items() if value is not None}


def observation_timestamps_are_extract_time(
    *,
    received_at_local: str,
    expected_deadline_mono: float | None,
    extract_end_mono: float | None,
    monotonic_ms: float | None,
) -> bool:
    """Market arrival stamps must match extract completion, not the timer deadline."""

    if (
        extract_end_mono is not None
        and monotonic_ms is not None
        and abs(float(monotonic_ms) - float(extract_end_mono)) > 1e-6
    ):
        return False
    if (
        expected_deadline_mono is not None
        and extract_end_mono is not None
        and monotonic_ms is not None
        and abs(float(monotonic_ms) - float(expected_deadline_mono)) < 1e-9
        and abs(float(extract_end_mono) - float(expected_deadline_mono)) > 1e-6
    ):
        return False
    return bool(received_at_local) and "deadline" not in received_at_local.lower()


@dataclass
class StageRequest:
    request_ordinal: int
    trigger: TriggerName
    session_generation: int
    producer_epoch: str
    callback_mono: float
    enqueue_mono: float
    content_queue_depth: int
    visibility_state: str = "visible"
    interval_callback_ordinal: int | None = None
    expected_deadline_mono: float | None = None
    callback_delay_ms: float | None = None
    elapsed_ideal_slot_ordinal: int | None = None
    mutation_callback_ordinal: int | None = None


@dataclass
class StageTrace:
    request: StageRequest
    abandoned: bool = False
    stale_generation: bool = False
    extract_start_mono: float | None = None
    extract_end_mono: float | None = None
    extract_duration_ms: float | None = None
    send_start_mono: float | None = None
    visibility_state_at_extract: str | None = None
    received_at_local: str | None = None
    monotonic_ms: float | None = None
    background_receive_mono: float | None = None
    background_queue_depth: int | None = None
    background_queue_wait_ms: float | None = None
    append_start_mono: float | None = None
    append_end_mono: float | None = None
    append_duration_ms: float | None = None
    ack_end_mono: float | None = None
    total_ack_latency_ms: float | None = None
    ack_outcome: str | None = None
    worker_boot_id: str | None = None
    persisted_sequence: int | None = None

    def content_queue_wait_ms(self) -> float | None:
        return queue_wait_ms(self.extract_start_mono, self.request.callback_mono)

    def as_stage_diagnostics(self) -> dict[str, Any]:
        req = self.request
        payload = {
            "diagnostic_format_version": DIAGNOSTIC_FORMAT_VERSION,
            "extension_version": EXTENSION_VERSION,
            "request_ordinal": req.request_ordinal,
            "trigger": req.trigger,
            "producer_epoch": req.producer_epoch,
            "session_generation": req.session_generation,
            "stale_generation": self.stale_generation,
            "interval_callback_ordinal": req.interval_callback_ordinal,
            "expected_deadline_mono": req.expected_deadline_mono,
            "elapsed_ideal_slot_ordinal": req.elapsed_ideal_slot_ordinal,
            "callback_mono": req.callback_mono,
            "callback_delay_ms": req.callback_delay_ms,
            "content_enqueue_mono": req.enqueue_mono,
            "content_queue_depth": req.content_queue_depth,
            "content_queue_wait_ms": self.content_queue_wait_ms(),
            "extract_start_mono": self.extract_start_mono,
            "extract_end_mono": self.extract_end_mono,
            "extract_duration_ms": self.extract_duration_ms,
            "send_start_mono": self.send_start_mono,
            "ack_end_mono": self.ack_end_mono,
            "total_ack_latency_ms": self.total_ack_latency_ms,
            "visibility_state": req.visibility_state,
            "visibility_state_at_extract": self.visibility_state_at_extract,
            "background_receive_mono": self.background_receive_mono,
            "background_queue_depth": self.background_queue_depth,
            "background_queue_wait_ms": self.background_queue_wait_ms,
            "append_start_mono": self.append_start_mono,
            "append_end_mono": self.append_end_mono,
            "append_duration_ms": self.append_duration_ms,
            "ack_outcome": self.ack_outcome,
            "worker_boot_id": self.worker_boot_id,
        }
        return sanitize_stage_diagnostics(payload) or {}


@dataclass
class BoundedSidecar:
    """In-memory diagnostic buffer. Drops details after caps; counters always update."""

    interval_details: list[dict[str, Any]] = field(default_factory=list)
    mutation_details: list[dict[str, Any]] = field(default_factory=list)
    slow_examples: list[dict[str, Any]] = field(default_factory=list)
    lifecycle: list[dict[str, Any]] = field(default_factory=list)
    completions: list[dict[str, Any]] = field(default_factory=list)
    histograms: dict[str, dict[str, Any]] = field(default_factory=dict)
    counters: dict[str, Any] = field(default_factory=empty_counters)
    producer_epochs: list[str] = field(default_factory=list)
    worker_boot_ids: list[str] = field(default_factory=list)
    content_queue_depth_high_water: int = 0
    background_queue_depth_high_water: int = 0
    content_oldest_wait_high_water_ms: float = 0.0

    def __post_init__(self) -> None:
        if not self.histograms:
            self.histograms = {
                "callback_delay_ms": empty_histogram(),
                "content_queue_wait_ms": empty_histogram(),
                "extract_duration_ms": empty_histogram(),
                "background_queue_wait_ms": empty_histogram(),
                "append_duration_ms": empty_histogram(),
                "total_ack_latency_ms": empty_histogram(),
            }

    def note_producer(self, epoch: str | None) -> None:
        clipped = clip_id(epoch)
        if clipped and clipped not in self.producer_epochs:
            self.producer_epochs.append(clipped)
            self.counters["producer_epochs_seen"] = len(self.producer_epochs)

    def note_worker_boot(self, boot_id: str | None) -> None:
        clipped = clip_id(boot_id)
        if clipped and clipped not in self.worker_boot_ids:
            self.worker_boot_ids.append(clipped)

    def record_lifecycle(self, event: dict[str, Any]) -> None:
        clean = sanitize_lifecycle_event(event)
        if clean is None:
            return
        if len(self.lifecycle) >= LIFECYCLE_CAP:
            self.counters["suppressed_lifecycle"] += 1
            self.counters["diagnostic_truncated"] = True
            return
        self.lifecycle.append(clean)

    def record_trace(self, trace: StageTrace) -> None:
        diag = trace.as_stage_diagnostics()
        trigger = trace.request.trigger
        self.note_producer(trace.request.producer_epoch)
        self.note_worker_boot(trace.worker_boot_id)
        self.content_queue_depth_high_water = max(
            self.content_queue_depth_high_water, trace.request.content_queue_depth
        )
        if trace.background_queue_depth is not None:
            self.background_queue_depth_high_water = max(
                self.background_queue_depth_high_water, trace.background_queue_depth
            )
        wait = trace.content_queue_wait_ms()
        if wait is not None:
            self.content_oldest_wait_high_water_ms = max(
                self.content_oldest_wait_high_water_ms, wait
            )
        observe_histogram(self.histograms["callback_delay_ms"], trace.request.callback_delay_ms)
        observe_histogram(self.histograms["content_queue_wait_ms"], wait)
        observe_histogram(self.histograms["extract_duration_ms"], trace.extract_duration_ms)
        observe_histogram(
            self.histograms["background_queue_wait_ms"], trace.background_queue_wait_ms
        )
        observe_histogram(self.histograms["append_duration_ms"], trace.append_duration_ms)
        observe_histogram(self.histograms["total_ack_latency_ms"], trace.total_ack_latency_ms)
        if trigger == "interval":
            if len(self.interval_details) < INTERVAL_DETAIL_CAP:
                self.interval_details.append(diag)
            else:
                self.counters["suppressed_interval_details"] += 1
                self.counters["diagnostic_truncated"] = True
        elif trigger == "mutation":
            ordinal = trace.request.mutation_callback_ordinal or trace.request.request_ordinal
            if should_sample_mutation(ordinal) and len(self.mutation_details) < MUTATION_DETAIL_CAP:
                self.mutation_details.append(diag)
            else:
                self.counters["suppressed_mutation_details"] += 1
                self.counters["diagnostic_truncated"] = True
        slow_ms = max(
            [
                value
                for value in (
                    trace.request.callback_delay_ms,
                    wait,
                    trace.extract_duration_ms,
                    trace.background_queue_wait_ms,
                    trace.append_duration_ms,
                    trace.total_ack_latency_ms,
                )
                if value is not None
            ]
            or [0.0]
        )
        if slow_ms >= SLOW_STAGE_MS:
            if len(self.slow_examples) < SLOW_EXAMPLE_CAP:
                self.slow_examples.append(diag)
            else:
                self.counters["suppressed_slow_examples"] += 1
                self.counters["diagnostic_truncated"] = True
        if trace.append_end_mono is not None or trace.ack_end_mono is not None:
            completion = {
                "request_ordinal": trace.request.request_ordinal,
                "trigger": trigger,
                "append_end_mono": trace.append_end_mono,
                "ack_end_mono": trace.ack_end_mono,
                "append_duration_ms": trace.append_duration_ms,
                "total_ack_latency_ms": trace.total_ack_latency_ms,
                "ack_outcome": trace.ack_outcome,
                "worker_boot_id": trace.worker_boot_id,
                "persisted_sequence": trace.persisted_sequence,
            }
            if len(self.completions) < INTERVAL_DETAIL_CAP + MUTATION_DETAIL_CAP:
                self.completions.append(completion)
            else:
                self.counters["suppressed_completions"] += 1
                self.counters["diagnostic_truncated"] = True

    def summary(self) -> dict[str, Any]:
        return {
            "schema": DIAGNOSTIC_RECORD_SCHEMA,
            "diagnostic_format_version": DIAGNOSTIC_FORMAT_VERSION,
            "extension_version": EXTENSION_VERSION,
            "counters": dict(self.counters),
            "histograms": self.histograms,
            "high_water": {
                "content_queue_depth": self.content_queue_depth_high_water,
                "background_queue_depth": self.background_queue_depth_high_water,
                "content_oldest_wait_ms": self.content_oldest_wait_high_water_ms,
            },
            "producer_epochs": list(self.producer_epochs),
            "worker_boot_ids": list(self.worker_boot_ids),
            "lifecycle": list(self.lifecycle),
            "interval_details": list(self.interval_details),
            "mutation_details": list(self.mutation_details),
            "slow_examples": list(self.slow_examples),
            "completions": list(self.completions),
        }


@dataclass
class CaptureStagePipeline:
    """Deterministic coupled content FIFO + background FIFO.

    Matches the live extension: one outstanding content send/ACK, serial
    IndexedDB append, Stop does not drain, stale generation is detected but
    still processed while ``capturing`` remains true.
    """

    interval_ms: int = 500
    extract_ms: float = 5.0
    append_ms: float = 10.0
    ipc_ms: float = 1.0
    producer_epoch: str = "producer-a"
    worker_boot_id: str = "worker-a"
    now: float = 0.0
    capturing: bool = False
    session_generation: int = 0
    session_id: str = "session-a"
    timer_registered_mono: float | None = None
    visibility_state: str = "visible"
    request_ordinal: int = 0
    interval_callback_ordinal: int = 0
    mutation_callback_ordinal: int = 0
    persisted_sequence: int = 0
    content_queue: deque[StageRequest] = field(default_factory=deque)
    traces: list[StageTrace] = field(default_factory=list)
    sidecar: BoundedSidecar = field(default_factory=BoundedSidecar)
    content_free_at: float = 0.0
    bg_free_at: float = 0.0
    _in_flight: list[StageTrace] = field(default_factory=list)

    def start_session(self, now: float, *, session_id: str | None = None) -> None:
        self.now = now
        self.session_generation += 1
        self.capturing = True
        self.session_id = session_id or self.session_id
        self.timer_registered_mono = now
        self.sidecar.record_lifecycle(
            {
                "kind": "session_start",
                "mono": now,
                "producer_epoch": self.producer_epoch,
                "session_generation": self.session_generation,
                "worker_boot_id": self.worker_boot_id,
            }
        )
        self.sidecar.record_lifecycle(
            {
                "kind": "timer_registered",
                "mono": now,
                "to_state": self.visibility_state,
                "producer_epoch": self.producer_epoch,
                "session_generation": self.session_generation,
            }
        )
        self._callback_and_enqueue("manual", now)

    def stop_session(self, now: float) -> None:
        self._advance_to(now)
        self.capturing = False
        outstanding = len(self.content_queue)
        self.sidecar.counters["outstanding_at_stop"] = outstanding
        self.sidecar.counters["expected_interval_opportunities"] = expected_interval_opportunities(
            self.timer_registered_mono, now, self.interval_ms
        )
        self.sidecar.record_lifecycle(
            {
                "kind": "session_stop",
                "mono": now,
                "producer_epoch": self.producer_epoch,
                "session_generation": self.session_generation,
            }
        )

    def set_visibility(self, now: float, state: str) -> None:
        self._advance_to(now)
        previous = self.visibility_state
        self.visibility_state = clip_enum(state, ALLOWED_VISIBILITY)
        self.sidecar.record_lifecycle(
            {
                "kind": "visibilitychange",
                "mono": now,
                "from_state": previous,
                "to_state": self.visibility_state,
                "producer_epoch": self.producer_epoch,
                "session_generation": self.session_generation,
            }
        )

    def interval_callback(self, now: float) -> StageRequest | None:
        self._advance_to(now)
        if not self.capturing or self.timer_registered_mono is None:
            return None
        self.interval_callback_ordinal += 1
        self.sidecar.counters["timer_callbacks"] += 1
        expected = expected_deadline_mono(
            self.timer_registered_mono, self.interval_callback_ordinal, self.interval_ms
        )
        delay = now - expected
        slot = elapsed_ideal_slot_ordinal(self.timer_registered_mono, now, self.interval_ms)
        return self._callback_and_enqueue(
            "interval",
            now,
            interval_callback_ordinal=self.interval_callback_ordinal,
            expected_deadline_mono=expected,
            callback_delay_ms=delay,
            elapsed_ideal_slot_ordinal=slot,
        )

    def mutation_callback(self, now: float) -> StageRequest | None:
        self._advance_to(now)
        if not self.capturing:
            return None
        self.mutation_callback_ordinal += 1
        self.sidecar.counters["mutation_callbacks"] += 1
        return self._callback_and_enqueue(
            "mutation", now, mutation_callback_ordinal=self.mutation_callback_ordinal
        )

    def settle(self, until: float) -> None:
        self._advance_to(until)

    def _callback_and_enqueue(
        self,
        trigger: TriggerName,
        now: float,
        **extra: Any,
    ) -> StageRequest:
        self.request_ordinal += 1
        depth = len(self.content_queue)
        request = StageRequest(
            request_ordinal=self.request_ordinal,
            trigger=trigger,
            session_generation=self.session_generation,
            producer_epoch=self.producer_epoch,
            callback_mono=now,
            enqueue_mono=now,
            content_queue_depth=depth,
            visibility_state=self.visibility_state,
            interval_callback_ordinal=extra.get("interval_callback_ordinal"),
            expected_deadline_mono=extra.get("expected_deadline_mono"),
            callback_delay_ms=extra.get("callback_delay_ms"),
            elapsed_ideal_slot_ordinal=extra.get("elapsed_ideal_slot_ordinal"),
            mutation_callback_ordinal=extra.get("mutation_callback_ordinal"),
        )
        if trigger == "manual":
            self.sidecar.counters["manual_callbacks"] += 1
        self.sidecar.counters["callbacks_enqueued"][trigger] += 1
        self.content_queue.append(request)
        self._drain(now)
        return request

    def _advance_to(self, now: float) -> None:
        self.now = now
        self._drain(now)

    def _complete_acks(self, now: float) -> None:
        still_inflight: list[StageTrace] = []
        for trace in self._in_flight:
            if trace.ack_end_mono is not None and trace.ack_end_mono <= now:
                self._finish_ack(trace)
            else:
                still_inflight.append(trace)
        self._in_flight = still_inflight

    def _drain(self, now: float) -> None:
        # Content FIFO starts the next task only after the previous ACK, matching emitChain.
        while True:
            self._complete_acks(now)
            if not self.content_queue:
                return
            request = self.content_queue[0]
            start_at = max(request.enqueue_mono, self.content_free_at)
            if start_at > now:
                return
            self.content_queue.popleft()
            self._start_request(request, start_at)

    def _start_request(self, request: StageRequest, start_at: float) -> None:
        stale = request.session_generation != self.session_generation
        if stale:
            self.sidecar.counters["stale_generation_detected"] += 1
        if not self.capturing:
            self.sidecar.counters["abandoned"][request.trigger] += 1
            self.content_free_at = start_at
            trace = StageTrace(request=request, abandoned=True, stale_generation=stale)
            self.traces.append(trace)
            self.sidecar.record_trace(trace)
            return
        self.sidecar.counters["tasks_started"][request.trigger] += 1
        extract_start = start_at
        extract_end = extract_start + self.extract_ms
        send_start = extract_end
        # Observation timestamps are extract completion, never the ideal deadline.
        received = datetime.fromtimestamp(extract_end / 1000.0, tz=UTC).isoformat()
        bg_receive = send_start + self.ipc_ms
        bg_wait_depth = sum(
            1
            for item in self._in_flight
            if item.append_end_mono and item.append_end_mono > bg_receive
        )
        append_start = max(bg_receive, self.bg_free_at)
        append_end = append_start + self.append_ms
        ack_end = append_end + self.ipc_ms
        self.content_free_at = ack_end
        self.bg_free_at = append_end
        self.sidecar.counters["extracted"][request.trigger] += 1
        self.sidecar.counters["sent"][request.trigger] += 1
        self.sidecar.counters["received"][request.trigger] += 1
        self.sidecar.counters["append_started"][request.trigger] += 1
        self.persisted_sequence += 1
        self.sidecar.counters["snapshots_persisted"][request.trigger] += 1
        trace = StageTrace(
            request=request,
            stale_generation=stale,
            extract_start_mono=extract_start,
            extract_end_mono=extract_end,
            extract_duration_ms=self.extract_ms,
            send_start_mono=send_start,
            visibility_state_at_extract=self.visibility_state,
            received_at_local=received,
            monotonic_ms=extract_end,
            background_receive_mono=bg_receive,
            background_queue_depth=bg_wait_depth,
            background_queue_wait_ms=append_start - bg_receive,
            append_start_mono=append_start,
            append_end_mono=append_end,
            append_duration_ms=self.append_ms,
            ack_end_mono=ack_end,
            total_ack_latency_ms=ack_end - send_start,
            ack_outcome="ok",
            worker_boot_id=self.worker_boot_id,
            persisted_sequence=self.persisted_sequence,
        )
        self._in_flight.append(trace)

    def _finish_ack(self, trace: StageTrace) -> None:
        self.sidecar.counters["acked"][trace.request.trigger] += 1
        self.traces.append(trace)
        self.sidecar.record_trace(trace)


def correlate_by_request_ordinal(traces: list[StageTrace]) -> dict[int, StageTrace]:
    return {trace.request.request_ordinal: trace for trace in traces}


def reconcile_counters(counters: dict[str, Any], *, clean_stop: bool) -> dict[str, bool]:
    """Check the architecture accounting identities on a clean Stop."""

    checks: dict[str, bool] = {}
    for trigger in ("interval", "mutation", "manual"):
        enqueued = int(counters["callbacks_enqueued"][trigger])
        started = int(counters["tasks_started"][trigger])
        abandoned = int(counters["abandoned"][trigger])
        extracted = int(counters["extracted"][trigger])
        sent = int(counters["sent"][trigger])
        acked = int(counters["acked"][trigger])
        failed = int(counters["failed"][trigger])
        persisted = int(counters["snapshots_persisted"][trigger])
        outstanding = int(counters.get("outstanding_at_stop") or 0) if trigger == "interval" else 0
        checks[f"{trigger}_started_abandoned"] = (
            started + abandoned == enqueued or not clean_stop
        )
        checks[f"{trigger}_extracted_le_started"] = extracted <= started
        checks[f"{trigger}_acked_plus_failed"] = acked + failed <= sent
        checks[f"{trigger}_persisted_vs_acked"] = persisted >= acked or not clean_stop
        if trigger == "interval":
            checks["interval_outstanding_nonnegative"] = outstanding >= 0
    return checks


MILESTONE_STATUS = "MEXC_UI_CAPTURE_STAGE_DIAGNOSTICS_READY"
MILESTONE_DECISION = "STOP_FOR_LEAD_REVIEW"


def milestone_report() -> dict[str, Any]:
    return {
        "milestone": "MEXC_UI_CAPTURE_STAGE_DIAGNOSTICS_V1",
        "status": MILESTONE_STATUS,
        "decision": MILESTONE_DECISION,
        "ml_status": "NOT_STARTED",
        "paper": False,
        "live": False,
        "strategy_tuning": False,
        "mom_gap_inspected": False,
        "protocol_version": "2.0.0",
        "protocol_changed": False,
        "catalog_version": "v1.2",
        "extension_previous": "1.3.3",
        "extension_version": EXTENSION_VERSION,
        "diagnostic_format_version": DIAGNOSTIC_FORMAT_VERSION,
        "scheduler_changed": False,
        "mutation_coalescing_changed": False,
        "persistence_batching_changed": False,
        "priority_changed": False,
        "observation_timestamps": "extract_completion_not_timer_deadline",
        "live_diagnostic_capture": "NOT_RUN",
        "path_instrumented": [
            "timer_delivery",
            "content_queue",
            "dom_extraction",
            "send_ipc",
            "background_queue",
            "indexeddb_append",
            "ack",
        ],
        "interval_callback_fields": [
            "interval_callback_ordinal",
            "expected_deadline_mono",
            "callback_mono",
            "callback_delay_ms",
            "content_enqueue_mono",
            "content_queue_wait_ms",
            "extract_start_mono",
            "extract_end_mono",
            "extract_duration_ms",
            "send_start_mono",
            "ack_end_mono",
            "total_ack_latency_ms",
        ],
        "background_fields": [
            "request_ordinal",
            "session_id",
            "producer_epoch",
            "session_generation",
            "background_receive_mono",
            "background_queue_wait_ms",
            "append_start_mono",
            "append_end_mono",
            "append_duration_ms",
            "worker_boot_id",
        ],
        "counters": [
            "expected_interval_opportunities",
            "timer_callbacks",
            "callbacks_enqueued",
            "tasks_started",
            "snapshots_persisted",
            "acked",
            "abandoned",
        ],
        "bounds": {
            "interval_detail_cap": INTERVAL_DETAIL_CAP,
            "mutation_detail_cap": MUTATION_DETAIL_CAP,
            "mutation_detail_first": MUTATION_DETAIL_FIRST,
            "mutation_detail_every": MUTATION_DETAIL_EVERY,
            "slow_example_cap": SLOW_EXAMPLE_CAP,
            "lifecycle_cap": LIFECYCLE_CAP,
            "id_max_chars": ID_MAX_CHARS,
            "enum_max_chars": ENUM_MAX_CHARS,
            "checkpoint_min_gap_ms": CHECKPOINT_MIN_GAP_MS,
            "histogram_bounds_ms": list(HISTOGRAM_BOUNDS_MS),
        },
        "fencing": "detect_only_stale_generation_and_session_id_mismatch",
        "private_dom": "stripped_from_diagnostics",
        "next_operator_experiment": (
            "12-minute visibility sequence from cadence architecture review §4.4 "
            "after lead review; do not start the 8-12h corpus or retune mom/gap"
        ),
    }


def render_milestone_markdown(report: dict[str, Any]) -> str:
    bounds = report["bounds"]
    path = " → ".join(report["path_instrumented"])
    interval_fields = ", ".join(f"`{name}`" for name in report["interval_callback_fields"])
    background_fields = ", ".join(f"`{name}`" for name in report["background_fields"])
    counters = ", ".join(f"`{name}`" for name in report["counters"])
    return f"""# MEXC UI capture stage diagnostics v1

MILESTONE: `{report["milestone"]}`

STATUS: `{report["status"]}`

DECISION: `{report["decision"]}`

ML_STATUS: `{report["ml_status"]}`

PAPER: **{str(report["paper"]).lower()}**

LIVE: **{str(report["live"]).lower()}**

STRATEGY_TUNING: **{str(report["strategy_tuning"]).lower()}**

MOM/GAP: **not inspected**

Protocol v2.0.0: **unchanged**

Catalog: `{report["catalog_version"]}` (unchanged)

Extension: `{report["extension_previous"]}` → `{report["extension_version"]}`

Scheduler / mutation coalescing / persistence batching / priority: **unchanged**

Live diagnostic capture: **{report["live_diagnostic_capture"]}**

## Purpose

Instrument the existing COLLECT-only MEXC UI capture path so a later short
Chrome run can separate timer delivery from queue wait, extraction, IPC,
IndexedDB append, and ACK. This milestone does not redesign the FIFO, does
not coalesce mutations, does not batch persistence, and does not start ML,
PAPER, LIVE, or mom/gap retune.

## Instrumented path

`{path}`

Interval callbacks record: {interval_fields}.

Background records, correlated by `request_ordinal` / `session_id` /
`producer_epoch` / `session_generation`: {background_fields}.

Cumulative counters: {counters}.

`document.visibilityState` is stamped at callback and extraction. Visibility
and page-lifecycle transitions are retained up to {bounds["lifecycle_cap"]}
events. Producer epoch plus session generation are stamped so multiple
producers or stale queued work can be **detected**. Stale work is not
rejected in this milestone (detect-only fencing).

## Observation timestamps

`received_at_local`, `observed_at_local`, and `monotonic_ms` remain extract
completion on the live DOM. Expected timer deadlines are diagnostic fields
only. Never backdate market observations to `t0 + n * interval`.

Append completion is not written into the same IndexedDB transaction after
that transaction finishes. `append_end_mono` travels on the ACK and in the
session-end summary / completion ring. Missing completion evidence is
`unknown`, not a zero duration.

## Bounds and privacy

- interval details cap: {bounds["interval_detail_cap"]}
- mutation samples: first {bounds["mutation_detail_first"]}, then every
  {bounds["mutation_detail_every"]}, cap {bounds["mutation_detail_cap"]}
- slow-stage examples: {bounds["slow_example_cap"]}
- lifecycle events: {bounds["lifecycle_cap"]}
- IDs ≤ {bounds["id_max_chars"]} chars; enums ≤ {bounds["enum_max_chars"]}
- checkpoint gap ≥ {bounds["checkpoint_min_gap_ms"]} ms, fire-and-forget on
  an already-awake worker (no extra 5 s timer that keeps the service worker
  alive)
- no HTML, account, order, or private DOM in diagnostic objects
- counters and histograms still update after detail caps
  (`diagnostic_truncated` plus suppressed counts)

## What this does not do

No live 12-minute visibility experiment was run here. No 8–12 h corpus. No
protocol v2 threshold change. Frozen profiles are untouched. Hibachi COLLECT
is untouched.

## Lead-review next step

{report["next_operator_experiment"]}.
"""


def write_reports(*, out_json: Path, out_md: Path) -> dict[str, Any]:
    report = milestone_report()
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    out_md.write_text(render_milestone_markdown(report), encoding="utf-8")
    return report


def summarize_capture_stage_diagnostics(path: Path) -> dict[str, Any]:
    """Read optional stage diagnostics from an NDJSON export. No mom/gap."""

    from trading_bot.research.mexc_shadow.ui_capture.durable import is_session_record
    from trading_bot.research.mexc_shadow.ui_capture.store import iter_all_mappings

    n_snapshots = 0
    n_with_diag = 0
    ordinals: list[int] = []
    vis_states: dict[str, int] = {}
    summary: dict[str, Any] | None = None
    for payload in iter_all_mappings(path):
        if is_session_record(payload):
            if payload.get("record_type") == "session_end":
                raw_summary = payload.get("stage_diagnostic_summary")
                if isinstance(raw_summary, dict):
                    summary = raw_summary
            continue
        if is_stage_diagnostic_record(payload):
            continue
        n_snapshots += 1
        diag = sanitize_stage_diagnostics(payload.get("stage_diagnostics"))
        if not diag:
            continue
        n_with_diag += 1
        ordinal = finite_int(diag.get("request_ordinal"))
        if ordinal is not None:
            ordinals.append(ordinal)
        vis = str(diag.get("visibility_state") or "unknown")
        vis_states[vis] = vis_states.get(vis, 0) + 1
    return {
        "n_snapshots": n_snapshots,
        "n_with_stage_diagnostics": n_with_diag,
        "unique_request_ordinals": len(set(ordinals)),
        "visibility_states": vis_states,
        "has_session_summary": summary is not None,
        "session_summary_counters": None if summary is None else summary.get("counters"),
        "status": "PRESENT" if n_with_diag or summary else "NO_STAGE_DIAGNOSTICS",
    }
