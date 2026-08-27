"""Lifecycle: seal → compress → upload → verify → reclaim."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from trading_bot.external_market_data.offload.compress import (
    gunzip_ndjson,
    gzip_ndjson,
    prove_round_trip,
)
from trading_bot.external_market_data.offload.manifest import build_manifest, write_manifest
from trading_bot.external_market_data.offload.segments import (
    SegmentPaths,
    SegmentState,
    SegmentStateRecord,
    iter_ndjson_records,
    read_state,
    recover_trailing_partial_ndjson,
    sha256_file,
    summarize_ndjson,
    write_state,
)

logger = logging.getLogger(__name__)

EXTERNAL_B2_PREFIX = "external/binance_usdm/ETHUSDT"


class ObjectStore(Protocol):
    def exists(self, key: str) -> bool: ...

    def publish_file(self, key: str, source: Path) -> None: ...

    def publish_bytes(self, key: str, value: bytes) -> None: ...

    def download_file(self, key: str, destination: Path) -> None: ...

    def read_bytes(self, key: str) -> bytes: ...


@dataclass(slots=True)
class OffloadResult:
    segment_id: str
    state: SegmentState
    remote_data_key: str | None
    remote_manifest_key: str | None
    elapsed_seconds: float
    error: str | None = None


def remote_data_key(segment_id: str) -> str:
    return f"{EXTERNAL_B2_PREFIX}/{segment_id}/events.ndjson.gz"


def remote_manifest_key(segment_id: str) -> str:
    return f"{EXTERNAL_B2_PREFIX}/{segment_id}/manifest.json"


def remote_evidence_key(segment_id: str) -> str:
    return f"{EXTERNAL_B2_PREFIX}/{segment_id}/VERIFY_OK.json"


def _bulky_payload_present(paths: SegmentPaths) -> bool:
    return any(
        path.exists()
        for path in (paths.sealed_ndjson, paths.gzip_path, paths.active_ndjson)
    )


def adopt_verified_reclaimed(paths: SegmentPaths, store: ObjectStore | None = None) -> bool:
    """Map remote-verified + locally reclaimed segments away from FAILED.

    After a successful reclaim the local ndjson/gz are *supposed* to be gone.
    Drain must not treat that as an unresolved error (canary segment 000006).
    """

    if _bulky_payload_present(paths):
        return False
    audit = (paths.dir / "reclaim_audit.json").exists()
    verified_local = (paths.dir / "verify_ok.json").exists()
    remote_ok = False
    if store is not None:
        try:
            remote_ok = (
                store.exists(remote_data_key(paths.segment_id))
                and store.exists(remote_manifest_key(paths.segment_id))
                and store.exists(remote_evidence_key(paths.segment_id))
            )
        except Exception:  # noqa: BLE001 — adoption is best-effort; leave FAILED if unknown
            remote_ok = False
    if not audit and not remote_ok and not verified_local:
        return False
    record = read_state(paths)
    if record is None:
        return False
    if record.state == SegmentState.RECLAIMABLE:
        return True
    record.state = SegmentState.RECLAIMABLE
    record.error = None
    write_state(paths, record)
    return True


class SegmentOffloader:
    """Async-safe sequential offloader for sealed segments (call from worker)."""

    def __init__(
        self,
        store: ObjectStore,
        *,
        verify_roundtrip_parquet: bool = False,
        max_upload_attempts: int = 5,
        backoff_base_seconds: float = 1.0,
    ) -> None:
        self.store = store
        self.verify_roundtrip_parquet = verify_roundtrip_parquet
        self.max_upload_attempts = max_upload_attempts
        self.backoff_base_seconds = backoff_base_seconds

    def process_sealed(self, paths: SegmentPaths) -> OffloadResult:
        started = time.perf_counter()
        record = read_state(paths)
        if record is None:
            raise FileNotFoundError(paths.segment_id)
        if adopt_verified_reclaimed(paths, self.store):
            current = read_state(paths)
            assert current is not None
            return OffloadResult(
                segment_id=paths.segment_id,
                state=current.state,
                remote_data_key=remote_data_key(paths.segment_id),
                remote_manifest_key=remote_manifest_key(paths.segment_id),
                elapsed_seconds=0.0,
            )
        if record.state == SegmentState.RECLAIMABLE:
            return OffloadResult(
                segment_id=paths.segment_id,
                state=record.state,
                remote_data_key=record.remote_key,
                remote_manifest_key=remote_manifest_key(paths.segment_id),
                elapsed_seconds=0.0,
            )
        if record.state == SegmentState.ACTIVE:
            raise RuntimeError("refusing to offload ACTIVE segment")
        if record.state not in {
            SegmentState.SEALED_UNVERIFIED,
            SegmentState.UPLOADING,
            SegmentState.VERIFIED_REMOTE,
            SegmentState.FAILED,
        }:
            raise RuntimeError(f"unexpected state {record.state}")

        try:
            if record.state in {
                SegmentState.SEALED_UNVERIFIED,
                SegmentState.FAILED,
                SegmentState.UPLOADING,
            }:
                self._compress_and_manifest(paths, record)
                self._upload_with_retry(paths, record)
            if record.state in {
                SegmentState.UPLOADING,
                SegmentState.SEALED_UNVERIFIED,
                SegmentState.FAILED,
                SegmentState.VERIFIED_REMOTE,
            }:
                # After upload path, state may already be UPLOADING; verify advances it.
                record = read_state(paths) or record
                if record.state != SegmentState.RECLAIMABLE:
                    self._verify_remote(paths, record)
            record = read_state(paths)
            assert record is not None
            return OffloadResult(
                segment_id=paths.segment_id,
                state=record.state,
                remote_data_key=remote_data_key(paths.segment_id),
                remote_manifest_key=remote_manifest_key(paths.segment_id),
                elapsed_seconds=time.perf_counter() - started,
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed segment to FAILED
            message = str(exc)
            if adopt_verified_reclaimed(paths, self.store):
                current = read_state(paths)
                assert current is not None
                return OffloadResult(
                    segment_id=paths.segment_id,
                    state=current.state,
                    remote_data_key=remote_data_key(paths.segment_id),
                    remote_manifest_key=remote_manifest_key(paths.segment_id),
                    elapsed_seconds=time.perf_counter() - started,
                )
            logger.error("offload failed for %s: %s", paths.segment_id, message)
            current = read_state(paths)
            if current is not None:
                current.state = SegmentState.FAILED
                current.error = message[:500]
                write_state(paths, current)
            return OffloadResult(
                segment_id=paths.segment_id,
                state=SegmentState.FAILED,
                remote_data_key=None,
                remote_manifest_key=None,
                elapsed_seconds=time.perf_counter() - started,
                error=message,
            )

    def _compress_and_manifest(self, paths: SegmentPaths, record: SegmentStateRecord) -> None:
        if not paths.sealed_ndjson.exists():
            raise FileNotFoundError(paths.sealed_ndjson)
        if not paths.gzip_path.exists():
            gzip_ndjson(paths.sealed_ndjson, paths.gzip_path)
        if self.verify_roundtrip_parquet:
            prove = prove_round_trip(paths.sealed_ndjson, paths.dir / "roundtrip_work")
            if not prove["roundtrip_equal"]:
                raise RuntimeError("parquet round-trip inequality")
        gzip_sha = sha256_file(paths.gzip_path)
        manifest = build_manifest(
            paths,
            archived_bytes=paths.gzip_path.stat().st_size,
            archive_format="ndjson.gz",
            content_sha256=record.content_sha256 or sha256_file(paths.sealed_ndjson),
        )
        manifest["archived_sha256"] = gzip_sha
        write_manifest(paths, manifest)
        record.state = SegmentState.UPLOADING
        record.remote_key = remote_data_key(paths.segment_id)
        write_state(paths, record)

    def _upload_with_retry(self, paths: SegmentPaths, record: SegmentStateRecord) -> None:
        data_key = remote_data_key(paths.segment_id)
        man_key = remote_manifest_key(paths.segment_id)
        last_error: Exception | None = None
        for attempt in range(1, self.max_upload_attempts + 1):
            try:
                if not self.store.exists(data_key):
                    self.store.publish_file(data_key, paths.gzip_path)
                # If object already exists, reconcile size/hash in verify step.
                if not self.store.exists(man_key):
                    self.store.publish_bytes(
                        man_key,
                        paths.manifest_path.read_bytes(),
                    )
                record.state = SegmentState.UPLOADING
                record.remote_key = data_key
                write_state(paths, record)
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                delay = self.backoff_base_seconds * (2 ** (attempt - 1))
                # Bounded jitter without importing secrets into hot path tests.
                delay += 0.05 * attempt
                time.sleep(min(delay, 30.0))
        raise RuntimeError(f"upload exhausted retries: {last_error}")

    def _verify_remote(self, paths: SegmentPaths, record: SegmentStateRecord) -> None:
        data_key = remote_data_key(paths.segment_id)
        man_key = remote_manifest_key(paths.segment_id)
        if not self.store.exists(data_key):
            raise RuntimeError("remote data object missing")
        if not self.store.exists(man_key):
            raise RuntimeError("remote manifest missing")
        local_gzip_sha = sha256_file(paths.gzip_path)
        local_size = paths.gzip_path.stat().st_size
        with tempfile.TemporaryDirectory(prefix="ext-verify-") as tmp:
            tmp_path = Path(tmp)
            downloaded = tmp_path / "events.ndjson.gz"
            self.store.download_file(data_key, downloaded)
            if downloaded.stat().st_size != local_size:
                raise RuntimeError("remote size mismatch")
            if sha256_file(downloaded) != local_gzip_sha:
                raise RuntimeError("remote content hash mismatch")
            restored = tmp_path / "events.ndjson"
            gunzip_ndjson(downloaded, restored)
            local_summary = summarize_ndjson(paths.sealed_ndjson)
            remote_summary = summarize_ndjson(restored)
            if remote_summary["event_count"] != local_summary["event_count"]:
                raise RuntimeError("restore event count mismatch")
            if remote_summary["content_sha256"] != local_summary["content_sha256"]:
                raise RuntimeError("restore content hash mismatch")
            # Schema / required field spot-check
            for row in iter_ndjson_records(restored):
                for field in (
                    "venue",
                    "instrument",
                    "event_type",
                    "received_at",
                    "local_sequence",
                    "payload",
                ):
                    if field not in row:
                        raise RuntimeError(f"restore missing field {field}")
                break
            remote_manifest = json.loads(self.store.read_bytes(man_key))
            if remote_manifest.get("content_sha256") != local_summary["content_sha256"]:
                raise RuntimeError("manifest content hash mismatch")
            if int(remote_manifest["event_counts"]["total"]) != local_summary["event_count"]:
                raise RuntimeError("manifest count mismatch")
            evidence = {
                "segment_id": paths.segment_id,
                "verified_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "local_gzip_sha256": local_gzip_sha,
                "event_count": local_summary["event_count"],
                "status": "VERIFY_OK",
            }
            evidence_key = remote_evidence_key(paths.segment_id)
            if not self.store.exists(evidence_key):
                self.store.publish_bytes(
                    evidence_key,
                    json.dumps(evidence, indent=2, sort_keys=True).encode("utf-8"),
                )
            # Local VERIFY_OK marker survives reclaim of bulky ndjson/gz so
            # drain/recover can adopt RECLAIMABLE without a B2 round-trip.
            verify_path = paths.dir / "verify_ok.json"
            if not verify_path.exists():
                verify_path.write_text(
                    json.dumps(evidence, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
        record.state = SegmentState.VERIFIED_REMOTE
        record.error = None
        write_state(paths, record)
        # Mark reclaimable only after full gate.
        record.state = SegmentState.RECLAIMABLE
        write_state(paths, record)


def reclaim_local_segment(paths: SegmentPaths, *, delete_sealed: bool = True) -> dict[str, Any]:
    """Delete only this verified segment's local bulky files; keep state audit."""

    record = read_state(paths)
    if record is None:
        raise FileNotFoundError(paths.segment_id)
    if record.state != SegmentState.RECLAIMABLE:
        raise RuntimeError(f"refusing delete in state {record.state}")
    deleted: list[str] = []
    for path in (
        paths.sealed_ndjson,
        paths.gzip_path,
        paths.parquet_path,
        paths.active_ndjson,
    ):
        if path.exists():
            if path == paths.sealed_ndjson and not delete_sealed:
                continue
            path.unlink()
            deleted.append(path.name)
    work = paths.dir / "roundtrip_work"
    if work.exists():
        shutil.rmtree(work)
        deleted.append("roundtrip_work/")
    audit = {
        "segment_id": paths.segment_id,
        "reclaimed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "deleted": deleted,
        "state": str(SegmentState.RECLAIMABLE),
    }
    audit_path = paths.dir / "reclaim_audit.json"
    if not audit_path.exists():
        audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")
    return audit


def recover_root(root: Path) -> dict[str, Any]:
    """Reconstruct/repair segment states from durable local evidence on restart."""

    actions: list[dict[str, Any]] = []
    if not root.exists():
        return {"actions": actions}
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        paths = SegmentPaths(root, child.name)
        for stale in child.glob(".state.*.tmp"):
            try:
                stale.unlink()
                actions.append({"segment_id": child.name, "action": "drop_stale_state_tmp"})
            except OSError:
                pass
        record = read_state(paths)
        if record is None:
            continue
        if record.state == SegmentState.ACTIVE and paths.active_ndjson.exists():
            stats = recover_trailing_partial_ndjson(paths.active_ndjson)
            actions.append({"segment_id": child.name, "action": "trim_partial", **stats})
            write_state(paths, record)
        elif record.state == SegmentState.ACTIVE and paths.sealed_ndjson.exists():
            # Crash after rename but before state update.
            record.state = SegmentState.SEALED_UNVERIFIED
            write_state(paths, record)
            actions.append({"segment_id": child.name, "action": "promote_sealed"})
        elif record.state == SegmentState.SEALED_UNVERIFIED and not paths.sealed_ndjson.exists():
            if paths.active_ndjson.exists():
                stats = recover_trailing_partial_ndjson(paths.active_ndjson)
                os.replace(paths.active_ndjson, paths.sealed_ndjson)
                actions.append({"segment_id": child.name, "action": "seal_recovered", **stats})
        elif record.state == SegmentState.FAILED:
            # Local payload gone after a completed reclaim must not stay FAILED.
            if adopt_verified_reclaimed(paths, store=None):
                actions.append({"segment_id": child.name, "action": "adopt_reclaimed"})
        # UPLOADING / VERIFIED_REMOTE: leave for offloader retry; never delete.
    return {"actions": actions}
