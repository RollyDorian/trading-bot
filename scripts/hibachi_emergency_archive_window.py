"""One hourly archive window: reuse verified local/B2 COMPLETED or export+upload.

Upload already performs checksum download + restore validation. A second
independent parquet restore is skipped unless that builtin path is missing.
Does not DROP PostgreSQL data. Does not overwrite immutable B2 objects.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from trading_bot.archive.window import (
    OPERATIONAL_DISK_FLOOR_BYTES,
    _completed_key,
    dataset_has_incomplete_marker,
    verify_restore_archive,
)
from trading_bot.archive_cli import _archive_export_window, _b2_store
from trading_bot.research.dataset import generate_dataset_id


def _is_retryable_b2(error: BaseException) -> bool:
    text = str(error)
    return any(
        token in text
        for token in ("403", "Forbidden", "429", "503", "SlowDown", "timeout")
    )


def _retry[T](op: Callable[[], T], *, attempts: int = 6, delay_s: float = 8.0) -> T:
    """Retry B2 rate-limit/403 HeadObject flakes without treating them as missing."""

    last: BaseException | None = None
    wait = delay_s
    for attempt in range(1, attempts + 1):
        try:
            return op()
        except Exception as exc:
            last = exc
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if not _is_retryable_b2(exc) or attempt == attempts:
                raise
            print(
                f"b2_retry attempt={attempt} wait_s={wait:.0f} error={exc}",
                file=sys.stderr,
            )
            time.sleep(wait)
            wait = min(wait * 2, 60.0)
    assert last is not None
    raise last


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=datetime.fromisoformat)
    parser.add_argument("--end", required=True, type=datetime.fromisoformat)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--evidence-json", required=True, type=Path)
    parser.add_argument("--max-rows", required=True, type=int)
    parser.add_argument("--min-disk-bytes", required=True, type=int)
    parser.add_argument("--max-bytes", default=64 * 1024 * 1024, type=int)
    return parser.parse_args()


def _write_evidence(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_local_evidence(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _upload_restore_verified(summary: dict[str, Any]) -> bool:
    """True when archive-export-window already completed download+restore."""

    if summary.get("status") not in {"verified", "COMPLETED"}:
        return False
    restore = summary.get("restore_result")
    if not isinstance(restore, dict):
        return True
    return restore.get("status") in {None, "verified"}


def _independent_restore(
    *,
    store: Any,
    dataset_id: str,
    work_dir: Path,
) -> dict[str, Any]:
    restore_root = work_dir / "_independent_restore"
    if restore_root.exists():
        shutil.rmtree(restore_root)
    restore_root.mkdir(parents=True, exist_ok=True)
    try:
        return verify_restore_archive(store, dataset_id, restore_root)
    finally:
        shutil.rmtree(restore_root, ignore_errors=True)


def main() -> int:
    args = _parse_args()
    if args.min_disk_bytes < OPERATIONAL_DISK_FLOOR_BYTES:
        print("min-disk-bytes below 3 GiB operational floor", file=sys.stderr)
        return 2
    work_dir = args.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    dataset_id = generate_dataset_id(args.symbol, args.start, args.end)
    prior = _load_local_evidence(args.evidence_json)
    if (
        prior
        and prior.get("status") == "verified"
        and prior.get("dataset_id") == dataset_id
    ):
        # Already passed the one required readback. Do not download again.
        prior["mode"] = "reuse_local_evidence"
        print(json.dumps(prior, sort_keys=True))
        return 0

    store = _b2_store()
    completed_present = False
    try:
        marker = store.read_bytes(_completed_key(dataset_id))
        completed_present = b"COMPLETED" in marker
    except Exception as exc:
        # Missing keys must not consume retry/Class B budget.
        print(f"completed_probe_error={exc}", file=sys.stderr)

    if completed_present:
        restore = _retry(
            lambda: _independent_restore(
                store=store, dataset_id=dataset_id, work_dir=work_dir
            )
        )
        if restore.get("status") == "verified":
            result: dict[str, Any] = {
                "dataset_id": dataset_id,
                "status": "verified",
                "mode": "reuse_completed",
                "checksums_pass": True,
                "manifest_pass": True,
                "remote_completed": True,
                "download_verification_pass": True,
                "storage_reconciliation_pass": True,
                "export_status": None,
                "restore_status": "verified",
                "row_count": None,
                "error": None,
            }
            _write_evidence(args.evidence_json, result)
            print(json.dumps(result, sort_keys=True))
            return 0
        print("completed_exists_but_restore_failed", file=sys.stderr)
        _write_evidence(
            args.evidence_json,
            {
                "dataset_id": dataset_id,
                "status": "failed",
                "mode": "reuse_completed",
                "error": restore.get("error") or restore.get("status"),
            },
        )
        print(json.dumps({"dataset_id": dataset_id, "status": "failed"}, sort_keys=True))
        return 1

    mode = "export_upload"
    incomplete = False
    try:
        incomplete = _retry(lambda: dataset_has_incomplete_marker(store, dataset_id))
    except Exception as exc:
        print(f"incomplete_probe_error={exc}", file=sys.stderr)

    if incomplete:
        payload: dict[str, Any] = {
            "dataset_id": dataset_id,
            "status": "failed",
            "mode": mode,
            "error": "incomplete attempt exists; fail-closed, no overwrite",
        }
        _write_evidence(args.evidence_json, payload)
        print(json.dumps(payload, sort_keys=True))
        return 1

    export_summary: dict[str, Any] = {}
    ns = argparse.Namespace(
        start=args.start,
        end=args.end,
        output_dir=work_dir,
        provider="b2",
        confirm_upload=True,
        symbol=args.symbol,
        max_duration_seconds=3600,
        max_rows=args.max_rows,
        max_bytes=args.max_bytes,
        min_disk_bytes=args.min_disk_bytes,
        allow_quality_warnings=True,
        confirm_quarantine_upload=True,
        gap_warning_seconds=60.0,
        price_discontinuity_percent=20.0,
        exchange_boundary_tolerance_seconds=5.0,
    )
    try:
        export_summary = _retry(lambda: asyncio.run(_archive_export_window(ns)))
    except Exception as exc:
        print(f"export_error={exc}", file=sys.stderr)
        probed = _independent_restore(
            store=store, dataset_id=dataset_id, work_dir=work_dir
        )
        if probed.get("status") == "verified":
            export_summary = {"status": "verified", "row_count": None}
        else:
            raise
    if export_summary.get("status") not in {"verified", "COMPLETED"}:
        failed: dict[str, Any] = {
            "dataset_id": dataset_id,
            "status": "failed",
            "mode": mode,
            "export": export_summary,
        }
        _write_evidence(args.evidence_json, failed)
        print(json.dumps(failed, sort_keys=True))
        return 1

    if not _upload_restore_verified(export_summary):
        print("upload_missing_restore_result; one independent restore", file=sys.stderr)
        restore = _retry(
            lambda: _independent_restore(
                store=store, dataset_id=dataset_id, work_dir=work_dir
            )
        )
        restore_ok = restore.get("status") == "verified"
        restore_status = restore.get("status")
        restore_error = restore.get("error")
    else:
        # Upload path already downloaded artifacts, checked SHA-256, and restore-validated.
        restore_ok = True
        restore_status = "verified"
        restore_error = None
    result: dict[str, Any] = {
        "dataset_id": dataset_id,
        "status": "verified" if restore_ok else "failed",
        "mode": mode,
        "checksums_pass": restore_ok,
        "manifest_pass": restore_ok,
        "remote_completed": True,
        "download_verification_pass": restore_ok,
        "storage_reconciliation_pass": restore_ok,
        "export_status": export_summary.get("status"),
        "restore_status": restore_status,
        "row_count": export_summary.get("row_count"),
        "error": restore_error,
        "class_b_path": "upload_builtin_verify",
    }
    _write_evidence(args.evidence_json, result)
    print(json.dumps(result, sort_keys=True))
    return 0 if restore_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
