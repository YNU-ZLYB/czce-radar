"""资讯原文、结构化抽取、日情绪与 LLM 调用日志。"""

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


class NewsDoc(Base):
    """资讯原文（source+external_id 与 content_hash 双唯一去重）。"""

    __tablename__ = "news_doc"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(40), nullable=False)
    raw: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        Index("ix_news_doc_published_at", published_at.desc()),
        {"comment": "uq(source,external_id) / uq(content_hash) 在迁移层定义"},
    )


class NewsExtract(Base):
    """资讯结构化抽取：一个文档 × 一个品种 × 一种方法唯一。"""

    __tablename__ = "news_extract"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("news_doc.id", name="fk_news_extract_doc"), nullable=False
    )
    variety_code: Mapped[str] = mapped_column(
        String(8), ForeignKey("variety.code", name="fk_news_extract_variety"), nullable=False
    )
    direction: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    strength: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    chain_stage: Mapped[str | None] = mapped_column(String(16))
    is_surprise: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)  # llm / rule
    model: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    evidence: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_news_extract_variety", "variety_code"),
        {"comment": "uq(doc_id,variety_code,method) 在迁移层定义"},
    )


class NewsSentimentD(Base):
    """每日资讯情绪（严格 T+1：T 日预测只读 trade_date < T）。"""

    __tablename__ = "news_sentiment_d"

    variety_code: Mapped[str] = mapped_column(String(8), primary_key=True)
    trade_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    score: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    doc_count: Mapped[int] = mapped_column(Integer, nullable=False)
    pos_count: Mapped[int | None] = mapped_column(Integer)
    neg_count: Mapped[int | None] = mapped_column(Integer)


class LlmCallLog(Base):
    """LLM 调用日志：成本、缓存命中、耗时（成本治理的数据来源）。"""

    __tablename__ = "llm_call_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    purpose: Mapped[str | None] = mapped_column(String(24))
    model: Mapped[str | None] = mapped_column(String(32))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_cny: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_llm_call_log_created_at", "created_at"),)
