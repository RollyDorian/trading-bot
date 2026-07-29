"""Add the RAW schema-version-2 envelope without indexing legacy NULL rows.

Revision ID: 20260729_0002
Revises: 20260715_0001
Create Date: 2026-07-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260729_0002"
down_revision: str | None = "20260715_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE market_events
            ADD COLUMN connection_id VARCHAR(36),
            ADD COLUMN local_sequence BIGINT,
            ADD COLUMN exchange_sequence BIGINT,
            ADD COLUMN schema_version SMALLINT NOT NULL DEFAULT 1
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE market_events
            DROP COLUMN schema_version,
            DROP COLUMN exchange_sequence,
            DROP COLUMN local_sequence,
            DROP COLUMN connection_id
        """
    )
