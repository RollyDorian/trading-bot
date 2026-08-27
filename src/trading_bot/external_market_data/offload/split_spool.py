"""Split a monolithic canary NDJSON spool into bounded sealed segments (offline proof)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading_bot.external_market_data.offload.segments import ActiveSegmentWriter


def split_spool(
    source: Path,
    destination: Path,
    *,
    max_bytes: int = 16 * 1024 * 1024,
) -> dict[str, object]:
    destination.mkdir(parents=True, exist_ok=True)
    writer = ActiveSegmentWriter(destination, max_bytes=max_bytes, max_seconds=10**9)
    sealed_ids: list[str] = []
    lines = 0
    with source.open("rb") as handle:
        for line in handle:
            if not line.strip():
                continue
            if not line.endswith(b"\n"):
                line = line + b"\n"
            sealed = writer.append_line(line)
            lines += 1
            if sealed is not None:
                sealed_ids.append(sealed.segment_id)
    final = writer.close()
    if final is not None:
        sealed_ids.append(final.segment_id)
    sizes = []
    for seg_id in sealed_ids:
        ndjson = destination / seg_id / "events.ndjson"
        sizes.append(ndjson.stat().st_size if ndjson.exists() else 0)
    return {
        "source": str(source),
        "destination": str(destination),
        "lines": lines,
        "segment_count": len(sealed_ids),
        "segment_ids": sealed_ids,
        "sizes_bytes": sizes,
        "max_bytes": max_bytes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Split canary NDJSON into sealed segments")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    args = parser.parse_args(argv)
    report = split_spool(args.source, args.destination, max_bytes=args.max_bytes)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
