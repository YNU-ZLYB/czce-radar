"""FeatureSnapshot 构建器：严格 asof 的品种特征快照（版本 f1）。

结构见《02-数据与预测引擎》§4。三条铁律：
1. 快照内所有数据时间戳 ≤ asof（日线类统一 ``trade_date < asof 当日`` 的 UTC 日界，
   即只用昨日及更早 + 隔夜外盘，手工误写当日行也不泄漏）；
2. 缺失字段显式 null，绝不用 0 冒充；
3. display_only 永不进入模型（盘面动量、量能）。

外盘日线来源：foreign_daily（worker 08:10 CST 由 quote_1m 按日聚合写入）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.models.knowledge import FeatureSnapshot
from czce_schema.symbols import DOMESTIC_SINA_SYMBOLS

logger = logging.getLogger(__name__)

FEATURE_VERSION = "f1"

#: 构建器查询只依赖 ``.execute``，Session 与 Connection 均可
Conn = AsyncConnection | AsyncSession


def _f(x: Decimal | float | int | None, nd: int = 4) -> float | None:
    """Decimal → round float；None 原样返回（缺失显式 null）。"""
    if x is None:
        return None
    v = float(x)
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, nd)


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return _f((new / old - 1) * 100)


def _std(values: list[float]) -> float | None:
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def _percentile_rank(sorted_values: list[float], x: float) -> float | None:
    if not sorted_values:
        return None
    below = sum(1 for v in sorted_values if v < x)
    return round(below / len(sorted_values) * 100, 1)


async def _price_block(conn: Conn, code: str, day: dt.date) -> dict[str, Any]:
    # day 为排他上界（UTC 日界）：日线永远只取 asof 当日之前，手工误写当日行也不泄漏
    rows = (
        await conn.execute(
            text(
                "SELECT trade_date, close, volume FROM daily_bar"
                " WHERE variety_code = :c AND trade_date < :d"
                " ORDER BY trade_date DESC LIMIT 21"
            ),
            {"c": code, "d": day},
        )
    ).all()
    if not rows:
        return {
            "close": None,
            "ret_1d": None,
            "ret_5d": None,
            "vol_20d": None,
            "pos_20d": None,
            "volume_ratio": None,
        }
    closes: list[float] = [float(r.close) for r in rows]  # 最新在前
    volumes: list[float] = [float(r.volume) for r in rows if r.volume is not None]
    rets = [closes[i] / closes[i + 1] - 1 for i in range(len(closes) - 1)]
    window = closes[:20]
    std = _std(rets[:20])
    return {
        "close": _f(closes[0]),
        "ret_1d": _f(rets[0] * 100) if rets else None,
        "ret_5d": _f((closes[0] / closes[5] - 1) * 100) if len(closes) > 5 else None,
        "vol_20d": _f(std * 100) if std is not None and len(rets) >= 5 else None,
        "pos_20d": _f((closes[0] - min(window)) / (max(window) - min(window)), 3)
        if max(window) > min(window)
        else None,
        "volume_ratio": _f(volumes[0] / (sum(volumes[1:21]) / min(20, len(volumes) - 1)), 3)
        if len(volumes) > 5
        else None,
    }


async def _foreign_block(
    conn: Conn, ext_codes: Sequence[str], day: dt.date
) -> dict[str, dict[str, Any]]:
    if not ext_codes:
        return {}
    rows = (
        await conn.execute(
            text(
                "SELECT ext_code, close, change_pct FROM ("
                "  SELECT ext_code, close, change_pct,"
                "         row_number() OVER (PARTITION BY ext_code ORDER BY trade_date DESC) rn"
                "  FROM foreign_daily WHERE trade_date < :d AND ext_code = ANY(:codes)"
                ") t WHERE rn <= 3 ORDER BY ext_code, rn"
            ),
            {"d": day, "codes": list(ext_codes)},
        )
    ).all()
    by_ext: dict[str, list[Any]] = {}
    for r in rows:
        by_ext.setdefault(r.ext_code, []).append(r)
    out: dict[str, dict[str, Any]] = {}
    for ext, items in by_ext.items():
        closes = [float(i.close) for i in items]  # 最新在前
        out[ext] = {
            "chg_1d": _f(items[0].change_pct)
            if items[0].change_pct is not None
            else _pct(closes[0], closes[1])
            if len(closes) > 1
            else None,
            "chg_3d": _pct(closes[0], closes[2]) if len(closes) > 2 else None,
            "session": "prior",
        }
    return out


async def _edges(conn: Conn, code: str) -> Sequence[Row[Any]]:
    return (
        await conn.execute(
            text(
                "SELECT e.name, e.proxy_ext, e.prior, v.linkage_q"
                " FROM knowledge_edge e JOIN variety v ON v.code = e.variety_code"
                " WHERE e.variety_code = :c AND e.proxy_ext IS NOT NULL"
                " ORDER BY e.prior DESC"
            ),
            {"c": code},
        )
    ).all()


async def _industry_block(conn: Conn, code: str, day: dt.date) -> dict[str, float | None]:
    """仓单/库存/基差：无数据显式 null（仓单已日更；库存/基差暂无稳定源）。"""
    rows = (
        await conn.execute(
            text(
                "SELECT kind, value FROM ("
                "  SELECT kind, value,"
                "         row_number() OVER (PARTITION BY kind ORDER BY trade_date DESC) rn"
                "  FROM industry_metric WHERE variety_code = :c AND trade_date < :d"
                ") t WHERE rn <= 250 ORDER BY kind, rn"
            ),
            {"c": code, "d": day},
        )
    ).all()
    by_kind_raw: dict[str, list[Any]] = {}
    for r in rows:
        by_kind_raw.setdefault(r.kind, []).append(r.value)  # 最新在前
    by_kind: dict[str, list[float]] = {k: [float(v) for v in vs] for k, vs in by_kind_raw.items()}
    receipt = by_kind.get("warehouse_receipt", [])
    inventory = by_kind.get("inventory", [])
    basis = by_kind.get("basis", [])
    inv_sorted = sorted(float(v) for v in inventory)
    return {
        "receipt_chg_5d": _pct(receipt[0], receipt[5]) if len(receipt) > 5 else None,
        "inventory_pct": _percentile_rank(inv_sorted, inventory[0]) if inventory else None,
        "basis_pct": _f(basis[0], 4) if basis else None,
    }


async def _news_block(conn: Conn, code: str, day: dt.date) -> dict[str, Any]:
    """严格 lag1：只取 trade_date < 当日的聚合情绪（表由 M5 资讯模块填充）。"""
    row = (
        await conn.execute(
            text(
                "SELECT score, doc_count FROM news_sentiment_d"
                " WHERE variety_code = :c AND trade_date < :d"
                " ORDER BY trade_date DESC LIMIT 1"
            ),
            {"c": code, "d": day},
        )
    ).first()
    if row is None:
        return {"sentiment_lag1": None, "count_lag1": None}
    return {"sentiment_lag1": _f(row.score, 3), "count_lag1": int(row.doc_count)}


async def _display_block(conn: Conn, code: str, asof: dt.datetime) -> dict[str, Any]:
    """display_only：只给人看，永不入模。"""
    symbol = DOMESTIC_SINA_SYMBOLS.get(code)
    spark: list[float] = []
    if symbol is not None:
        rows = (
            await conn.execute(
                text(
                    "SELECT ts, price FROM quote_1m"
                    " WHERE symbol = :s AND ts <= :a AND ts > :a - interval '6 hours'"
                    " ORDER BY ts"
                ),
                {"s": symbol, "a": asof},
            )
        ).all()
        # 每个 10 分钟桶取最后一价，得到 ~36 点的 spark
        bucket: dict[dt.datetime, float] = {}
        for r in rows:
            key = r.ts.replace(second=0, microsecond=0, minute=(r.ts.minute // 10) * 10)
            bucket[key] = float(r.price)
        spark = [bucket[k] for k in sorted(bucket)]
    return {"spark_6h": spark}


async def build_snapshot(
    conn: Conn, code: str, asof: dt.datetime, version: str = FEATURE_VERSION
) -> dict[str, Any]:
    """构建单品种特征快照（features JSONB，不含 display）。"""
    asof = asof.astimezone(dt.UTC)
    day = asof.date()

    edges = await _edges(conn, code)
    ext_codes = [e.proxy_ext for e in edges]
    price, foreign, industry, news = await asyncio.gather(
        _price_block(conn, code, day),
        _foreign_block(conn, ext_codes, day),
        _industry_block(conn, code, day),
        _news_block(conn, code, day),
    )

    linkage = []
    for e in edges:
        chg = foreign.get(e.proxy_ext, {}).get("chg_1d")
        if chg is None:
            continue
        linkage.append(
            {
                "name": e.name,
                "ext": e.proxy_ext,
                "prior": _f(e.prior, 3),
                "q": _f(e.linkage_q, 3),
                "chg_1d": chg,
                "contrib": _f(float(chg) * float(e.prior) * float(e.linkage_q), 2),
            }
        )

    season_row = (
        await conn.execute(
            text(
                "SELECT stage, bias, confidence FROM seasonality"
                " WHERE variety_code = :c AND month = :m"
            ),
            {"c": code, "m": asof.month},
        )
    ).first()
    seasonality = {
        "month": asof.month,
        "bias": _f(season_row.bias) if season_row else None,
        "stage": season_row.stage if season_row else None,
        "confidence": _f(season_row.confidence, 2) if season_row else None,
    }

    display = await _display_block(conn, code, asof)
    display["volume_ratio"] = price.pop("volume_ratio")

    return {
        "features": {
            "version": version,
            "asof": asof.isoformat().replace("+00:00", "Z"),
            "price": price,
            "foreign": foreign,
            "linkage": linkage,
            "industry": industry,
            "seasonality": seasonality,
            "news": news,
            "event": {"is_event_day": False, "tags": []},
        },
        "display": display,
    }


async def persist_snapshot(
    conn: Conn,
    code: str,
    asof: dt.datetime,
    bundle: dict[str, Any],
    version: str = FEATURE_VERSION,
) -> None:
    """upsert 特征快照（variety+asof+version 唯一）。"""
    await conn.execute(
        pg_insert(FeatureSnapshot)
        .values(
            variety_code=code,
            asof=asof,
            version=version,
            features=bundle["features"],
            display=bundle["display"],
        )
        .on_conflict_do_update(
            constraint="uq_feature_snapshot",
            set_={
                "features": pg_insert(FeatureSnapshot).excluded.features,
                "display": pg_insert(FeatureSnapshot).excluded.display,
            },
        )
    )


async def build_varieties(
    session_factory: async_sessionmaker[AsyncSession],
    codes: Sequence[str],
    asof: dt.datetime | None = None,
) -> list[str]:
    """为一批品种构建并落库快照（worker 08:25 任务入口）。返回失败品种。"""
    asof = (asof or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    failed: list[str] = []
    for code in sorted(codes):
        try:
            async with session_factory() as session, session.begin():
                bundle = await build_snapshot(session, code, asof)
                await persist_snapshot(session, code, asof, bundle)
        except Exception:
            logger.exception("build_snapshot_failed code=%s asof=%s", code, asof)
            failed.append(code)
    return failed
