"""Managed generation maintenance (provision + rotate metadata). Never DROP.

Intended for cron/systemd/docker oneshot independent of an interactive SSH
session. Uses the owner/maintenance database URL that can CREATE partition
children; the research collector role must not run this.

Does not archive to B2 and does not physically DROP partitions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from trading_bot.storage.capacity import (  # noqa: E402
    CapacityInputs,
    CapacityState,
    assess_capacity,
)
from trading_bot.storage.partitions import (  # noqa: E402
    GenerationState,
    list_generations,
    measure_relation_size,
)
from trading_bot.storage.rotation import maintain_writable_generations  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provision successor + rotate ACTIVE metadata (no DROP)"
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("MAINTENANCE_DATABASE_URL")
        or os.environ.get("DATABASE_URL"),
        help="Owner/maintenance async URL (not research)",
    )
    parser.add_argument(
        "--free-disk-bytes",
        type=int,
        required=True,
        help="Current filesystem free bytes (fail-closed capacity gate)",
    )
    parser.add_argument(
        "--wal-bytes",
        type=int,
        default=128 * 1024 * 1024,
        help="WAL/transient cushion bytes",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    if not args.database_url:
        print("MAINTENANCE_DATABASE_URL / DATABASE_URL required", file=sys.stderr)
        return 2
    engine = create_async_engine(args.database_url)
    try:
        async with engine.begin() as conn:
            generations = await list_generations(conn)
            closed_bytes = 0
            closed_count = 0
            drop_bytes = 0
            drop_count = 0
            active_bytes = 0
            for generation in generations:
                if generation.state == GenerationState.ACTIVE:
                    active_bytes = (
                        await measure_relation_size(conn, generation.partition_name)
                    ).total_bytes
                phys = generation.physical_bytes_at_close or 0
                if generation.state in {
                    GenerationState.CLOSED_UNARCHIVED,
                    GenerationState.ARCHIVING,
                    GenerationState.ARCHIVE_FAILED,
                    GenerationState.VERIFY_FAILED,
                }:
                    closed_count += 1
                    closed_bytes += phys
                if generation.state == GenerationState.DROP_ELIGIBLE:
                    drop_count += 1
                    drop_bytes += phys
            assessment = assess_capacity(
                CapacityInputs(
                    free_disk_bytes=args.free_disk_bytes,
                    closed_unarchived_count=closed_count,
                    closed_unarchived_bytes=closed_bytes,
                    drop_eligible_count=drop_count,
                    drop_eligible_bytes=drop_bytes,
                    active_generation_bytes=active_bytes,
                    wal_buffer_bytes=args.wal_bytes,
                )
            )
            # Writable-cover maintenance must run even under capacity STOP.
            # Incident: closed-archive backlog STOP exited before CREATE TABLE,
            # leaving ACTIVE uncovered past its upper bound.
            action = await maintain_writable_generations(conn)
            payload = {
                "status": (
                    "STOP_REQUIRED"
                    if assessment.state == CapacityState.STOP_REQUIRED
                    else "ok"
                ),
                "capacity": assessment.state.value,
                "reasons": list(assessment.reasons),
                "provisioned_successor": action.provisioned_successor,
                "rotated_active": action.rotated_active,
                "active": (
                    None
                    if action.active is None
                    else {
                        "generation_key": action.active.generation_key,
                        "state": action.active.state.value,
                        "id_start": action.active.id_start,
                        "id_end": action.active.id_end,
                    }
                ),
                "successor": (
                    None
                    if action.successor is None
                    else {
                        "generation_key": action.successor.generation_key,
                        "state": action.successor.state.value,
                        "id_start": action.successor.id_start,
                        "id_end": action.successor.id_end,
                    }
                ),
                "note": (
                    "physical DROP is never performed by this tool; "
                    "capacity STOP no longer skips successor provision/rotate"
                ),
                "collector_action": (
                    "stop"
                    if assessment.state == CapacityState.STOP_REQUIRED
                    else "none"
                ),
                "capacity_stop": (
                    "CAPACITY_STOP_REQUIRED"
                    if assessment.state == CapacityState.STOP_REQUIRED
                    else None
                ),
            }
            print(json.dumps(payload, sort_keys=True))
            if assessment.state == CapacityState.STOP_REQUIRED:
                return 3
            return 0
    finally:
        await engine.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parse_args())))


if __name__ == "__main__":
    main()
