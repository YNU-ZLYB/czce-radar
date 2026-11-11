"""industry_metric.kind 加宽：warehouse_receipt 等标准 kind 超过 16 字符

Revision ID: 0005_metric_kind_widen
Revises: 0004_knowledge_features
Create Date: 2026-11-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_metric_kind_widen"
down_revision: str | None = "0004_knowledge_features"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "industry_metric",
        "kind",
        existing_type=sa.String(16),
        type_=sa.String(32),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "industry_metric",
        "kind",
        existing_type=sa.String(32),
        type_=sa.String(16),
        existing_nullable=False,
    )
