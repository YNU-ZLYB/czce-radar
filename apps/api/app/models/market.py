"""日线与治理表模型。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, Date, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DailyBar(Base):
    """国内品种日线（回测与特征的权威历史）。"""

    __tablename__ = "daily_bar"

    variety_code: Mapped[str] = mapped_column(String(8), primary_key=True)
    trade_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    settle: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    volume: Mapped[int | None] = mapped_column(BigInteger)
    oi: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(24), nullable=False)


class ForeignDaily(Base):
    """外盘日线。"""

    __tablename__ = "foreign_daily"

    ext_code: Mapped[str] = mapped_column(String(8), primary_key=True)
    trade_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    change_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    source: Mapped[str] = mapped_column(String(24), nullable=False)


class Source(Base):
    """数据源注册与最新状态（/api/v1/sources 的数据来源）。"""

    __tablename__ = "source"

    code: Mapped[str] = mapped_column(String(24), primary_key=True)
    name: Mapped[str] = mapped_column(String(48), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    interval_sec: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_status: Mapped[str | None] = mapped_column(String(12))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_run_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)


class FetchLog(Base):
    """每次抓取的结果记录（成功/失败都记，故障定位的依据）。"""

    __tablename__ = "fetch_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_code: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)  # ok/empty/error
    fetched: Mapped[int | None] = mapped_column(Integer)
    inserted: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[dt.datetime] = mapped_column(nullable=False)
