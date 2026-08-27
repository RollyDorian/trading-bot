"""CLI helpers for external offload status and offline proof."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from trading_bot.archive.store import LocalArchiveStore
from trading_bot.external_market_data.offload.compress import (
    gzip_ndjson,
    ndjson_to_parquet,
    prove_round_trip,
)
from trading_bot.external_market_data.offload.lifecycle import (
    ObjectStore,
    SegmentOffloader,
    reclaim_local_segment,
    recover_root,
)
from trading_bot.external_market_data.offload.segments import (
    SegmentPaths,
    SegmentState,
    read_state,
)
from trading_bot.external_market_data.offload.split_spool import split_spool
from trading_bot.external_market_data.offload.status import status_json


def _cmd_status(args: argparse.Namespace) -> int:
    print(
        status_json(
            args.root,
            external_mode=args.external_mode,
            b2_health=args.b2_health,
            ingest_msg_per_sec=args.ingest_msg_per_sec,
            ingest_mib_per_hour=args.ingest_mib_per_hour,
            offload_mib_per_hour=args.offload_mib_per_hour,
            backlog_trend=args.backlog_trend,
        )
    )
    return 0


def _cmd_split(args: argparse.Namespace) -> int:
    report = split_spool(args.source, args.destination, max_bytes=args.max_bytes)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _cmd_compress_report(args: argparse.Namespace) -> int:
    work = args.work_dir
    work.mkdir(parents=True, exist_ok=True)
    gzip_path = work / "sample.ndjson.gz"
    parquet_path = work / "sample.parquet"
    gzip_stats = gzip_ndjson(args.source, gzip_path)
    # Parquet on full 128 MiB can be memory-heavy; allow --max-lines sample.
    source_for_parquet = args.source
    if args.max_lines is not None:
        sample = work / "sample_head.ndjson"
        with args.source.open("rb") as src, sample.open("wb") as dst:
            for i, line in enumerate(src):
                if i >= args.max_lines:
                    break
                dst.write(line if line.endswith(b"\n") else line + b"\n")
        source_for_parquet = sample
    parquet_stats = ndjson_to_parquet(source_for_parquet, parquet_path)
    roundtrip = prove_round_trip(source_for_parquet, work / "roundtrip")
    report = {
        "source": str(args.source),
        "gzip": gzip_stats,
        "parquet": parquet_stats,
        "parquet_source": str(source_for_parquet),
        "roundtrip_equal": roundtrip["roundtrip_equal"],
        "roundtrip": {
            "checked_events": roundtrip["checked_events"],
            "field_mismatches": roundtrip["field_mismatches"],
            "event_count_match": roundtrip["event_count_match"],
            "parquet_bytes": roundtrip["parquet"]["parquet_bytes"],
            "elapsed_seconds": roundtrip["parquet"]["elapsed_seconds"],
            "note": roundtrip["note"],
        },
    }
    out = work / "compression_report.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if roundtrip["roundtrip_equal"] else 2


def _b2_store() -> ObjectStore:
    # Lazy import: production canary images may predate archive.b2 on overlay mounts.
    from trading_bot.archive.b2 import B2ArchiveConfig
    from trading_bot.archive.store import S3ArchiveStore

    return S3ArchiveStore.for_b2(B2ArchiveConfig.from_environ())


def _cmd_offload_segments(args: argparse.Namespace) -> int:
    recover_root(args.root)
    store: ObjectStore = (
        LocalArchiveStore(args.local_store_root) if args.store == "local" else _b2_store()
    )
    offloader = SegmentOffloader(store, verify_roundtrip_parquet=args.verify_parquet)
    results = []
    for child in sorted(p for p in args.root.iterdir() if p.is_dir()):
        paths = SegmentPaths(args.root, child.name)
        record = read_state(paths)
        if record is None:
            continue
        if record.state in {
            SegmentState.SEALED_UNVERIFIED,
            SegmentState.UPLOADING,
            SegmentState.FAILED,
            SegmentState.VERIFIED_REMOTE,
        }:
            result = offloader.process_sealed(paths)
            results.append(
                {
                    "segment_id": result.segment_id,
                    "state": str(result.state),
                    "elapsed_seconds": result.elapsed_seconds,
                    "error": result.error,
                    "remote_data_key": result.remote_data_key,
                }
            )
            if args.reclaim and result.state == SegmentState.RECLAIMABLE:
                reclaim_local_segment(paths)
    print(json.dumps({"results": results}, indent=2, sort_keys=True))
    failed = [r for r in results if r["state"] == "FAILED"]
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hibachi-external-offload")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Read-only operator status")
    p_status.add_argument("--root", type=Path, required=True)
    p_status.add_argument("--external-mode", default="OFF")
    p_status.add_argument("--b2-health", default="unknown")
    p_status.add_argument("--ingest-msg-per-sec", type=float, default=0.0)
    p_status.add_argument("--ingest-mib-per-hour", type=float, default=0.0)
    p_status.add_argument("--offload-mib-per-hour", type=float, default=0.0)
    p_status.add_argument("--backlog-trend", default="unknown")
    p_status.set_defaults(func=_cmd_status)

    p_split = sub.add_parser("split", help="Split canary spool into sealed segments")
    p_split.add_argument("--source", type=Path, required=True)
    p_split.add_argument("--destination", type=Path, required=True)
    p_split.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    p_split.set_defaults(func=_cmd_split)

    p_comp = sub.add_parser("compress-report", help="Measure gzip/Parquet on canary data")
    p_comp.add_argument("--source", type=Path, required=True)
    p_comp.add_argument("--work-dir", type=Path, required=True)
    p_comp.add_argument("--max-lines", type=int, default=None)
    p_comp.set_defaults(func=_cmd_compress_report)

    p_off = sub.add_parser("offload", help="Offload sealed segments to store")
    p_off.add_argument("--root", type=Path, required=True)
    p_off.add_argument("--store", choices=("local", "b2"), default="local")
    p_off.add_argument(
        "--local-store-root",
        type=Path,
        default=Path("/tmp/external-offload-local-store"),
    )
    p_off.add_argument("--verify-parquet", action="store_true")
    p_off.add_argument("--reclaim", action="store_true")
    p_off.set_defaults(func=_cmd_offload_segments)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    code = args.func(args)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
    sys.stdout.flush()
