"""知识库与特征相关表。"""

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
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KnowledgeNode(Base):
    """产业链节点（上游/中游/下游），展示与解释用，不直接计分。"""

    __tablename__ = "knowledge_node"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    name: Mapped[str] = mapped_column(String(48), nullable=False)
    ext_code: Mapped[str | None] = mapped_column(String(8))

    __table_args__ = {"comment": "uq(kind,name) 在迁移层定义"}  # noqa: RUF012


class KnowledgeEdge(Base):
    """传导边：驱动因子 → 品种（Layer A 计分核心，prior 来自手册，q 品种级校准）。"""

    __tablename__ = "knowledge_edge"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    variety_code: Mapped[str] = mapped_column(
        String(8), ForeignKey("variety.code", name="fk_edge_variety"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    name: Mapped[str] = mapped_column(String(48), nullable=False)
    proxy_ext: Mapped[str | None] = mapped_column(String(8))
    proxy_variety: Mapped[str | None] = mapped_column(
        String(8), ForeignKey("variety.code", name="fk_edge_proxy_variety")
    )
    metric: Mapped[str | None] = mapped_column(String(16))
    lag_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    prior: Mapped[Decimal] = mapped_column(Numeric(5, 3), nullable=False)
    mechanism: Mapped[str | None] = mapped_column(Text)
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_knowledge_edge_variety", "variety_code"),
        Index("ix_knowledge_edge_proxy_ext", "proxy_ext"),
    )


class Seasonality(Base):
    """季节性偏移（人工核实后生效）。"""

    __tablename__ = "seasonality"

    variety_code: Mapped[str] = mapped_column(
        String(8), ForeignKey("variety.code", name="fk_season_variety"), primary_key=True
    )
    month: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    stage: Mapped[str | None] = mapped_column(String(32))
    bias: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(String(16))


class IndustryMetric(Base):
    """产业指标（仓单/库存/现货基差），kind 区分。"""

    __tablename__ = "industry_metric"

    variety_code: Mapped[str] = mapped_column(String(8), primary_key=True)
    trade_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(24), nullable=False)


class FeatureSnapshot(Base):
    """特征快照（可复现核心：variety + asof + version 唯一）。"""

    __tablename__ = "feature_snapshot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    variety_code: Mapped[str] = mapped_column(String(8), nullable=False)
    asof: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    features: Mapped[dict] = mapped_column(JSONB, nullable=False)
    display: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_feature_snapshot_asof", "asof"),
        {"comment": "uq(variety_code,asof,version) 在迁移层定义"},
    )
