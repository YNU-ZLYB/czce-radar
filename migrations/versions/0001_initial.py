"""initial variety table

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "variety",
        sa.Column("code", sa.String(length=8), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("sector", sa.String(length=24), nullable=False),
        sa.Column("is_tradable", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("listed_at", sa.Date(), nullable=True),
        sa.Column("delisted_at", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("code", name="pk_variety"),
    )
    op.create_index("ix_variety_sector_sort", "variety", ["sector", "sort_order"])


def downgrade() -> None:
    op.drop_index("ix_variety_sector_sort", table_name="variety")
    op.drop_table("variety")
