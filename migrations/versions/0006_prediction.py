"""prediction / outcome / backtest_run

Revision ID: 0006_prediction
Revises: 0005_metric_kind_widen
Create Date: 2026-11-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0006_prediction"
down_revision: str | None = "0005_metric_kind_widen"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 预测（事前唯一权威）：每个品种每天每模型一条
    op.create_table(
        "prediction",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("variety_code", sa.String(8), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("horizon", sa.String(8), nullable=False, server_default="1d"),
        sa.Column("asof", sa.DateTime(timezone=True), nullable=False),
        sa.Column("direction", sa.SmallInteger(), nullable=False),
        sa.Column("bucket", sa.String(8), nullable=False),
        sa.Column("p_up", sa.Numeric(5, 4), nullable=False),
        sa.Column("p_flat", sa.Numeric(5, 4), nullable=False),
        sa.Column("p_down", sa.Numeric(5, 4), nullable=False),
        sa.Column("exp_change", sa.Numeric(10, 4), nullable=True),
        sa.Column("signal", sa.SmallInteger(), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("contributor", JSONB(), nullable=False),
        sa.Column("model_version", sa.String(24), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["variety_code"], ["variety.code"], name="fk_prediction_variety"),
        sa.PrimaryKeyConstraint("id", name="pk_prediction"),
        sa.UniqueConstraint(
            "variety_code",
            "target_date",
            "horizon",
            "model_version",
            name="uq_prediction_variety_target_horizon_model",
        ),
    )
    op.create_index("ix_prediction_target_date", "prediction", ["target_date"])
    op.create_index("ix_prediction_variety_date", "prediction", ["variety_code", "target_date"])

    # 事后评分：一条预测最多一条结果
    op.create_table(
        "outcome",
        sa.Column("prediction_id", sa.BigInteger(), nullable=False),
        sa.Column("actual_change", sa.Numeric(10, 4), nullable=False),
        sa.Column("actual_dir", sa.SmallInteger(), nullable=False),
        sa.Column("actual_bucket", sa.String(8), nullable=False),
        sa.Column("hit_dir", sa.Boolean(), nullable=False),
        sa.Column("brier", sa.Numeric(8, 6), nullable=False),
        sa.Column(
            "scored_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["prediction_id"], ["prediction.id"], name="fk_outcome_prediction"
        ),
        sa.PrimaryKeyConstraint("prediction_id", name="pk_outcome"),
    )

    # 回测运行记录（报告可追溯）
    op.create_table(
        "backtest_run",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("model_version", sa.String(24), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("config", JSONB(), nullable=True),
        sa.Column("metrics", JSONB(), nullable=True),
        sa.Column("report_md", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_backtest_run"),
    )


def downgrade() -> None:
    op.drop_table("backtest_run")
    op.drop_table("outcome")
    op.drop_index("ix_prediction_variety_date", table_name="prediction")
    op.drop_index("ix_prediction_target_date", table_name="prediction")
    op.drop_table("prediction")
