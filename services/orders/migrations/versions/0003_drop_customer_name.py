"""DEMO (do not merge): drop customers.name to show the data contract check failing

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-09 18:35:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("customers", "name")


def downgrade() -> None:
    op.add_column("customers", sa.Column("name", sa.Text(), nullable=False, server_default=""))
