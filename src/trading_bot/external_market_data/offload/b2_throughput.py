"""Bounded B2 throughput probe for external segment-sized artifacts.

Uploads/downloads a temporary object under external/binance_usdm/_throughput/
and never deletes remote objects (immutable smoke). Prints JSON metrics only.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from trading_bot.archive.b2 import B2ArchiveConfig
from trading_bot.archive.store import S3ArchiveStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True, help="Local file to upload")
    parser.add_argument(
        "--key",
        default=None,
        help="Object key under bucket (default: external/.../_throughput/<name>)",
    )
    parser.add_argument("--download-to", type=Path, required=True)
    args = parser.parse_args(argv)

    store = S3ArchiveStore.for_b2(B2ArchiveConfig.from_environ())
    name = args.source.name
    key = args.key or f"external/binance_usdm/_throughput/{int(time.time())}_{name}"
    size = args.source.stat().st_size
    report: dict[str, object] = {
        "key": key,
        "bytes": size,
        "existed_before": store.exists(key),
    }
    if not report["existed_before"]:
        t0 = time.perf_counter()
        store.publish_file(key, args.source)
        upload_s = time.perf_counter() - t0
        report["upload_seconds"] = upload_s
        report["upload_mib_per_s"] = (size / (1024 * 1024)) / max(upload_s, 1e-9)
    else:
        report["upload_seconds"] = None
        report["upload_mib_per_s"] = None
        report["note"] = "skipped upload; object already existed (immutable)"

    args.download_to.parent.mkdir(parents=True, exist_ok=True)
    t1 = time.perf_counter()
    store.download_file(key, args.download_to)
    download_s = time.perf_counter() - t1
    report["download_seconds"] = download_s
    report["download_mib_per_s"] = (size / (1024 * 1024)) / max(download_s, 1e-9)
    report["download_bytes"] = args.download_to.stat().st_size
    report["size_match"] = args.download_to.stat().st_size == size
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["size_match"] else 2


if __name__ == "__main__":
    # Avoid printing secrets; config loads from env only.
    if not os.environ.get("B2_S3_BUCKET"):
        raise SystemExit("B2_S3_BUCKET not set")
    raise SystemExit(main())
