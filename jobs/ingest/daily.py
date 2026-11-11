"""日线入库（国内 daily_bar），回补与日常更新共用。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app.models.market import DailyBar
from jobs.collectors.base import RawItem

_CHUNK = 2000  # asyncpg 单语句参数上限 32767 ÷ 10 列，留余量


def _d(raw: Any) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def _i(raw: Any) -> int | None:
    value = _d(raw)
    return int(value) if value is not None else None


def normalize_daily_bar(variety_code: str, item: RawItem) -> dict[str, Any] | None:
    payload = item.payload
    open_, high, low, close = (_d(payload.get(k)) for k in ("open", "high", "low", "close"))
    if None in (open_, high, low, close):
        return None
    assert open_ is not None and high is not None and low is not None and close is not None
    try:
        trade_date = dt.date.fromisoformat(str(item.external_id))
    except ValueError:
        return None
    return {
        "variety_code": variety_code,
        "trade_date": trade_date,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "settle": _d(payload.get("settle")),
        "volume": _i(payload.get("volume")),
        "oi": _i(payload.get("oi")),
        "source": item.source,
    }


async def upsert_daily_bars(conn: AsyncConnection, rows: list[dict[str, Any]]) -> int:
    """日线回补允许修正历史，用 DO UPDATE；幂等（同样数据重复执行结果不变）。

    全历史一次拉取可达数千行，asyncpg 单条语句参数上限 32767，
    因此按 2000 行分批。
    """
    if not rows:
        return 0
    inserted = 0
    for start in range(0, len(rows), _CHUNK):
        batch = rows[start : start + _CHUNK]
        stmt = pg_insert(DailyBar).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["variety_code", "trade_date"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "settle": stmt.excluded.settle,
                "volume": stmt.excluded.volume,
                "oi": stmt.excluded.oi,
                "source": stmt.excluded.source,
            },
        )
        result = await conn.execute(stmt)
        inserted += int(result.rowcount or 0)
    return inserted
