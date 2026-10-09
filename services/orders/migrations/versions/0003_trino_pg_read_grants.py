"""Read-only grants for role trino_pg (Trino catalog `pg`: reconciliation, CDC epochs)

Spec: docs/contracts/services.md, "Other database roles on shop-db". The role itself is a CNPG managed role
(sf-platform) or created by the compose init SQL; this migration fails with a clear message if it is missing.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-09 19:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMAS = "public, meta"
READ_TABLES = "customers, products, orders, order_items, payments, heartbeat, meta.cdc_epochs"


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trino_pg') THEN
                RAISE EXCEPTION 'Trino role "trino_pg" is missing: create it before migrating (CNPG managed.roles '
                                'on shop-db; compose init SQL, for an existing volume run make dev-reset)';
            END IF;
        END
        $$
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMAS} TO trino_pg")
    op.execute(f"GRANT SELECT ON {READ_TABLES} TO trino_pg")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT ON {READ_TABLES} FROM trino_pg")
    op.execute(f"REVOKE USAGE ON SCHEMA {SCHEMAS} FROM trino_pg")
