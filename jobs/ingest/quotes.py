"""行情入库：归一化 + 质量校验 + 幂等写入 + 分区保障。

幂等保证：唯一键 (source, symbol, ts) + ON CONFLICT DO NOTHING，
同一批原始数据执行两遍，数据库行数不变。
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app.models.quote import Quote1m
from jobs.collectors.base import RawItem

logger = logging.getLogger(__name__)

JUMP_THRESHOLD = Decimal("0.15")  # 与昨结算偏离 >15% 告警（不丢弃）


def _to_decimal(raw: Any) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def _to_int(raw: Any) -> int | None:
    value = _to_decimal(raw)
    return int(value) if value is not None else None


def normalize_quote(item: RawItem) -> dict[str, Any] | None:
    """RawItem → quote_1m 行；非法行返回 None（记数，不中断）。"""
    payload = item.payload
    market = payload.get("market")
    if market not in ("D", "F") or item.ts is None:
        return None
    price = _to_decimal(payload.get("price"))
    if price is None or price <= 0:
        return None
    prev_close = _to_decimal(payload.get("prev_close"))
    if prev_close is not None and prev_close > 0:
        change = abs(price - prev_close) / prev_close
        if change > JUMP_THRESHOLD:
            logger.warning(
                "quote_jump symbol=%s price=%s prev=%s change=%.1f%%",
                item.external_id,
                price,
                prev_close,
                float(change) * 100,
            )
    return {
        "market": market,
        "symbol": item.external_id,
        "variety_code": payload.get("variety_code"),
        "price": price,
        "prev_close": prev_close,
        "open": _to_decimal(payload.get("open")),
        "high": _to_decimal(payload.get("high")),
        "low": _to_decimal(payload.get("low")),
        "volume": _to_int(payload.get("volume")),
        "oi": _to_int(payload.get("oi")),
        "ts": item.ts,
        "source": item.source,
    }


def partition_dates(rows: list[dict[str, Any]]) -> list[dt.date]:
    return sorted({row["ts"].astimezone(dt.UTC).date() for row in rows})


async def ensure_partitions(conn: AsyncConnection, dates: list[dt.date]) -> None:
    for day in dates:
        await conn.execute(text("SELECT czce_ensure_quote_partition(:d)"), {"d": day})


async def ingest_quotes(conn: AsyncConnection, items: list[RawItem]) -> int:
    """把采集到的行情写入 quote_1m；返回新插入行数。"""
    rows = [row for row in (normalize_quote(item) for item in items) if row is not None]
    if not rows:
        return 0
    await ensure_partitions(conn, partition_dates(rows))
    stmt = (
        pg_insert(Quote1m)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["source", "symbol", "ts"])
    )
    result = await conn.execute(stmt)
    return int(result.rowcount or 0)
