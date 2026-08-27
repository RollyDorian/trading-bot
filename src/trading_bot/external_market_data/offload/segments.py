"""External RAW segment identity and append-safe NDJSON staging."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class SegmentState(StrEnum):
    ACTIVE = "ACTIVE"
    SEALED_UNVERIFIED = "SEALED_UNVERIFIED"
    UPLOADING = "UPLOADING"
    VERIFIED_REMOTE = "VERIFIED_REMOTE"
    RECLAIMABLE = "RECLAIMABLE"
    FAILED = "FAILED"


SEGMENT_SCHEMA_VERSION = 1
_SEGMENT_ID_RE = re.compile(
    r"^binance_usdm_ETHUSDT_(?P<start>\d{8}T\d{6}Z)_(?P<seq>\d{6})$"
)
_STATE_LOCKS_GUARD = threading.Lock()
_STATE_LOCKS: dict[str, threading.Lock] = {}


def _lock_for_segment(segment_id: str) -> threading.Lock:
    with _STATE_LOCKS_GUARD:
        lock = _STATE_LOCKS.get(segment_id)
        if lock is None:
            lock = threading.Lock()
            _STATE_LOCKS[segment_id] = lock
        return lock


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory fsync; Windows may reject O_RDONLY on dirs."""

    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        return
    finally:
        os.close(dir_fd)


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_segment_id(*, start_utc: datetime, sequence: int) -> str:
    stamp = start_utc.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"binance_usdm_ETHUSDT_{stamp}_{sequence:06d}"


@dataclass(frozen=True, slots=True)
class SegmentPaths:
    root: Path
    segment_id: str

    @property
    def dir(self) -> Path:
        return self.root / self.segment_id

    @property
    def active_ndjson(self) -> Path:
        return self.dir / "events.active.ndjson"

    @property
    def sealed_ndjson(self) -> Path:
        return self.dir / "events.ndjson"

    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    @property
    def manifest_path(self) -> Path:
        return self.dir / "manifest.json"

    @property
    def gzip_path(self) -> Path:
        return self.dir / "events.ndjson.gz"

    @property
    def parquet_path(self) -> Path:
        return self.dir / "events.parquet"


@dataclass(slots=True)
class SegmentStateRecord:
    segment_id: str
    state: SegmentState
    created_at_utc: str
    updated_at_utc: str
    connection_ids: list[str]
    event_count: int = 0
    raw_bytes: int = 0
    first_local_sequence: int | None = None
    last_local_sequence: int | None = None
    received_at_min: str | None = None
    received_at_max: str | None = None
    exchange_at_min: str | None = None
    exchange_at_max: str | None = None
    content_sha256: str | None = None
    remote_key: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["state"] = str(self.state)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SegmentStateRecord:
        return cls(
            segment_id=str(payload["segment_id"]),
            state=SegmentState(str(payload["state"])),
            created_at_utc=str(payload["created_at_utc"]),
            updated_at_utc=str(payload["updated_at_utc"]),
            connection_ids=list(payload.get("connection_ids") or []),
            event_count=int(payload.get("event_count") or 0),
            raw_bytes=int(payload.get("raw_bytes") or 0),
            first_local_sequence=payload.get("first_local_sequence"),
            last_local_sequence=payload.get("last_local_sequence"),
            received_at_min=payload.get("received_at_min"),
            received_at_max=payload.get("received_at_max"),
            exchange_at_min=payload.get("exchange_at_min"),
            exchange_at_max=payload.get("exchange_at_max"),
            content_sha256=payload.get("content_sha256"),
            remote_key=payload.get("remote_key"),
            error=payload.get("error"),
        )


def write_state(paths: SegmentPaths, record: SegmentStateRecord) -> None:
    """Atomically persist segment state.json.

    Uses a unique temp name (never a shared ``state.tmp``) so concurrent
    seal/offload/reconnect writers cannot unlink each other's temp file.
    Per-segment lock serializes replace. Observers only see complete JSON.
    """

    paths.dir.mkdir(parents=True, exist_ok=True)
    record.updated_at_utc = utc_now().isoformat()
    payload = json.dumps(record.to_dict(), indent=2, sort_keys=True)
    tmp = paths.dir / f".state.{os.getpid()}.{time.time_ns()}.tmp"
    with _lock_for_segment(paths.segment_id):
        last_error: Exception | None = None
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            for _ in range(8):
                try:
                    os.replace(tmp, paths.state_path)
                    last_error = None
                    break
                except OSError as exc:
                    last_error = exc
                    time.sleep(0.02)
            if last_error is not None:
                raise last_error
            _fsync_directory(paths.dir)
        finally:
            tmp.unlink(missing_ok=True)


def read_state(paths: SegmentPaths) -> SegmentStateRecord | None:
    if not paths.state_path.exists():
        return None
    return SegmentStateRecord.from_dict(
        json.loads(paths.state_path.read_text(encoding="utf-8"))
    )


def recover_trailing_partial_ndjson(path: Path) -> dict[str, Any]:
    """Truncate only an incomplete final line without reading the whole file.

    Lines are hundreds of bytes; a 1 MiB tail window always contains a newline
    on a well-formed 16 MiB segment. Avoids a 16 MiB RAM spike during seal.
    """

    if not path.exists():
        return {"bytes_before": 0, "bytes_after": 0, "truncated_partial": False}
    before = path.stat().st_size
    if before == 0:
        return {"bytes_before": 0, "bytes_after": 0, "truncated_partial": False}
    with path.open("rb") as handle:
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) == b"\n":
            return {"bytes_before": before, "bytes_after": before, "truncated_partial": False}
        window = min(before, 1024 * 1024)
        handle.seek(before - window)
        chunk = handle.read(window)
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        # Entire file (or last 1 MiB) is one partial line.
        if before <= window:
            path.write_bytes(b"")
            return {"bytes_before": before, "bytes_after": 0, "truncated_partial": True}
        keep = before - window
    else:
        keep = before - window + last_nl + 1
    os.truncate(path, keep)
    return {"bytes_before": before, "bytes_after": keep, "truncated_partial": True}


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def iter_ndjson_records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def seal_active_segment(paths: SegmentPaths) -> SegmentStateRecord:
    """ACTIVE → SEALED_UNVERIFIED: recover partial, rename to immutable events.ndjson."""

    record = read_state(paths)
    if record is None:
        raise FileNotFoundError(f"missing state for {paths.segment_id}")
    if record.state != SegmentState.ACTIVE:
        raise RuntimeError(f"cannot seal non-ACTIVE segment: {record.state}")
    if not paths.active_ndjson.exists():
        raise FileNotFoundError(f"missing active file: {paths.active_ndjson}")
    recover_trailing_partial_ndjson(paths.active_ndjson)
    if paths.sealed_ndjson.exists():
        raise RuntimeError(f"sealed file already exists: {paths.sealed_ndjson}")
    os.replace(paths.active_ndjson, paths.sealed_ndjson)
    # Refresh counts from sealed content.
    counts = summarize_ndjson(paths.sealed_ndjson)
    record.event_count = counts["event_count"]
    record.raw_bytes = counts["raw_bytes"]
    record.connection_ids = counts["connection_ids"]
    record.first_local_sequence = counts["first_local_sequence"]
    record.last_local_sequence = counts["last_local_sequence"]
    record.received_at_min = counts["received_at_min"]
    record.received_at_max = counts["received_at_max"]
    record.exchange_at_min = counts["exchange_at_min"]
    record.exchange_at_max = counts["exchange_at_max"]
    record.content_sha256 = sha256_file(paths.sealed_ndjson)
    record.state = SegmentState.SEALED_UNVERIFIED
    write_state(paths, record)
    return record


def summarize_ndjson(path: Path) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    connection_ids: set[str] = set()
    first_seq: int | None = None
    last_seq: int | None = None
    recv_min: str | None = None
    recv_max: str | None = None
    exch_min: str | None = None
    exch_max: str | None = None
    count = 0
    for row in iter_ndjson_records(path):
        count += 1
        event_type = str(row.get("event_type") or "unknown")
        by_type[event_type] = by_type.get(event_type, 0) + 1
        cid = row.get("connection_id")
        if cid is not None:
            connection_ids.add(str(cid))
        seq = row.get("local_sequence")
        if isinstance(seq, int):
            first_seq = seq if first_seq is None else min(first_seq, seq)
            last_seq = seq if last_seq is None else max(last_seq, seq)
        recv = row.get("received_at")
        if isinstance(recv, str):
            recv_min = recv if recv_min is None else min(recv_min, recv)
            recv_max = recv if recv_max is None else max(recv_max, recv)
        exch = row.get("exchange_at")
        if isinstance(exch, str):
            exch_min = exch if exch_min is None else min(exch_min, exch)
            exch_max = exch if exch_max is None else max(exch_max, exch)
    return {
        "event_count": count,
        "raw_bytes": path.stat().st_size,
        "by_type": by_type,
        "connection_ids": sorted(connection_ids),
        "first_local_sequence": first_seq,
        "last_local_sequence": last_seq,
        "received_at_min": recv_min,
        "received_at_max": recv_max,
        "exchange_at_min": exch_min,
        "exchange_at_max": exch_max,
        "content_sha256": sha256_file(path),
    }


class ActiveSegmentWriter:
    """Cheap append-only writer for the hot receive path."""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = 16 * 1024 * 1024,
        max_seconds: float = 300.0,
    ) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.max_seconds = max_seconds
        self.root.mkdir(parents=True, exist_ok=True)
        self._sequence = self._next_sequence()
        self._paths: SegmentPaths | None = None
        self._record: SegmentStateRecord | None = None
        self._fh: Any = None
        self._opened_monotonic: float = 0.0
        self._bytes = 0

    @property
    def has_active(self) -> bool:
        return self._fh is not None

    def _next_sequence(self) -> int:
        existing = [p.name for p in self.root.iterdir() if p.is_dir()]
        max_seq = 0
        for name in existing:
            match = _SEGMENT_ID_RE.match(name)
            if match:
                max_seq = max(max_seq, int(match.group("seq")))
        return max_seq + 1

    def _open_new(self) -> None:
        import time

        start = utc_now()
        segment_id = format_segment_id(start_utc=start, sequence=self._sequence)
        self._sequence += 1
        paths = SegmentPaths(self.root, segment_id)
        paths.dir.mkdir(parents=True, exist_ok=False)
        record = SegmentStateRecord(
            segment_id=segment_id,
            state=SegmentState.ACTIVE,
            created_at_utc=start.isoformat(),
            updated_at_utc=start.isoformat(),
            connection_ids=[],
        )
        write_state(paths, record)
        self._paths = paths
        self._record = record
        self._fh = paths.active_ndjson.open("ab")
        self._opened_monotonic = time.monotonic()
        self._bytes = 0

    def append_line(self, line: bytes, *, connection_id: str | None = None) -> SegmentPaths | None:
        """Append one complete NDJSON line (must end with \\n). May return sealed paths."""

        import time

        if not line.endswith(b"\n"):
            raise ValueError("NDJSON line must end with newline")
        if self._fh is None:
            self._open_new()
        assert self._paths is not None and self._record is not None and self._fh is not None
        if connection_id and connection_id not in self._record.connection_ids:
            self._record.connection_ids.append(connection_id)
        self._fh.write(line)
        self._fh.flush()
        self._bytes += len(line)
        self._record.raw_bytes = self._bytes
        self._record.event_count += 1
        sealed: SegmentPaths | None = None
        age = time.monotonic() - self._opened_monotonic
        if self._bytes >= self.max_bytes or age >= self.max_seconds:
            sealed = self.seal()
        return sealed

    def seal(self) -> SegmentPaths:
        if self._fh is None or self._paths is None or self._record is None:
            raise RuntimeError("no ACTIVE segment to seal")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self._fh = None
        write_state(self._paths, self._record)
        paths = self._paths
        seal_active_segment(paths)
        self._paths = None
        self._record = None
        self._bytes = 0
        return paths

    def close(self) -> SegmentPaths | None:
        if self._fh is None:
            return None
        return self.seal()
