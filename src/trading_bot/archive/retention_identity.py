"""Least-privilege identity checks for RAW retention mutation."""

from __future__ import annotations

import os

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

EXPECTED_RETENTION_DB_ROLE = "retention"

# Runtime, migration, superuser, and integration-test roles must never mutate RAW.
FORBIDDEN_RETENTION_MUTATION_ROLES = frozenset(
    {
        "research",
        "cryptobot",
        # Production COLLECT runtime login (SELECT+INSERT only; never retention).
        "cryptobot_runtime",
        "postgres",
        "test",
    }
)


def require_retention_database_url() -> str:
    """Return the retention mutation URL or fail closed without printing secrets."""

    database_url = os.environ.get("RETENTION_DATABASE_URL")
    if not database_url:
        raise ValueError(
            "RETENTION_DATABASE_URL is required for retention-execute "
            "--confirm-delete; research DATABASE_URL cannot be used for mutation"
        )
    parsed = make_url(database_url)
    if not parsed.drivername.startswith("postgresql"):
        raise ValueError("RETENTION_DATABASE_URL must use PostgreSQL")
    username = parsed.username
    if username != EXPECTED_RETENTION_DB_ROLE:
        raise ValueError(
            f"RETENTION_DATABASE_URL must authenticate as "
            f"{EXPECTED_RETENTION_DB_ROLE!r}, got {username!r}"
        )
    if parsed.host is None or parsed.database is None:
        raise ValueError(
            "RETENTION_DATABASE_URL must identify a PostgreSQL host and database"
        )
    return database_url


async def require_retention_mutation_identity(session: AsyncSession) -> str:
    """Verify the active PostgreSQL session is the dedicated retention role."""

    row = (
        await session.execute(
            text(
                "SELECT current_user, session_user, "
                "current_setting('is_superuser')::boolean"
            )
        )
    ).one()
    current_user = str(row[0])
    session_user = str(row[1])
    is_superuser = bool(row[2])

    if is_superuser:
        raise PermissionError("retention mutation rejected: superuser session")
    if current_user in FORBIDDEN_RETENTION_MUTATION_ROLES:
        raise PermissionError(
            f"retention mutation forbidden for role {current_user!r}"
        )
    if current_user != EXPECTED_RETENTION_DB_ROLE:
        raise PermissionError(
            "retention mutation requires database role "
            f"{EXPECTED_RETENTION_DB_ROLE!r}, got {current_user!r}"
        )
    if session_user != EXPECTED_RETENTION_DB_ROLE:
        raise PermissionError(
            "retention mutation requires session_user "
            f"{EXPECTED_RETENTION_DB_ROLE!r}, got {session_user!r}"
        )
    return current_user
