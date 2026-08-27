"""Add the minimal normalized-data core.

Revision ID: 20260729_0003
Revises: 20260729_0002
Create Date: 2026-07-29
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260729_0003"
# Normalized core stays optional after RAW partition adoption on constrained hosts.
down_revision: str | None = "20260809_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NUMERIC = sa.Numeric(38, 18)


def _provenance_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column("raw_event_id", sa.BigInteger(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exchange_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("connection_id", sa.String(36), nullable=True),
        sa.Column("local_sequence", sa.BigInteger(), nullable=True),
        sa.Column("exchange_sequence", sa.BigInteger(), nullable=True),
        sa.Column("raw_schema_version", sa.SmallInteger(), nullable=False),
        sa.Column("pipeline_version", sa.SmallInteger(), nullable=False),
        sa.Column("data_quality", sa.String(32), nullable=False),
    ]


def _typed_table(
    name: str,
    *columns: sa.Column[Any],
    unique_name: str,
) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        *_provenance_columns(),
        *columns,
        sa.UniqueConstraint("raw_event_id", "pipeline_version", name=unique_name),
        schema="normalized",
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("CREATE SCHEMA normalized")
    op.execute("CREATE SCHEMA pipeline")
    _typed_table(
        "best_quotes",
        sa.Column("bid_price", NUMERIC, nullable=False),
        sa.Column("bid_size", NUMERIC, nullable=False),
        sa.Column("ask_price", NUMERIC, nullable=False),
        sa.Column("ask_size", NUMERIC, nullable=False),
        unique_name="uq_best_quotes_raw_pipeline",
    )
    op.create_index(
        "ix_best_quotes_symbol_available",
        "best_quotes",
        ["symbol", "available_at"],
        schema="normalized",
    )
    _typed_table(
        "reference_prices",
        sa.Column("price_kind", sa.String(16), nullable=False),
        sa.Column("price", NUMERIC, nullable=False),
        unique_name="uq_reference_prices_raw_pipeline",
    )
    op.create_index(
        "ix_reference_prices_symbol_kind_available",
        "reference_prices",
        ["symbol", "price_kind", "available_at"],
        schema="normalized",
    )
    _typed_table(
        "funding_estimates",
        sa.Column("estimated_rate", NUMERIC, nullable=False),
        sa.Column("next_funding_at", sa.DateTime(timezone=True), nullable=False),
        unique_name="uq_funding_estimates_raw_pipeline",
    )
    op.create_index(
        "ix_funding_estimates_symbol_available",
        "funding_estimates",
        ["symbol", "available_at"],
        schema="normalized",
    )
    _typed_table(
        "orderbook_events",
        sa.Column("message_type", sa.String(16), nullable=False),
        sa.Column("depth", sa.SmallInteger(), nullable=False),
        sa.Column("granularity", NUMERIC, nullable=False),
        sa.Column(
            "bids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "asks",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("changed_level_count", sa.SmallInteger(), nullable=False),
        unique_name="uq_orderbook_events_raw_pipeline",
    )
    op.create_index(
        "ix_orderbook_events_symbol_available",
        "orderbook_events",
        ["symbol", "available_at"],
        schema="normalized",
    )
    op.create_table(
        "checkpoints",
        sa.Column("consumer", sa.String(64), primary_key=True),
        sa.Column("last_raw_event_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("pipeline_version", sa.SmallInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema="pipeline",
    )
    op.create_table(
        "normalization_errors",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("raw_event_id", sa.BigInteger(), nullable=False),
        sa.Column("pipeline_version", sa.SmallInteger(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=False),
        sa.Column("error_detail", sa.String(160), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "raw_event_id",
            "pipeline_version",
            name="uq_normalization_errors_raw_pipeline",
        ),
        schema="pipeline",
    )
    op.create_index(
        "ix_normalization_errors_code_created",
        "normalization_errors",
        ["error_code", "created_at"],
        schema="pipeline",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("normalization_errors", schema="pipeline")
    op.drop_table("checkpoints", schema="pipeline")
    op.drop_table("orderbook_events", schema="normalized")
    op.drop_table("funding_estimates", schema="normalized")
    op.drop_table("reference_prices", schema="normalized")
    op.drop_table("best_quotes", schema="normalized")
    op.execute("DROP SCHEMA pipeline")
    op.execute("DROP SCHEMA normalized")
