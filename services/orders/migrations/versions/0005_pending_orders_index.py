"""Partial index on pending orders, for the stranded-order sweeper and its oldest-pending gauge

The sweeper (orders) claims `pending` orders that have not moved for a while and re-settles them; the gauge reports
the age of the oldest `pending` order. Both touch only `pending` rows, a handful at any time, so a partial index keeps
them cheap however large `orders` grows. Indexes are not part of the data contracts (columns and keys only).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-10 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_orders_pending_created_at",
        "orders",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_orders_pending_created_at", table_name="orders")
