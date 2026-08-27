"""Isolated dual-connection Binance USD-M external reference collector runtime."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp

from trading_bot.external_market_data.binance_parser import (
    BinanceParseError,
    parse_binance_usdm_event,
)
from trading_bot.external_market_data.contract import (
    AGG_TRADE_WS_URL,
    BOOK_TICKER_WS_URL,
    CONTRACT_DOCS,
    CONTRACT_NAME,
    CONTRACT_VERIFIED_AT_UTC,
    INSTRUMENT,
    VENUE,
)
from trading_bot.external_market_data.envelope import EventType, utc_now
from trading_bot.external_market_data.metrics import ExternalMetrics
from trading_bot.external_market_data.offload.capacity import (
    BacklogAction,
    CapacityPolicy,
    measure_local_external_bytes,
)
from trading_bot.external_market_data.offload.status import collect_status
from trading_bot.external_market_data.offload.worker import AsyncOffloadWorker
from trading_bot.external_market_data.segmented_spool import SegmentedExternalSpool
from trading_bot.external_market_data.spool import ExternalCapacityStop, filesystem_free_bytes

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExternalRuntimeConfig:
    enabled: bool
    spool_dir: Path
    filesystem_floor_bytes: int
    floor_margin_bytes: int
    pressure_bytes: int
    stop_bytes: int
    segment_max_bytes: int
    segment_max_seconds: float
    canary_max_seconds: int
    status_path: Path
    offload_enabled: bool
    offload_store: str  # local | b2
    offload_local_root: Path
    book_ticker_url: str = BOOK_TICKER_WS_URL
    agg_trade_url: str = AGG_TRADE_WS_URL
    max_malformed_before_stop: int = 50
    # Legacy alias retained for compose compatibility; maps to stop_bytes.
    hard_cap_bytes: int = 192 * 1024 * 1024


def load_config_from_env() -> ExternalRuntimeConfig:
    enabled = os.environ.get("EXTERNAL_REF_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    spool = Path(
        os.environ.get("EXTERNAL_REF_SPOOL_DIR", "/app/external-spool")
    ).resolve()
    floor = int(os.environ.get("EXTERNAL_REF_FS_FLOOR_BYTES", str(5 * 1024**3)))
    margin = int(os.environ.get("EXTERNAL_REF_FS_FLOOR_MARGIN_BYTES", str(512 * 1024**2)))
    pressure = int(os.environ.get("EXTERNAL_REF_PRESSURE_BYTES", str(128 * 1024**2)))
    stop = int(os.environ.get("EXTERNAL_REF_STOP_BYTES", str(192 * 1024**2)))
    # Legacy hard-cap env still accepted as stop budget for segmented mode.
    legacy_cap = os.environ.get("EXTERNAL_REF_SPOOL_HARD_CAP_BYTES")
    if legacy_cap is not None:
        stop = max(stop, int(legacy_cap))
    segment_max = int(os.environ.get("EXTERNAL_REF_SEGMENT_MAX_BYTES", str(16 * 1024**2)))
    segment_secs = float(os.environ.get("EXTERNAL_REF_SEGMENT_MAX_SECONDS", "300"))
    canary = int(os.environ.get("EXTERNAL_REF_CANARY_MAX_SECONDS", "1800"))
    status = Path(
        os.environ.get(
            "EXTERNAL_REF_STATUS_PATH",
            str(spool / "external_ref_status.json"),
        )
    )
    offload_enabled = os.environ.get("EXTERNAL_REF_OFFLOAD_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    offload_store = os.environ.get("EXTERNAL_REF_OFFLOAD_STORE", "b2").strip().lower()
    offload_local = Path(
        os.environ.get("EXTERNAL_REF_OFFLOAD_LOCAL_ROOT", str(spool / ".local-store"))
    )
    return ExternalRuntimeConfig(
        enabled=enabled,
        spool_dir=spool,
        filesystem_floor_bytes=floor,
        floor_margin_bytes=margin,
        pressure_bytes=pressure,
        stop_bytes=stop,
        segment_max_bytes=segment_max,
        segment_max_seconds=segment_secs,
        canary_max_seconds=canary,
        status_path=status,
        offload_enabled=offload_enabled,
        offload_store=offload_store,
        offload_local_root=offload_local,
        hard_cap_bytes=stop,
    )


def process_rss_bytes() -> int | None:
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    return int(parts[1]) * 1024
    except OSError:
        return None
    return None


def _build_store(config: ExternalRuntimeConfig) -> Any:
    if config.offload_store == "local":
        from trading_bot.archive.store import LocalArchiveStore

        return LocalArchiveStore(config.offload_local_root)
    from trading_bot.archive.b2 import B2ArchiveConfig
    from trading_bot.archive.store import S3ArchiveStore

    return S3ArchiveStore.for_b2(B2ArchiveConfig.from_environ())


@dataclass(slots=True)
class RateTracker:
    """Bounded 1s/10s peak message and byte rates."""

    _msg_1s: deque[tuple[float, int]] = field(default_factory=deque)
    _bytes_1s: deque[tuple[float, int]] = field(default_factory=deque)
    peak_msg_per_sec_1s: float = 0.0
    peak_msg_per_sec_10s: float = 0.0
    peak_bytes_per_sec_1s: float = 0.0

    def note(self, *, messages: int = 1, raw_bytes: int = 0) -> None:
        now = time.monotonic()
        self._msg_1s.append((now, messages))
        self._bytes_1s.append((now, raw_bytes))
        self._trim(now)
        msg_1 = sum(n for t, n in self._msg_1s if now - t <= 1.0)
        msg_10 = sum(n for t, n in self._msg_1s if now - t <= 10.0)
        byt_1 = sum(n for t, n in self._bytes_1s if now - t <= 1.0)
        self.peak_msg_per_sec_1s = max(self.peak_msg_per_sec_1s, float(msg_1))
        self.peak_msg_per_sec_10s = max(self.peak_msg_per_sec_10s, msg_10 / 10.0)
        self.peak_bytes_per_sec_1s = max(self.peak_bytes_per_sec_1s, float(byt_1))

    def _trim(self, now: float) -> None:
        while self._msg_1s and now - self._msg_1s[0][0] > 10.0:
            self._msg_1s.popleft()
        while self._bytes_1s and now - self._bytes_1s[0][0] > 10.0:
            self._bytes_1s.popleft()


class ExternalRefCollector:
    """Owns only the external failure domain (two Binance USD-M WS sessions)."""

    def __init__(self, config: ExternalRuntimeConfig) -> None:
        self.config = config
        self.metrics = ExternalMetrics()
        self.policy = CapacityPolicy(
            pressure_bytes=config.pressure_bytes,
            stop_bytes=config.stop_bytes,
            global_floor_bytes=config.filesystem_floor_bytes,
            floor_margin_bytes=config.floor_margin_bytes,
            external_budget_bytes=config.stop_bytes,
        )
        self.spool = SegmentedExternalSpool(
            config.spool_dir,
            policy=self.policy,
            max_segment_bytes=config.segment_max_bytes,
            max_segment_seconds=config.segment_max_seconds,
        )
        self._stop = asyncio.Event()
        self._stop_reason: str | None = None
        self._first_book = False
        self._first_agg = False
        self._peak_rss: int | None = None
        self._min_free: int | None = None
        self._max_local_total = 0
        self._rate = RateTracker()
        self._backlog_samples: deque[tuple[float, int]] = deque(maxlen=120)
        self._offload_worker: AsyncOffloadWorker | None = None
        self._b2_health = "disabled"

    def request_stop(self, reason: str) -> None:
        if self._stop_reason is None:
            self._stop_reason = reason
            self.metrics.stop_reason = reason
        self._stop.set()

    async def run(self) -> int:
        if not self.config.enabled:
            logger.info("external_ref_disabled_exit")
            return 0

        free = filesystem_free_bytes(self.config.spool_dir)
        min_required = (
            self.config.filesystem_floor_bytes
            + self.config.floor_margin_bytes
            + self.config.segment_max_bytes
        )
        if free < min_required:
            self._write_status(
                {
                    "STATUS": "EXTERNAL_LIVE_OFFLOAD_BLOCKED",
                    "reason": "insufficient_prestart_free_for_floor_margin_segment",
                    "filesystem_free_bytes": free,
                    "floor_bytes": self.config.filesystem_floor_bytes,
                    "floor_margin_bytes": self.config.floor_margin_bytes,
                    "segment_max_bytes": self.config.segment_max_bytes,
                    "min_required_bytes": min_required,
                }
            )
            return 2

        # Recovery before accepting normal offload state.
        if self.config.offload_enabled:
            store = _build_store(self.config)
            self._offload_worker = AsyncOffloadWorker(
                self.config.spool_dir,
                store,
                stop_event=self._stop,
                auto_reclaim=True,
            )
            recovery = self._offload_worker.recover()
            logger.info("external_offload_recovery %s", recovery)
            self._b2_health = "healthy" if self.config.offload_store == "b2" else "local"

        self.spool.open()
        disk_before = filesystem_free_bytes(self.config.spool_dir)
        self._min_free = disk_before
        loop = asyncio.get_running_loop()

        def _stop_on_signal(signum: signal.Signals) -> None:
            self.request_stop(f"signal_{signum.name}")

        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _stop_on_signal, sig)

        ingest_tasks = [
            asyncio.create_task(
                self._session_loop(
                    url=self.config.book_ticker_url,
                    expected_event="book_ticker",
                    label="book_ticker",
                ),
                name="book_ticker_session",
            ),
            asyncio.create_task(
                self._session_loop(
                    url=self.config.agg_trade_url,
                    expected_event="agg_trade",
                    label="agg_trade",
                ),
                name="agg_trade_session",
            ),
            asyncio.create_task(self._watchdog(), name="watchdog"),
            asyncio.create_task(self._status_loop(), name="status_loop"),
        ]
        worker_task: asyncio.Task[Any] | None = None
        if self._offload_worker is not None:
            worker_task = asyncio.create_task(self._offload_worker.run(), name="offload_worker")

        exit_code = 0
        try:
            await self._stop.wait()
        except asyncio.CancelledError:
            self.request_stop("cancelled")
            raise
        finally:
            # 1-2: stop ingest first so no new events / no concurrent state writers.
            for task in ingest_tasks:
                task.cancel()
            await asyncio.gather(*ingest_tasks, return_exceptions=True)
            # 3: seal ACTIVE.
            sealed = self.spool.close()
            del sealed
            if self._offload_worker is not None:
                # 4-5: single-owner drain then join worker. Do not drain_remaining
                # in parallel with run() (that produced the 000006 FAILED race).
                with contextlib.suppress(Exception):
                    await self._offload_worker.shutdown_drain(deadline_seconds=35.0)
                if worker_task is not None:
                    worker_task.cancel()
                    await asyncio.gather(worker_task, return_exceptions=True)
                # 6: recover_root is idempotent; adopts remote-verified reclaim leftovers.
                with contextlib.suppress(Exception):
                    self._offload_worker.recover()
            disk_after = filesystem_free_bytes(self.config.spool_dir)
            local_total = measure_local_external_bytes(self.config.spool_dir)
            status = self.metrics.snapshot(
                spool_bytes=self.spool.stats.bytes_written,
                filesystem_free=disk_after,
            )
            offload_snap = (
                self._offload_worker.snapshot() if self._offload_worker is not None else {}
            )
            live_status = collect_status(
                self.config.spool_dir,
                external_mode="STOPPED",
                b2_health=self._b2_health,
                ingest_msg_per_sec=float(status.get("messages_per_sec") or 0.0),
                ingest_mib_per_hour=float(status.get("projected_mib_per_hour") or 0.0),
                offload_mib_per_hour=float(offload_snap.get("offload_mib_per_hour") or 0.0),
                backlog_trend=self._backlog_trend(),
                policy=self.policy,
            ).to_dict()
            status.update(
                {
                    "STATUS": self._status_code(),
                    "venue": VENUE,
                    "instrument": INSTRUMENT,
                    "contract_name": CONTRACT_NAME,
                    "contract_verified_at_utc": CONTRACT_VERIFIED_AT_UTC,
                    "contract_docs": CONTRACT_DOCS,
                    "stop_reason": self._stop_reason,
                    "disk_free_before_bytes": disk_before,
                    "disk_free_after_bytes": disk_after,
                    "disk_free_min_tracked_bytes": self._min_free,
                    "filesystem_floor_bytes": self.config.filesystem_floor_bytes,
                    "floor_margin_bytes": self.config.floor_margin_bytes,
                    "pressure_bytes": self.config.pressure_bytes,
                    "stop_bytes": self.config.stop_bytes,
                    "segment_max_bytes": self.config.segment_max_bytes,
                    "segments_sealed": self.spool.stats.segments_sealed,
                    "local_total_bytes": local_total,
                    "local_total_max_bytes": self._max_local_total,
                    "first_book_ticker": self._first_book,
                    "first_agg_trade": self._first_agg,
                    "peak_rss_bytes": self._peak_rss,
                    "peak_msg_per_sec_1s": self._rate.peak_msg_per_sec_1s,
                    "peak_msg_per_sec_10s_avg": self._rate.peak_msg_per_sec_10s,
                    "peak_bytes_per_sec_1s": self._rate.peak_bytes_per_sec_1s,
                    "backlog_trend": self._backlog_trend(),
                    "offload": offload_snap,
                    "operator_status": live_status,
                    "ended_at_utc": datetime.now(UTC).isoformat(),
                }
            )
            self._write_status(status)
            if self._stop_reason and self._stop_reason.startswith("EXTERNAL_CAPACITY"):
                exit_code = 3
            elif self._stop_reason and "malformed" in self._stop_reason:
                exit_code = 4
        return exit_code

    def _status_code(self) -> str:
        reason = self._stop_reason or ""
        if reason.startswith("EXTERNAL_CAPACITY"):
            return "EXTERNAL_CAPACITY_STOP"
        if reason.startswith("canary_max"):
            return "EXTERNAL_LIVE_OFFLOAD_PASS"
        if "malformed" in reason or "schema" in reason:
            return "EXTERNAL_LIVE_OFFLOAD_FAILED"
        if reason.startswith("signal_"):
            return "EXTERNAL_LIVE_OFFLOAD_PASS"
        if "blocked" in reason.lower():
            return "EXTERNAL_LIVE_OFFLOAD_BLOCKED"
        return "EXTERNAL_LIVE_OFFLOAD_FAILED"

    def _backlog_trend(self) -> str:
        if len(self._backlog_samples) < 5:
            return "unknown"
        oldest = self._backlog_samples[0][1]
        newest = self._backlog_samples[-1][1]
        delta = newest - oldest
        if abs(delta) < 1024 * 1024:
            return "stable"
        return "growing" if delta > 0 else "shrinking"

    def _write_status(self, payload: dict[str, Any]) -> None:
        self.config.status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config.status_path.with_name(
            f".{self.config.status_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        try:
            tmp.write_text(
                json.dumps(payload, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
            with tmp.open("ab") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.config.status_path)
        finally:
            tmp.unlink(missing_ok=True)

    async def _status_loop(self) -> None:
        while not self._stop.is_set():
            local = measure_local_external_bytes(self.config.spool_dir)
            self._max_local_total = max(self._max_local_total, local)
            self._backlog_samples.append((time.monotonic(), local))
            free = filesystem_free_bytes(self.config.spool_dir)
            self._min_free = min(self._min_free or free, free)
            offload_snap = (
                self._offload_worker.snapshot() if self._offload_worker is not None else {}
            )
            elapsed = self.metrics.elapsed_seconds()
            ingest_mib_h = (
                (self.spool.stats.bytes_written / elapsed) * 3600.0 / (1024.0 * 1024.0)
                if elapsed > 0
                else 0.0
            )
            payload = collect_status(
                self.config.spool_dir,
                external_mode="RUNNING",
                b2_health=self._b2_health,
                ingest_msg_per_sec=self.metrics.messages_total / max(elapsed, 1e-9),
                ingest_mib_per_hour=ingest_mib_h,
                offload_mib_per_hour=float(offload_snap.get("offload_mib_per_hour") or 0.0),
                backlog_trend=self._backlog_trend(),
                policy=self.policy,
            ).to_dict()
            payload.update(
                {
                    "STATUS": "RUNNING",
                    "peak_rss_bytes": self._peak_rss,
                    "peak_msg_per_sec_1s": self._rate.peak_msg_per_sec_1s,
                    "segments_sealed": self.spool.stats.segments_sealed,
                    "offload": offload_snap,
                    "updated_at_utc": datetime.now(UTC).isoformat(),
                }
            )
            self._write_status(payload)
            if payload.get("ACTION") == BacklogAction.EXTERNAL_STOP_REQUIRED.value:
                self.request_stop("EXTERNAL_CAPACITY_STOP:status_action")
                return
            await asyncio.sleep(5.0)

    async def _watchdog(self) -> None:
        started = time.monotonic()
        while not self._stop.is_set():
            elapsed = time.monotonic() - started
            if elapsed >= self.config.canary_max_seconds:
                self.request_stop("canary_max_seconds")
                return
            free = filesystem_free_bytes(self.config.spool_dir)
            self._min_free = min(self._min_free or free, free)
            rss = process_rss_bytes()
            if rss is not None:
                self._peak_rss = max(self._peak_rss or 0, rss)
            if free < self.config.filesystem_floor_bytes:
                self.request_stop("EXTERNAL_CAPACITY_STOP_filesystem_floor")
                return
            local = measure_local_external_bytes(self.config.spool_dir)
            self._max_local_total = max(self._max_local_total, local)
            action = self.policy.classify(
                local_total_bytes=local,
                filesystem_free_bytes=free,
            )
            if action == BacklogAction.EXTERNAL_STOP_REQUIRED:
                self.request_stop(f"EXTERNAL_CAPACITY_STOP:{action.value}")
                return
            await asyncio.sleep(1.0)

    async def _session_loop(
        self,
        *,
        url: str,
        expected_event: EventType,
        label: str,
    ) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            connection_id = str(uuid.uuid4())
            local_sequence = 0
            try:
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None)
                connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=300)
                async with (
                    aiohttp.ClientSession(timeout=timeout, connector=connector) as session,
                    session.ws_connect(
                        url,
                        heartbeat=None,
                        autoping=True,
                        # bookTicker/aggTrade are <2 KiB; 64 KiB bounds the WS frame buffer.
                        max_msg_size=64 * 1024,
                    ) as ws,
                ):
                    logger.info("external_ws_connected label=%s", label)
                    backoff = 1.0
                    async for msg in ws:
                        if self._stop.is_set():
                            break
                        received_at = utc_now()
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            raw = msg.data
                            raw_bytes = len(raw.encode("utf-8"))
                            try:
                                payload = json.loads(raw)
                                local_sequence += 1
                                envelope = parse_binance_usdm_event(
                                    payload,
                                    received_at=received_at,
                                    connection_id=connection_id,
                                    local_sequence=local_sequence,
                                    expected_event=expected_event,
                                )
                                self.spool.append(envelope)
                                self.metrics.note_message(
                                    event_type=envelope.event_type,
                                    raw_bytes=raw_bytes,
                                    received_at=envelope.received_at,
                                    exchange_at=envelope.exchange_at,
                                    connection_id=connection_id,
                                    local_sequence=local_sequence,
                                )
                                self._rate.note(messages=1, raw_bytes=raw_bytes)
                                if envelope.event_type == "book_ticker":
                                    self._first_book = True
                                else:
                                    self._first_agg = True
                            except ExternalCapacityStop as exc:
                                self.request_stop(f"EXTERNAL_CAPACITY_STOP:{exc}")
                                return
                            except (
                                BinanceParseError,
                                json.JSONDecodeError,
                                TypeError,
                                ValueError,
                            ) as exc:
                                self.metrics.malformed_count += 1
                                logger.warning(
                                    "external_malformed label=%s error=%s",
                                    label,
                                    exc,
                                )
                                if (
                                    self.metrics.malformed_count
                                    >= self.config.max_malformed_before_stop
                                ):
                                    self.request_stop("recurring_malformed_schema")
                                    return
                        elif msg.type in {
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            break
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            self.metrics.malformed_count += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network / protocol — reconnect bounded
                logger.warning(
                    "external_ws_error label=%s error=%s:%s",
                    label,
                    type(exc).__name__,
                    exc,
                )
            if self._stop.is_set():
                return
            self.metrics.reconnect_count += 1
            delay = min(backoff, 60.0)
            jitter = delay * (0.1 * (os.getpid() % 7) / 7.0)
            await asyncio.sleep(delay + jitter)
            backoff = min(backoff * 2.0, 60.0)
