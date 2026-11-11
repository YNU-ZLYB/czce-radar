"""news_doc / news_extract / news_sentiment_d / prediction_revision / llm_call_log

Revision ID: 0008_news
Revises: 0007_model_registry
Create Date: 2026-11-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008_news"
down_revision: str | None = "0007_model_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 资讯原文（去重：source+external_id 与 content_hash 双唯一）
    op.create_table(
        "news_doc",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(40), nullable=False),
        sa.Column("raw", JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_news_doc"),
        sa.UniqueConstraint("source", "external_id", name="uq_news_doc_source_external_id"),
        sa.UniqueConstraint("content_hash", name="uq_news_doc_content_hash"),
    )
    op.create_index(
        "ix_news_doc_published_at", "news_doc", [sa.text("published_at DESC")]
    )

    # 结构化抽取（一个文档 × 一个品种 × 一种方法 唯一，便于幂等重跑）
    op.create_table(
        "news_extract",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("doc_id", sa.BigInteger(), nullable=False),
        sa.Column("variety_code", sa.String(8), nullable=False),
        sa.Column("direction", sa.SmallInteger(), nullable=False),
        sa.Column("strength", sa.SmallInteger(), nullable=False),
        sa.Column("chain_stage", sa.String(16), nullable=True),
        sa.Column("is_surprise", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("model", sa.String(32), nullable=True),
        sa.Column("confidence", sa.Numeric(4, 2), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["doc_id"], ["news_doc.id"], name="fk_news_extract_doc"),
        sa.ForeignKeyConstraint(
            ["variety_code"], ["variety.code"], name="fk_news_extract_variety"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_news_extract"),
        sa.UniqueConstraint(
            "doc_id", "variety_code", "method", name="uq_news_extract_doc_variety_method"
        ),
    )
    op.create_index("ix_news_extract_variety", "news_extract", ["variety_code"])

    # 每日资讯情绪：0004 已建表（pos/neg/neutral），此处对齐《02》口径：
    # score 放宽到 NUMERIC(8,3)，pos/neg 重命名为 pos_count/neg_count（neutral 保留备用）。
    op.alter_column(
        "news_sentiment_d",
        "score",
        existing_type=sa.Numeric(6, 3),
        type_=sa.Numeric(8, 3),
        existing_nullable=False,
    )
    op.alter_column(
        "news_sentiment_d", "pos", new_column_name="pos_count", existing_nullable=False
    )
    op.alter_column(
        "news_sentiment_d", "neg", new_column_name="neg_count", existing_nullable=False
    )

    # 盘中修订（只追加，不覆盖原预测）
    op.create_table(
        "prediction_revision",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("prediction_id", sa.BigInteger(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prev_signal", sa.SmallInteger(), nullable=True),
        sa.Column("new_signal", sa.SmallInteger(), nullable=True),
        sa.Column("prev_direction", sa.SmallInteger(), nullable=True),
        sa.Column("new_direction", sa.SmallInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("trigger_doc_id", sa.BigInteger(), nullable=True),
        sa.Column("is_surprise", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(
            ["prediction_id"], ["prediction.id"], name="fk_revision_prediction"
        ),
        sa.ForeignKeyConstraint(
            ["trigger_doc_id"], ["news_doc.id"], name="fk_revision_trigger_doc"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_prediction_revision"),
        sa.UniqueConstraint(
            "prediction_id", "trigger_doc_id", name="uq_revision_prediction_doc"
        ),
    )
    op.create_index(
        "ix_prediction_revision_at", "prediction_revision", [sa.text("at DESC")]
    )

    # LLM 调用日志（成本、缓存命中、耗时）
    op.create_table(
        "llm_call_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("purpose", sa.String(24), nullable=True),
        sa.Column("model", sa.String(32), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_cny", sa.Numeric(8, 4), nullable=True),
        sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_llm_call_log"),
    )
    op.create_index("ix_llm_call_log_created_at", "llm_call_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_llm_call_log_created_at", table_name="llm_call_log")
    op.drop_table("llm_call_log")
    op.drop_index("ix_prediction_revision_at", table_name="prediction_revision")
    op.drop_table("prediction_revision")
    op.alter_column(
        "news_sentiment_d", "neg_count", new_column_name="neg", existing_nullable=False
    )
    op.alter_column(
        "news_sentiment_d", "pos_count", new_column_name="pos", existing_nullable=False
    )
    op.alter_column(
        "news_sentiment_d",
        "score",
        existing_type=sa.Numeric(8, 3),
        type_=sa.Numeric(6, 3),
        existing_nullable=False,
    )
    op.drop_index("ix_news_extract_variety", table_name="news_extract")
    op.drop_table("news_extract")
    op.drop_index("ix_news_doc_published_at", table_name="news_doc")
    op.drop_table("news_doc")
