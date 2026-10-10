"""shipments: new CDC source table written by fulfillment-worker

Follows the "adding a source table" rule (docs/contracts/services.md) in one migration: create the table, add it to
publication shop_cdc, grant SELECT to debezium and trino_pg (data/contracts/shipments.yaml lands in the same PR).
Writer role fulfillment_worker (CNPG managed role, compose init SQL) gets only what the worker needs:
INSERT on shipments and SELECT on orders.id (to skip events whose order no longer exists, e.g. after a PITR restore).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-10 13:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fulfillment_worker') THEN
                RAISE EXCEPTION 'Worker role "fulfillment_worker" is missing: create it before migrating (CNPG '
                                'managed.roles on shop-db; compose init SQL, for an existing volume run make dev-reset)';
            END IF;
        END
        $$
        """
    )

    op.create_table(
        "shipments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("cdc_epoch", sa.Integer(), nullable=False),
        sa.Column("source_lsn", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name=op.f("fk_shipments_order_id_orders")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shipments")),
        sa.UniqueConstraint("order_id", name=op.f("uq_shipments_order_id")),
    )
    # set_updated_at() comes from 0001.
    op.execute(
        "CREATE TRIGGER trg_shipments_updated_at BEFORE UPDATE ON shipments "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.execute("ALTER PUBLICATION shop_cdc ADD TABLE shipments")
    op.execute("GRANT SELECT ON shipments TO debezium, trino_pg")
    op.execute("GRANT USAGE ON SCHEMA public TO fulfillment_worker")
    op.execute("GRANT INSERT ON shipments TO fulfillment_worker")
    op.execute("GRANT SELECT (id) ON orders TO fulfillment_worker")


def downgrade() -> None:
    op.execute("REVOKE SELECT (id) ON orders FROM fulfillment_worker")
    op.execute("REVOKE USAGE ON SCHEMA public FROM fulfillment_worker")
    op.execute("ALTER PUBLICATION shop_cdc DROP TABLE shipments")
    op.drop_table("shipments")  # drops its trigger and every grant on it
