"""Create append-only event storage.

Revision ID: 20260715_0001
Revises:
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260715_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("exchange_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_market_events_source_sequence",
        "market_events",
        ["source", "sequence"],
    )
    op.create_index(
        "ix_market_events_symbol_exchange_at",
        "market_events",
        ["symbol", "exchange_at"],
    )
    op.create_index(
        "ix_market_events_type_received_at",
        "market_events",
        ["event_type", "received_at"],
    )

    op.create_table(
        "system_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("component", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_system_events_type_occurred_at",
        "system_events",
        ["event_type", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_system_events_type_occurred_at", table_name="system_events")
    op.drop_table("system_events")
    op.drop_index("ix_market_events_type_received_at", table_name="market_events")
    op.drop_index("ix_market_events_symbol_exchange_at", table_name="market_events")
    op.drop_index("ix_market_events_source_sequence", table_name="market_events")
    op.drop_table("market_events")
