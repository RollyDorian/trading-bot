import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Never

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.config import Settings
from trading_bot.storage.database import create_engine, create_session_factory

HEALTHY_EXIT_CODE = 0
UNHEALTHY_EXIT_CODE = 1
FAILURE_EXIT_CODE = 2
SAFE_FAILURE_MESSAGE = "collector healthcheck failed"


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        raise ValueError


def receipt_is_fresh(
    latest_receipt: datetime | None,
    *,
    now: datetime,
    max_age_seconds: float,
) -> bool:
    if latest_receipt is None or latest_receipt.tzinfo is None:
        return False
    age = now.astimezone(UTC) - latest_receipt.astimezone(UTC)
    return timedelta(0) <= age <= timedelta(seconds=max_age_seconds)


# O(1) freshness: latest persisted row in the ACTIVE id range via PK
# backward scan (ORDER BY id DESC LIMIT 1). Do not MAX(received_at) over the
# whole ACTIVE child — that index scan of 200k+ rows timed out at 10s under
# 1-vCPU contention with the external gzip/B2 path (2026-08-19T18:33:53Z).
# Still partition-pruned: never a parent-wide historical MAX().
ACTIVE_RECEIPT_SQL = """
SELECT m.received_at
FROM market_event_generations AS g
JOIN LATERAL (
  SELECT received_at
  FROM market_events AS m
  WHERE m.id >= g.id_start
    AND m.id < g.id_end
  ORDER BY m.id DESC
  LIMIT 1
) AS m ON TRUE
WHERE g.state = 'ACTIVE'
"""


async def latest_active_receipt(session: AsyncSession) -> datetime | None:
    """Latest RAW receipt inside the ACTIVE generation (id-bounded)."""

    result = await session.execute(text(ACTIVE_RECEIPT_SQL))
    value = result.scalar_one_or_none()
    return value if value is None or isinstance(value, datetime) else None


async def collector_is_healthy(settings: Settings, *, max_age_seconds: float) -> bool:
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            latest_receipt = await latest_active_receipt(session)
    except SQLAlchemyError:
        return False
    finally:
        await engine.dispose()
    return receipt_is_fresh(
        latest_receipt,
        now=datetime.now(UTC),
        max_age_seconds=max_age_seconds,
    )


def run(argv: Sequence[str] | None = None) -> int:
    try:
        parser = _SafeArgumentParser(
            description="Check COLLECT-only database freshness",
            add_help=False,
            exit_on_error=False,
        )
        parser.add_argument("--max-age-seconds", type=float, default=120.0)
        args = parser.parse_args(argv)
        if args.max_age_seconds <= 0:
            raise ValueError("max age must be positive")
        healthy = asyncio.run(
            collector_is_healthy(Settings(), max_age_seconds=args.max_age_seconds)
        )
    except BaseException:
        print(SAFE_FAILURE_MESSAGE, file=sys.stderr)
        return FAILURE_EXIT_CODE
    if not healthy:
        return UNHEALTHY_EXIT_CODE
    print("healthy")
    return HEALTHY_EXIT_CODE


def main(argv: Sequence[str] | None = None) -> Never:
    raise SystemExit(run(argv))


if __name__ == "__main__":
    main()
