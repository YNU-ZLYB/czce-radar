"""knowledge & feature tables: knowledge_node/edge, seasonality, industry_metric, feature_snapshot

Revision ID: 0004_knowledge_features
Revises: 0003_market_tables
Create Date: 2026-11-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_knowledge_features"
down_revision: str | None = "0003_market_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 品种级传导校准系数（v1 回测实证；0 = 未校准/被证伪，不展示传导行）
    op.add_column(
        "variety",
        sa.Column("linkage_q", sa.Numeric(4, 3), nullable=False, server_default="0"),
    )

    # 产业链节点（上游/中游/下游，展示与 LLM 解释用，不直接计分）
    op.create_table(
        "knowledge_node",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),  # upstream/midstream/downstream
        sa.Column("name", sa.String(48), nullable=False),
        sa.Column("ext_code", sa.String(8), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_node"),
        sa.UniqueConstraint("kind", "name", name="uq_knowledge_node_kind_name"),
    )

    # 传导边：驱动因子 → 品种（Layer A 计分的核心，prior 来自手册，q 品种级校准）
    op.create_table(
        "knowledge_edge",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("variety_code", sa.String(8), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),  # cost/demand/substitute/policy/weather
        sa.Column("name", sa.String(48), nullable=False),
        sa.Column("proxy_ext", sa.String(8), nullable=True),
        sa.Column("proxy_variety", sa.String(8), nullable=True),
        sa.Column("metric", sa.String(16), nullable=True),
        sa.Column("lag_days", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("prior", sa.Numeric(5, 3), nullable=False),
        sa.Column("mechanism", sa.Text(), nullable=True),
        sa.Column("reviewed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["variety_code"], ["variety.code"], name="fk_edge_variety"),
        sa.ForeignKeyConstraint(["proxy_variety"], ["variety.code"], name="fk_edge_proxy_variety"),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_edge"),
        sa.UniqueConstraint("variety_code", "name", name="uq_knowledge_edge_variety_name"),
    )
    op.create_index("ix_knowledge_edge_variety", "knowledge_edge", ["variety_code"])
    op.create_index("ix_knowledge_edge_proxy_ext", "knowledge_edge", ["proxy_ext"])

    # 季节性偏移（人工核实后生效；reviewed_by 为核实人标记）
    op.create_table(
        "seasonality",
        sa.Column("variety_code", sa.String(8), nullable=False),
        sa.Column("month", sa.SmallInteger(), nullable=False),
        sa.Column("stage", sa.String(32), nullable=True),
        sa.Column("bias", sa.Numeric(6, 3), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 2), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.String(16), nullable=True),
        sa.ForeignKeyConstraint(["variety_code"], ["variety.code"], name="fk_season_variety"),
        sa.PrimaryKeyConstraint("variety_code", "month", name="pk_seasonality"),
    )

    # 产业指标（仓单/库存/现货基差），kind 区分
    op.create_table(
        "industry_metric",
        sa.Column("variety_code", sa.String(8), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("value", sa.Numeric(18, 4), nullable=False),
        sa.Column("unit", sa.String(16), nullable=True),
        sa.Column("source", sa.String(24), nullable=False),
        sa.PrimaryKeyConstraint("variety_code", "trade_date", "kind", name="pk_industry_metric"),
    )

    # 特征快照（可复现核心：variety + asof + version 唯一）
    op.create_table(
        "feature_snapshot",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("variety_code", sa.String(8), nullable=False),
        sa.Column("asof", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("features", sa.JSON(), nullable=False),
        sa.Column("display", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_feature_snapshot"),
        sa.UniqueConstraint("variety_code", "asof", "version", name="uq_feature_snapshot"),
    )
    op.create_index("ix_feature_snapshot_asof", "feature_snapshot", ["asof"])

    # 资讯情绪日聚合（M5 资讯模块填充；特征层 T-1 严格滞后读取）
    op.create_table(
        "news_sentiment_d",
        sa.Column("variety_code", sa.String(8), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("score", sa.Numeric(6, 3), nullable=False),
        sa.Column("doc_count", sa.Integer(), nullable=False),
        sa.Column("pos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("neg", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("neutral", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("variety_code", "trade_date", name="pk_news_sentiment_d"),
    )


def downgrade() -> None:
    op.drop_table("news_sentiment_d")
    op.drop_index("ix_feature_snapshot_asof", table_name="feature_snapshot")
    op.drop_table("feature_snapshot")
    op.drop_table("industry_metric")
    op.drop_table("seasonality")
    op.drop_index("ix_knowledge_edge_proxy_ext", table_name="knowledge_edge")
    op.drop_index("ix_knowledge_edge_variety", table_name="knowledge_edge")
    op.drop_table("knowledge_edge")
    op.drop_table("knowledge_node")
    op.drop_column("variety", "linkage_q")
