"""CDC source objects: heartbeat, meta.cdc_epochs, publication shop_cdc, grants to debezium

Spec: docs/contracts/services.md, "CDC source objects". The role `debezium` (LOGIN REPLICATION) is created outside
Alembic (CNPG managed role, compose init SQL); this migration fails with a clear message if it is missing, and the
migration Job's retries cover the short window before CNPG reconciles it.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09 18:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SHOP_TABLES = ("customers", "products", "orders", "order_items", "payments")
# Explicit list, never FOR ALL TABLES: a new source table needs a migration and a data contract (data/contracts/).
PUBLISHED_TABLES = (*SHOP_TABLES, "heartbeat")


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'debezium') THEN
                RAISE EXCEPTION 'role "debezium" does not exist'
                    USING HINT = 'Create it first: CNPG managed.roles on shop-db, or the compose init SQL '
                                 '(existing compose volume: make dev-reset).';
            END IF;
        END
        $$
        """
    )

    op.create_table(
        "heartbeat",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("beat_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_heartbeat_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_heartbeat")),
    )
    op.execute("INSERT INTO heartbeat (id) VALUES (1)")

    op.execute("CREATE SCHEMA meta")
    op.create_table(
        "cdc_epochs",
        sa.Column("epoch", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("snapshot_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("epoch", name=op.f("pk_cdc_epochs")),
        schema="meta",
    )

    tables = ", ".join(PUBLISHED_TABLES)
    op.execute(f"CREATE PUBLICATION shop_cdc FOR TABLE {tables}")
    op.execute("GRANT USAGE ON SCHEMA public TO debezium")
    op.execute(f"GRANT SELECT ON {tables} TO debezium")
    op.execute("GRANT UPDATE ON heartbeat TO debezium")


def downgrade() -> None:
    op.execute("DROP PUBLICATION shop_cdc")
    op.execute(f"REVOKE ALL ON {', '.join(SHOP_TABLES)} FROM debezium")
    op.execute("REVOKE USAGE ON SCHEMA public FROM debezium")
    op.drop_table("cdc_epochs", schema="meta")
    op.execute("DROP SCHEMA meta")
    op.drop_table("heartbeat")
