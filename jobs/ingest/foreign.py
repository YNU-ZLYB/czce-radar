"""外盘日线聚合：quote_1m（分钟）→ foreign_daily（按 UTC 日收盘）。

外盘日线是传导特征（chg_1d/chg_3d）与回测的权威数据源。收盘定义：
该 UTC 日内 quote_1m 的最后一笔价。change_pct 相对「此前最近一个有行情的 UTC 日」。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app.models.market import ForeignDaily
from czce_schema.symbols import FOREIGN_SYMBOLS

SOURCE = "sina_quotes"
_LOOKBACK = "10 days"  # 跨周末/假日的上一收盘回看窗口


async def _last_closes(
    conn: AsyncConnection, start_excl: dt.datetime, end_excl: dt.datetime
) -> dict[str, Decimal]:
    rows = (
        await conn.execute(
            text(
                "SELECT symbol, (array_agg(price ORDER BY ts DESC))[1] AS close"
                " FROM quote_1m"
                " WHERE market = 'F' AND ts >= :a AND ts < :b"
                " GROUP BY symbol"
            ),
            {"a": start_excl, "b": end_excl},
        )
    ).all()
    return {r.symbol: Decimal(str(r.close)) for r in rows}


def _valid_ext(symbol: str) -> bool:
    return symbol in FOREIGN_SYMBOLS


async def aggregate_foreign_day(conn: AsyncConnection, day: dt.date) -> int:
    """聚合单个 UTC 日。返回 upsert 行数。幂等：重复执行只覆盖同日值。"""
    day_start = dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC)
    day_end = day_start + dt.timedelta(days=1)
    closes = await _last_closes(conn, day_start, day_end)
    if not closes:
        return 0

    prev = await _last_closes(conn, day_start - dt.timedelta(days=10), day_start)

    rows: list[dict[str, object]] = []
    for symbol, close in sorted(closes.items()):
        if not _valid_ext(symbol):
            continue
        prev_close = prev.get(symbol)
        change = (
            round((float(close) / float(prev_close) - 1) * 100, 4)
            if prev_close and prev_close != 0
            else None
        )
        try:
            rows.append(
                {
                    "ext_code": symbol,
                    "trade_date": day,
                    "close": close,
                    "change_pct": change,
                    "source": SOURCE,
                }
            )
        except (InvalidOperation, ValueError):  # pragma: no cover - 防脏价
            continue
    if not rows:
        return 0
    await conn.execute(
        pg_insert(ForeignDaily)
        .values(rows)
        .on_conflict_do_update(
            constraint="pk_foreign_daily",
            set_={
                "close": pg_insert(ForeignDaily).excluded.close,
                "change_pct": pg_insert(ForeignDaily).excluded.change_pct,
                "source": pg_insert(ForeignDaily).excluded.source,
            },
        )
    )
    return len(rows)


async def aggregate_foreign_range(conn: AsyncConnection, start: dt.date, end: dt.date) -> int:
    """闭区间 [start, end] 逐日聚合。"""
    total = 0
    day = start
    while day <= end:
        total += await aggregate_foreign_day(conn, day)
        day += dt.timedelta(days=1)
    return total


def _series_rows(items: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        payload = item.payload or {}
        day = payload.get("trade_date")
        if not isinstance(day, dt.date):
            continue
        rows.append(
            {
                "ext_code": payload["ext_code"],
                "trade_date": day,
                "close": payload["close"],
                "source": item.source,
            }
        )
    return rows


async def _recompute_change_pct(conn: AsyncConnection, ext_code: str) -> int:
    """按整条序列重算 change_pct（首行留 null），保证增量采集后历史值也正确。"""
    rows = (
        await conn.execute(
            text(
                "SELECT trade_date, close FROM foreign_daily"
                " WHERE ext_code = :c ORDER BY trade_date"
            ),
            {"c": ext_code},
        )
    ).all()
    updates: list[tuple[dt.date, float]] = []
    prev: float | None = None
    for row in rows:
        close = float(row.close)
        if prev is not None and prev != 0:
            updates.append((row.trade_date, round((close / prev - 1) * 100, 4)))
        prev = close

    for start in range(0, len(updates), 500):
        chunk = updates[start : start + 500]
        values_sql = ", ".join(f"(:d{i}, :p{i})" for i in range(len(chunk)))
        params: dict[str, Any] = {"c": ext_code}
        for i, (day, pct) in enumerate(chunk):
            # VALUES 的 d/p 都是文本列（SQL 侧统一 v.d::date / v.p::numeric）
            params[f"d{i}"] = day.isoformat()
            params[f"p{i}"] = f"{pct}"
        await conn.execute(
            text(
                "UPDATE foreign_daily AS f SET change_pct = v.p::numeric"
                f" FROM (VALUES {values_sql}) AS v(d, p)"
                " WHERE f.ext_code = :c AND f.trade_date = v.d::date"
            ),
            params,
        )
    return len(rows)


async def upsert_fred_series(conn: AsyncConnection, items: list[Any]) -> int:
    """FRED 日线：整段 upsert 后重算涨跌幅（首行 null，其余相对前一交易日）。"""
    rows = _series_rows(items)
    if not rows:
        return 0
    await conn.execute(
        pg_insert(ForeignDaily)
        .values(rows)
        .on_conflict_do_update(
            constraint="pk_foreign_daily",
            set_={
                "close": pg_insert(ForeignDaily).excluded.close,
                "source": pg_insert(ForeignDaily).excluded.source,
            },
        )
    )
    await _recompute_change_pct(conn, rows[0]["ext_code"])
    return len(rows)
