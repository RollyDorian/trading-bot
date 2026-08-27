"""Convert empty monolithic ``market_events`` to RANGE(id) partitioned storage.

Revision ID: 20260809_0004
Revises: 20260729_0002
Create Date: 2026-08-09

Fail closed if the table already contains rows. Preserves
``market_events_id_seq`` so the next insert continues monotonically.
Destructive DROP of generations remains operator-controlled outside this
migration; this revision only establishes the parent + first ACTIVE partition
and durable generation metadata.

Chained after RAW v2 (0002) so production can adopt partitions without first
enabling the normalized-core revision (0003), which remains optional and
disabled for live tail/backfill on the constrained VPS.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "20260809_0004"
down_revision: str | None = "20260729_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep in sync with trading_bot.storage.partitions.DEFAULT_GENERATION_ROW_SPAN.
DEFAULT_GENERATION_ROW_SPAN = 400_000


def upgrade() -> None:
    if context.is_offline_mode():
        # Offline SQL cannot inspect emptiness/sequence state. Emit the
        # structural conversion; operators must verify COUNT(*)=0 and setval
        # continuity before applying in production.
        _emit_partitioned_ddl(
            last_value=1,
            is_called=False,
            next_id=1,
        )
        return

    bind = op.get_bind()
    row_count = bind.execute(sa.text("SELECT COUNT(*) FROM market_events")).scalar_one()
    if int(row_count) != 0:
        raise RuntimeError(
            "refusing to convert market_events: table is not empty; "
            "archive and retain first, then convert only when empty"
        )
    relkind = bind.execute(
        sa.text(
            """
            SELECT c.relkind
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = 'market_events'
            """
        )
    ).scalar_one()
    if relkind == "p":
        raise RuntimeError("market_events is already partitioned")

    seq = bind.execute(
        sa.text("SELECT last_value, is_called FROM market_events_id_seq")
    ).one()
    last_value = int(seq.last_value)
    is_called = bool(seq.is_called)
    next_id = last_value + 1 if is_called else last_value
    _emit_partitioned_ddl(
        last_value=last_value,
        is_called=is_called,
        next_id=next_id,
    )


def _emit_partitioned_ddl(*, last_value: int, is_called: bool, next_id: int) -> None:
    # Prevent DROP TABLE from removing the global identity sequence.
    op.execute(sa.text("ALTER SEQUENCE market_events_id_seq OWNED BY NONE"))
    op.execute(sa.text("DROP TABLE market_events"))

    op.execute(
        sa.text(
            """
            CREATE TABLE market_events (
                id BIGINT NOT NULL,
                received_at TIMESTAMPTZ DEFAULT now() NOT NULL,
                exchange_at TIMESTAMPTZ,
                source VARCHAR(32) NOT NULL,
                event_type VARCHAR(64) NOT NULL,
                symbol VARCHAR(32) NOT NULL,
                sequence BIGINT,
                connection_id VARCHAR(36),
                local_sequence BIGINT,
                exchange_sequence BIGINT,
                schema_version SMALLINT DEFAULT 1 NOT NULL,
                latency_ms DOUBLE PRECISION,
                payload JSONB NOT NULL,
                PRIMARY KEY (id)
            ) PARTITION BY RANGE (id)
            """
        )
    )
    op.execute(
        sa.text(
            """
            ALTER TABLE market_events
            ALTER COLUMN id SET DEFAULT nextval('market_events_id_seq')
            """
        )
    )
    op.execute(
        sa.text("ALTER SEQUENCE market_events_id_seq OWNED BY market_events.id")
    )
    # is_called=true semantics: next nextval() returns last_value+1.
    op.execute(
        sa.text(
            f"SELECT setval('market_events_id_seq', {last_value}, "
            f"{'true' if is_called else 'false'})"
        )
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

    id_start = next_id
    id_end = id_start + DEFAULT_GENERATION_ROW_SPAN
    partition_name = f"market_events_g_{id_start}"
    generation_key = f"g_{id_start}_{id_end}"
    op.execute(
        sa.text(
            f"""
            CREATE TABLE {partition_name}
            PARTITION OF market_events
            FOR VALUES FROM ({id_start}) TO ({id_end})
            """
        )
    )

    op.create_table(
        "market_event_generations",
        sa.Column("generation_key", sa.Text(), primary_key=True),
        sa.Column("partition_name", sa.Text(), nullable=False, unique=True),
        sa.Column("id_start", sa.BigInteger(), nullable=False),
        sa.Column("id_end", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("row_span", sa.BigInteger(), nullable=False),
        sa.Column("physical_bytes_at_close", sa.BigInteger(), nullable=True),
        sa.Column("archive_evidence_sha256", sa.Text(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("drop_eligible_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dropped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id_start < id_end", name="ck_market_event_generations_bounds"),
        sa.CheckConstraint(
            "state IN ("
            "'PROVISIONED','ACTIVE','CLOSED_UNARCHIVED','ARCHIVING',"
            "'VERIFIED','DROP_ELIGIBLE','DROPPED','ARCHIVE_FAILED','VERIFY_FAILED'"
            ")",
            name="ck_market_event_generations_state",
        ),
    )
    op.create_index(
        "ix_market_event_generations_id_range",
        "market_event_generations",
        ["id_start", "id_end"],
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO market_event_generations (
                generation_key, partition_name, id_start, id_end, state, row_span
            ) VALUES (
                '{generation_key}', '{partition_name}', {id_start}, {id_end},
                'ACTIVE', {DEFAULT_GENERATION_ROW_SPAN}
            )
            """
        )
    )

    # Operator-callable DROP gate: checks DROP_ELIGIBLE then drops the partition.
    # Invoked only by a maintenance identity / owner; research must not EXECUTE.
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION public.drop_verified_market_event_generation(
                p_generation_key text,
                p_confirmation_token text,
                p_operator_approved boolean
            ) RETURNS bigint
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = public
            AS $fn$
            DECLARE
                v_state text;
                v_partition text;
                v_bytes bigint;
            BEGIN
                IF p_confirmation_token IS DISTINCT FROM 'DROP_VERIFIED_GENERATION' THEN
                    RAISE EXCEPTION 'invalid DROP confirmation token';
                END IF;
                IF NOT COALESCE(p_operator_approved, false) THEN
                    RAISE EXCEPTION 'operator approval required for generation DROP';
                END IF;
                SELECT state, partition_name
                  INTO v_state, v_partition
                  FROM market_event_generations
                 WHERE generation_key = p_generation_key
                 FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'unknown generation %', p_generation_key;
                END IF;
                IF v_state IS DISTINCT FROM 'DROP_ELIGIBLE' THEN
                    RAISE EXCEPTION
                        'refusing DROP for %: state is %, required DROP_ELIGIBLE',
                        p_generation_key, v_state;
                END IF;
                IF v_state = 'ACTIVE' THEN
                    RAISE EXCEPTION 'never DROP ACTIVE';
                END IF;
                v_bytes := pg_total_relation_size(
                    to_regclass(format('public.%I', v_partition))
                );
                EXECUTE format('DROP TABLE %I', v_partition);
                UPDATE market_event_generations
                   SET state = 'DROPPED',
                       dropped_at = now(),
                       updated_at = now()
                 WHERE generation_key = p_generation_key;
                RETURN COALESCE(v_bytes, 0);
            END
            $fn$
            """
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION "
            "public.drop_verified_market_event_generation(text, text, boolean) "
            "FROM PUBLIC"
        )
    )


def downgrade() -> None:
    if context.is_offline_mode():
        last_value = 1
        is_called = False
    else:
        bind = op.get_bind()
        row_count = bind.execute(sa.text("SELECT COUNT(*) FROM market_events")).scalar_one()
        if int(row_count) != 0:
            raise RuntimeError(
                "refusing to downgrade partitioned market_events while rows exist"
            )
        seq = bind.execute(
            sa.text("SELECT last_value, is_called FROM market_events_id_seq")
        ).one()
        last_value = int(seq.last_value)
        is_called = bool(seq.is_called)

    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS "
            "public.drop_verified_market_event_generation(text, text, boolean)"
        )
    )
    op.execute(sa.text("ALTER SEQUENCE market_events_id_seq OWNED BY NONE"))
    # Drop parent cascades to partitions.
    op.execute(sa.text("DROP TABLE IF EXISTS market_events CASCADE"))
    op.drop_table("market_event_generations")

    # Reattach the preserved global sequence; do not let autoincrement create a new one.
    op.create_table(
        "market_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
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
        sa.Column("connection_id", sa.String(length=36), nullable=True),
        sa.Column("local_sequence", sa.BigInteger(), nullable=True),
        sa.Column("exchange_sequence", sa.BigInteger(), nullable=True),
        sa.Column(
            "schema_version",
            sa.SmallInteger(),
            server_default="1",
            nullable=False,
        ),
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
    op.execute(sa.text("ALTER SEQUENCE market_events_id_seq OWNED BY market_events.id"))
    op.execute(
        sa.text(
            """
            ALTER TABLE market_events
            ALTER COLUMN id SET DEFAULT nextval('market_events_id_seq')
            """
        )
    )
    op.execute(
        sa.text(
            f"SELECT setval('market_events_id_seq', {last_value}, "
            f"{'true' if is_called else 'false'})"
        )
    )
