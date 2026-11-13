"""llm_doc_state: 缓存每个文档每个模型的 LLM 抽取结果状态，避免重复调用。

Revision ID: 0010_llm_doc_state
Revises: 0009_scorecard_snapshot
Create Date: 2026-11-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_llm_doc_state"
down_revision: str | None = "0009_scorecard_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_doc_state",
        sa.Column("doc_id", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.String(32), nullable=False),
        sa.Column("kept", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "dropped", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["doc_id"], ["news_doc.id"], name="fk_llm_doc_state_doc", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("doc_id", "model", name="pk_llm_doc_state"),
    )
    op.create_index("ix_llm_doc_state_model", "llm_doc_state", ["model"])


def downgrade() -> None:
    op.drop_index("ix_llm_doc_state_model", table_name="llm_doc_state")
    op.drop_table("llm_doc_state")
