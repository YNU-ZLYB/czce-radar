"""产业指标入库（仓单/库存/基差）。"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app.models.knowledge import IndustryMetric
from jobs.collectors.base import RawItem

SOURCE = "czce"


def receipt_rows(items: list[RawItem]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        payload = item.payload or {}
        day = payload.get("trade_date")
        if not isinstance(day, dt.date):
            continue
        rows.append(
            {
                "variety_code": payload["code"],
                "trade_date": day,
                "kind": "warehouse_receipt",
                "value": payload["receipt"],
                "unit": "张",
                "source": SOURCE,
            }
        )
    return rows


async def upsert_receipts(conn: AsyncConnection, items: list[RawItem]) -> int:
    """幂等写入仓单数量（同日重复运行只覆盖数值）。"""
    rows = receipt_rows(items)
    if not rows:
        return 0
    await conn.execute(
        pg_insert(IndustryMetric)
        .values(rows)
        .on_conflict_do_update(
            constraint="pk_industry_metric",
            set_={
                "value": pg_insert(IndustryMetric).excluded.value,
                "unit": pg_insert(IndustryMetric).excluded.unit,
                "source": pg_insert(IndustryMetric).excluded.source,
            },
        )
    )
    return len(rows)
