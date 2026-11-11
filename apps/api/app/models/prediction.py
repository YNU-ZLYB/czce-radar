"""预测、事后评分与回测记录。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Prediction(Base):
    """事前预测（唯一权威）；同一品种/目标日/模型版本唯一。"""

    __tablename__ = "prediction"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    variety_code: Mapped[str] = mapped_column(
        String(8), ForeignKey("variety.code", name="fk_prediction_variety"), nullable=False
    )
    target_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    horizon: Mapped[str] = mapped_column(String(8), nullable=False, default="1d")
    asof: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    direction: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    bucket: Mapped[str] = mapped_column(String(8), nullable=False)
    p_up: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    p_flat: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    p_down: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    exp_change: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    signal: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    contributor: Mapped[list] = mapped_column(JSONB, nullable=False)
    model_version: Mapped[str] = mapped_column(String(24), nullable=False)
    narrative: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_prediction_target_date", "target_date"),
        Index("ix_prediction_variety_date", "variety_code", "target_date"),
        {
            "comment": "uq(variety_code,target_date,horizon,model_version) 在迁移层定义"
        },
    )


class PredictionRevision(Base):
    """盘中修订（只追加，不覆盖原预测；同一预测+触发文档唯一）。"""

    __tablename__ = "prediction_revision"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    prediction_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("prediction.id", name="fk_revision_prediction"),
        nullable=False,
    )
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    prev_signal: Mapped[int | None] = mapped_column(SmallInteger)
    new_signal: Mapped[int | None] = mapped_column(SmallInteger)
    prev_direction: Mapped[int | None] = mapped_column(SmallInteger)
    new_direction: Mapped[int | None] = mapped_column(SmallInteger)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_doc_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("news_doc.id", name="fk_revision_trigger_doc")
    )
    is_surprise: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_prediction_revision_at", at.desc()),
        {"comment": "uq(prediction_id,trigger_doc_id) 在迁移层定义"},
    )


class Outcome(Base):
    """事后评分（一条预测最多一条结果）。"""

    __tablename__ = "outcome"

    prediction_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("prediction.id", name="fk_outcome_prediction"),
        primary_key=True,
    )
    actual_change: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    actual_dir: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    actual_bucket: Mapped[str] = mapped_column(String(8), nullable=False)
    hit_dir: Mapped[bool] = mapped_column(Boolean, nullable=False)
    brier: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    scored_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ModelRegistry(Base):
    """模型注册表：版本、样本外指标与状态（active/shadow/retired）。"""

    __tablename__ = "model_registry"

    version: Mapped[str] = mapped_column(String(24), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # rule/gbm/fusion
    params: Mapped[dict | None] = mapped_column(JSONB)
    train_start: Mapped[dt.date | None] = mapped_column(Date)
    train_end: Mapped[dt.date | None] = mapped_column(Date)
    metrics: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="shadow")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BacktestRun(Base):
    """回测运行记录（报告与指标可追溯）。"""

    __tablename__ = "backtest_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    model_version: Mapped[str] = mapped_column(String(24), nullable=False)
    start_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    end_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    config: Mapped[dict | None] = mapped_column(JSONB)
    metrics: Mapped[dict | None] = mapped_column(JSONB)
    report_md: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
