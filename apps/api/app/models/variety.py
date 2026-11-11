"""品种主数据。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import Boolean, Date, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Variety(Base):
    """郑商所品种。已下市品种保留主数据，但 is_tradable=false 且不参与预测。"""

    __tablename__ = "variety"

    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    sector: Mapped[str] = mapped_column(String(24), nullable=False)
    is_tradable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    listed_at: Mapped[dt.date | None] = mapped_column(Date)
    delisted_at: Mapped[dt.date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    #: 外盘传导校准系数（v1 回测实证；0=未校准/证伪）
    linkage_q: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False, default=Decimal("0"))

    __table_args__ = (Index("ix_variety_sector_sort", "sector", "sort_order"),)
