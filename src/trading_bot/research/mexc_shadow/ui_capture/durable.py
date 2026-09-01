"""Local durable capture: bounded chunks, reconstructable NDJSON, fail-closed.

Python mirror of the extension IndexedDB contract. No network, no B2.
A service-worker restart is modeled by ``from_state`` / ``snapshot_state``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4

from trading_bot.research.mexc_shadow.safety import assert_no_credential_keys
from trading_bot.research.mexc_shadow.ui_capture.catalog import SCHEMA_NAME

SESSION_RECORD_SCHEMA = "mexc_ui_capture_session"
SESSION_RECORD_VERSION = 1
DEFAULT_CHUNK_SIZE = 250
RECORD_TYPE_START = "session_start"
RECORD_TYPE_END = "session_end"
SESSION_RECORD_TYPES = frozenset({RECORD_TYPE_START, RECORD_TYPE_END})


class DurableStorageError(Exception):
    """Commit failed. The active session must stop; do not drop the error."""


@dataclass
class SessionMeta:
    session_id: str
    started_at: str
    interval_ms: int
    page_host: str | None = None
    page_path: str | None = None
    ended_at: str | None = None
    status: str = "running"  # running | stopped | failed
    n_snapshots: int = 0
    n_chunks: int = 0
    first_sequence: int | None = None
    last_sequence: int | None = None
    chunk_size: int = DEFAULT_CHUNK_SIZE
    storage_error: str | None = None
    sequence_gaps: list[dict[str, Any]] = field(default_factory=list)
    client_sequence_mismatches: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_session_record(payload: dict[str, Any]) -> bool:
    record_type = str(payload.get("record_type") or "")
    if record_type in SESSION_RECORD_TYPES:
        return True
    schema = str(payload.get("schema") or "")
    return schema == SESSION_RECORD_SCHEMA and record_type != ""


def session_start_record(meta: SessionMeta) -> dict[str, Any]:
    return {
        "record_type": RECORD_TYPE_START,
        "schema": SESSION_RECORD_SCHEMA,
        "schema_version": SESSION_RECORD_VERSION,
        "session_id": meta.session_id,
        "started_at": meta.started_at,
        "interval_ms": meta.interval_ms,
        "page_host": meta.page_host,
        "page_path": meta.page_path,
        "chunk_size": meta.chunk_size,
        "status": meta.status,
    }


def session_end_record(meta: SessionMeta) -> dict[str, Any]:
    return {
        "record_type": RECORD_TYPE_END,
        "schema": SESSION_RECORD_SCHEMA,
        "schema_version": SESSION_RECORD_VERSION,
        "session_id": meta.session_id,
        "started_at": meta.started_at,
        "ended_at": meta.ended_at,
        "interval_ms": meta.interval_ms,
        "page_host": meta.page_host,
        "page_path": meta.page_path,
        "status": meta.status,
        "n_snapshots": meta.n_snapshots,
        "n_chunks": meta.n_chunks,
        "first_sequence": meta.first_sequence,
        "last_sequence": meta.last_sequence,
        "chunk_size": meta.chunk_size,
        "storage_error": meta.storage_error,
        "sequence_gaps": list(meta.sequence_gaps),
        "client_sequence_mismatches": list(meta.client_sequence_mismatches),
    }


def dumps_line(payload: dict[str, Any]) -> str:
    assert_no_credential_keys(payload)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def diagnose_sequence(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gaps / duplicates in the reconstructed snapshot stream (not session rows)."""

    gaps: list[dict[str, Any]] = []
    previous: int | None = None
    seen: set[int] = set()
    for index, payload in enumerate(snapshots):
        if str(payload.get("schema") or "") != SCHEMA_NAME:
            continue
        seq = int(payload["sequence"])
        if seq in seen:
            gaps.append({"kind": "duplicate_sequence", "sequence": seq, "index": index})
        seen.add(seq)
        if previous is not None and seq != previous + 1:
            gaps.append(
                {
                    "kind": "gap",
                    "expected": previous + 1,
                    "got": seq,
                    "index": index,
                }
            )
        previous = seq
    return gaps


class DurableCaptureStore:
    """Append-only chunked capture. Never silently truncates committed snapshots."""

    def __init__(self, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        self.chunk_size = chunk_size
        self.sessions: dict[str, SessionMeta] = {}
        # (session_id, chunk_index) -> committed snapshot dicts
        self.chunks: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.active_session_id: str | None = None
        self._fail_next: str | None = None

    def snapshot_state(self) -> dict[str, Any]:
        """Serialize committed state (service-worker restart equivalent)."""

        chunk_rows = [
            {
                "session_id": session_id,
                "chunk_index": chunk_index,
                "lines": list(lines),
            }
            for (session_id, chunk_index), lines in sorted(self.chunks.items())
        ]
        return {
            "chunk_size": self.chunk_size,
            "active_session_id": self.active_session_id,
            "sessions": {key: meta.as_dict() for key, meta in self.sessions.items()},
            "chunks": chunk_rows,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> DurableCaptureStore:
        store = cls(chunk_size=int(state.get("chunk_size") or DEFAULT_CHUNK_SIZE))
        store.active_session_id = state.get("active_session_id")
        for key, raw in dict(state.get("sessions") or {}).items():
            store.sessions[key] = SessionMeta(**raw)
        for row in state.get("chunks") or []:
            session_id = str(row["session_id"])
            chunk_index = int(row["chunk_index"])
            store.chunks[(session_id, chunk_index)] = [dict(item) for item in row["lines"]]
        return store

    def fail_next_append(self, message: str) -> None:
        self._fail_next = message

    def start_session(
        self,
        *,
        started_at: str,
        interval_ms: int,
        page_host: str | None = None,
        page_path: str | None = None,
        session_id: str | None = None,
    ) -> SessionMeta:
        if self.active_session_id:
            active = self.sessions[self.active_session_id]
            if active.status == "running":
                self.stop_session(ended_at=started_at, status="stopped")
        meta = SessionMeta(
            session_id=session_id or str(uuid4()),
            started_at=started_at,
            interval_ms=interval_ms,
            page_host=page_host,
            page_path=page_path,
            chunk_size=self.chunk_size,
        )
        self.sessions[meta.session_id] = meta
        self.active_session_id = meta.session_id
        return meta

    def _active(self) -> SessionMeta:
        if not self.active_session_id or self.active_session_id not in self.sessions:
            raise DurableStorageError("no active capture session")
        meta = self.sessions[self.active_session_id]
        if meta.status != "running":
            raise DurableStorageError(f"session {meta.session_id} is {meta.status}")
        if meta.storage_error:
            raise DurableStorageError(meta.storage_error)
        return meta

    def _fail_session(self, meta: SessionMeta, message: str, ended_at: str | None) -> None:
        meta.status = "failed"
        meta.storage_error = message
        meta.ended_at = ended_at or meta.ended_at
        self.active_session_id = None

    def append_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        received_at: str | None = None,
    ) -> dict[str, Any]:
        """Commit one snapshot. On storage error the session fails closed."""

        assert_no_credential_keys(snapshot)
        meta = self._active()
        if self._fail_next:
            message = self._fail_next
            self._fail_next = None
            self._fail_session(meta, message, received_at or str(snapshot.get("received_at_local")))
            raise DurableStorageError(message)

        assigned = (meta.last_sequence or 0) + 1
        client_seq_raw = snapshot.get("sequence")
        try:
            client_seq = int(client_seq_raw) if client_seq_raw is not None else None
        except (TypeError, ValueError):
            client_seq = None
        if client_seq not in (None, 0, assigned):
            meta.client_sequence_mismatches.append(
                {"expected": assigned, "got": client_seq, "assigned": assigned}
            )
        committed = dict(snapshot)
        committed["sequence"] = assigned
        committed["capture_id"] = meta.session_id
        if meta.page_host and not committed.get("page_host"):
            committed["page_host"] = meta.page_host
        if meta.page_path and not committed.get("page_path"):
            committed["page_path"] = meta.page_path

        chunk_index = meta.n_snapshots // meta.chunk_size
        key = (meta.session_id, chunk_index)
        chunk = self.chunks.setdefault(key, [])
        if len(chunk) >= meta.chunk_size:
            # Should not happen if n_snapshots and chunk_index stay aligned.
            self._fail_session(meta, "chunk overflow", received_at)
            raise DurableStorageError("chunk overflow")
        chunk.append(committed)
        meta.n_snapshots += 1
        meta.n_chunks = chunk_index + 1
        if meta.first_sequence is None:
            meta.first_sequence = assigned
        if meta.last_sequence is not None and assigned != meta.last_sequence + 1:
            meta.sequence_gaps.append(
                {"expected": meta.last_sequence + 1, "got": assigned}
            )
        meta.last_sequence = assigned
        return committed

    def stop_session(self, *, ended_at: str, status: str = "stopped") -> SessionMeta:
        meta = self._active()
        meta.ended_at = ended_at
        meta.status = status
        self.active_session_id = None
        return meta

    def export_snapshots(self, session_id: str) -> list[dict[str, Any]]:
        meta = self.sessions.get(session_id)
        if meta is None:
            raise DurableStorageError(f"unknown session {session_id}")
        rows: list[dict[str, Any]] = []
        for chunk_index in range(meta.n_chunks):
            rows.extend(self.chunks.get((session_id, chunk_index), []))
        return rows

    def export_lines(self, session_id: str) -> list[str]:
        """Exact ordered NDJSON-equivalent stream including session start/end."""

        meta = self.sessions.get(session_id)
        if meta is None:
            raise DurableStorageError(f"unknown session {session_id}")
        snapshots = self.export_snapshots(session_id)
        export_gaps = diagnose_sequence(snapshots)
        if export_gaps:
            meta.sequence_gaps.extend(export_gaps)
        lines = [dumps_line(session_start_record(meta))]
        for payload in snapshots:
            lines.append(dumps_line(payload))
        lines.append(dumps_line(session_end_record(meta)))
        return lines

    def export_ndjson(self, session_id: str) -> str:
        return "\n".join(self.export_lines(session_id)) + "\n"
