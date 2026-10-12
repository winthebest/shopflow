"""orders.idempotency_key and orders.request_hash, for `Idempotency-Key` on POST /checkout

A client that retries a checkout after a timeout or a 5xx sends the same key: orders answers with the order it already
created (or 409 while that order is still `pending`) instead of creating a second order and a second charge, and
refuses the key with a different request (`request_hash` = SHA-256 of the canonical request). Keys are optional, so
both columns are nullable; UNIQUE allows any number of NULLs. Additive for the data contract (no version bump).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-12 11:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("idempotency_key", sa.Text(), nullable=True))
    op.add_column("orders", sa.Column("request_hash", sa.Text(), nullable=True))
    op.create_unique_constraint(op.f("uq_orders_idempotency_key"), "orders", ["idempotency_key"])
    op.create_check_constraint(
        op.f("ck_orders_idempotency_key_length"), "orders", "char_length(idempotency_key) BETWEEN 1 AND 64"
    )
    op.create_check_constraint(
        op.f("ck_orders_idempotency_key_with_hash"), "orders", "(idempotency_key IS NULL) = (request_hash IS NULL)"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_orders_idempotency_key_with_hash"), "orders", type_="check")
    op.drop_constraint(op.f("ck_orders_idempotency_key_length"), "orders", type_="check")
    op.drop_constraint(op.f("uq_orders_idempotency_key"), "orders", type_="unique")
    op.drop_column("orders", "request_hash")
    op.drop_column("orders", "idempotency_key")
