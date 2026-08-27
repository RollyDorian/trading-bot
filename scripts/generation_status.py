"""Read-only RAW generation rotation status for operators.

Does not mutate PostgreSQL, B2, filesystem thresholds, or collector state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

# Allow `python scripts/generation_status.py` from a checkout without install.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from trading_bot.storage.operator_status import (  # noqa: E402
    build_operator_status,
    format_operator_status_text,
    operator_status_to_dict,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only RAW generation / capacity operator status"
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Async SQLAlchemy URL (default: DATABASE_URL)",
    )
    parser.add_argument(
        "--free-disk-bytes",
        type=int,
        default=None,
        help="Override free disk bytes (default: disk holding --disk-path)",
    )
    parser.add_argument(
        "--disk-path",
        type=Path,
        default=Path("."),
        help="Filesystem path whose free bytes feed capacity (prefer PG data mount)",
    )
    parser.add_argument(
        "--wal-bytes",
        type=int,
        default=None,
        help="Optional WAL size hint in bytes",
    )
    parser.add_argument(
        "--collector-state",
        default=os.environ.get("COLLECTOR_STATE", "unknown"),
        help="RUNNING / STOPPED / unknown (informational only)",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text)",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    if not args.database_url:
        print("DATABASE_URL / --database-url required", file=sys.stderr)
        return 2
    free = args.free_disk_bytes
    if free is None:
        free = int(shutil.disk_usage(args.disk_path).free)
    engine = create_async_engine(args.database_url)
    try:
        async with engine.connect() as conn:
            report = await build_operator_status(
                conn,
                free_disk_bytes=free,
                wal_bytes=args.wal_bytes,
                collector_state=args.collector_state,
            )
        if args.format == "json":
            print(json.dumps(operator_status_to_dict(report), indent=2, sort_keys=True))
        else:
            print(format_operator_status_text(report))
        return 0
    finally:
        await engine.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parse_args())))


if __name__ == "__main__":
    main()
