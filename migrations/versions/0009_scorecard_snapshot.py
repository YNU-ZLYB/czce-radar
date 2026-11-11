"""scorecard_snapshot：每日事后评分快照，供滚动命中率看板与周报对比。

Revision ID: 0009_scorecard_snapshot
Revises: 0008_news
Create Date: 2026-11-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0009_scorecard_snapshot"
down_revision: str | None = "0008_news"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scorecard_snapshot",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("model_version", sa.String(24), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("scored_through", sa.Date(), nullable=True),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("coverage", sa.Numeric(6, 4), nullable=False),
        sa.Column("direction_n", sa.Integer(), nullable=False),
        sa.Column("direction_accuracy", sa.Numeric(6, 4), nullable=True),
        sa.Column("brier", sa.Numeric(8, 6), nullable=True),
        sa.Column("by_variety", JSONB(), nullable=True),
        sa.Column("top_misses", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_scorecard_snapshot"),
        sa.UniqueConstraint(
            "run_date", "model_version", name="uq_scorecard_run_date_model"
        ),
    )
    op.create_index(
        "ix_scorecard_snapshot_run_date",
        "scorecard_snapshot",
        [sa.text("run_date DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_scorecard_snapshot_run_date", table_name="scorecard_snapshot")
    op.drop_table("scorecard_snapshot")
