import argparse
import asyncio
import json
from pathlib import Path

from trading_bot.config import integration_test_database_url
from trading_bot.normalization.pilot import bounded_summary, run_capacity_pilot
from trading_bot.storage.database import create_engine, create_session_factory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a bounded normalized-data capacity pilot")
    parser.add_argument("--capacity-path", type=Path, required=True)
    parser.add_argument("--max-raw-rows", type=int, required=True)
    parser.add_argument("--production-free-bytes", type=int)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


async def run() -> int:
    args = parse_args()
    engine = create_engine(integration_test_database_url())
    try:
        report = await run_capacity_pilot(
            create_session_factory(engine),
            capacity_path=args.capacity_path,
            max_raw_rows=args.max_raw_rows,
            production_free_bytes=args.production_free_bytes,
        )
    finally:
        await engine.dispose()
    print(json.dumps(report, sort_keys=True) if args.json else bounded_summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
