"""model_registry

Revision ID: 0007_model_registry
Revises: 0006_prediction
Create Date: 2026-11-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0007_model_registry"
down_revision: str | None = "0006_prediction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 模型注册表：版本、指标、状态（active/shadow/retired）
    op.create_table(
        "model_registry",
        sa.Column("version", sa.String(24), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("params", JSONB(), nullable=True),
        sa.Column("train_start", sa.Date(), nullable=True),
        sa.Column("train_end", sa.Date(), nullable=True),
        sa.Column("metrics", JSONB(), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="shadow"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("version", name="pk_model_registry"),
    )


def downgrade() -> None:
    op.drop_table("model_registry")
