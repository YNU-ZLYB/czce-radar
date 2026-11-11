"""分钟行情（分区表父模型）。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Quote1m(Base):
    """quote_1m：按日 RANGE 分区；唯一键 (source, symbol, ts) 保证幂等。

    分区父表在数据库中没有主键（PostgreSQL 限制：主键必须包含分区键），
    唯一约束与 BRIN/普通索引都由迁移用原生 DDL 建立。模型里声明 id 为主键
    仅为满足 ORM 组装要求，插入走 Core（pg_insert），不会生成依赖主键的语句。
    """

    __tablename__ = "quote_1m"
    __table_args__ = {"postgresql_partition_by": "RANGE (ts)"}  # noqa: RUF012

    id: Mapped[int] = mapped_column(BigInteger, autoincrement=True, primary_key=True)
    market: Mapped[str] = mapped_column(String(1), nullable=False)  # 'D' 国内 / 'F' 外盘
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    variety_code: Mapped[str | None] = mapped_column(String(8))
    price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    prev_close: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    open: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    high: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    low: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    volume: Mapped[int | None] = mapped_column(BigInteger)
    oi: Mapped[int | None] = mapped_column(BigInteger)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
