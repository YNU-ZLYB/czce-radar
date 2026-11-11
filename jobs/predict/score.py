"""事后评分：预测到期后用真实收盘写 ``outcome``（幂等，可重复执行）。

口径（与 ``scripts/backtest.py`` 完全一致）：
- ``actual_change`` = 目标日收盘 / 前一交易日收盘 - 1（%）
- σ = 目标日**之前** 20 个日收益的标准差（严格无泄漏）
- ``actual_bucket``/``actual_dir`` 由 σ 自适应分档推出（``classify_bucket``）
- ``hit_dir``：方向预测（非震荡）按涨跌符号判定；震荡预测按「实际落在震荡带」判定
- ``brier``：三分类对 bucket 推出的 actual_dir 评分（同 ``metrics.brier_score``）

目标日收盘未入库（未来日期/停牌/采集未到）则跳过，留待下次执行。
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jobs.predict.rules import classify_bucket, direction_of

_SIGMA_WINDOW = 20
# target 收盘 + 前一交易日 + σ 窗口所需历史（窗口 20 个收益需 21 个收盘），多取冗余
_BARS_NEEDED = _SIGMA_WINDOW + 3


async def _unscored(session: AsyncSession, today: dt.date) -> list[Any]:
    rows = (
        await session.execute(
            text(
                "SELECT p.id, p.variety_code, p.target_date, p.direction,"
                "       p.p_up, p.p_flat, p.p_down"
                " FROM prediction p LEFT JOIN outcome o ON o.prediction_id = p.id"
                " WHERE o.prediction_id IS NULL AND p.target_date < :today"
                " ORDER BY p.target_date, p.id"
            ),
            {"today": today},
        )
    ).all()
    return list(rows)


async def _recent_closes(
    session: AsyncSession, variety_code: str, target: dt.date
) -> list[tuple[dt.date, float]]:
    rows = (
        await session.execute(
            text(
                "SELECT trade_date, close FROM daily_bar"
                " WHERE variety_code = :code AND trade_date <= :d"
                " ORDER BY trade_date DESC LIMIT :n"
            ),
            {"code": variety_code, "d": target, "n": _BARS_NEEDED},
        )
    ).all()
    return [(row.trade_date, float(row.close)) for row in reversed(rows)]


def _std(values: list[float]) -> float:
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def score_one(
    *,
    direction: int,
    p_up: float,
    p_flat: float,
    p_down: float,
    closes: list[tuple[dt.date, float]],
    target: dt.date,
) -> dict[str, Any] | None:
    """纯函数：由收盘序列计算一条 outcome；数据不足返回 ``None``。"""
    if not closes or closes[-1][0] != target:
        return None
    if len(closes) < _SIGMA_WINDOW + 2:
        return None
    prices = [c for _, c in closes]
    actual_change = (prices[-1] / prices[-2] - 1) * 100
    window = [prices[i] / prices[i - 1] - 1 for i in range(1, len(prices))]
    sigma = _std(window[-_SIGMA_WINDOW - 1 : -1]) * 100  # 目标日之前的 20 个收益
    actual_bucket = classify_bucket(actual_change, sigma)
    actual_dir = direction_of(actual_bucket)
    if direction != 0:
        sign = 1 if actual_change > 0 else -1 if actual_change < 0 else 0
        hit = direction == sign
    else:
        hit = actual_dir == 0
    onehot = (
        1.0 if actual_dir < 0 else 0.0,
        1.0 if actual_dir == 0 else 0.0,
        1.0 if actual_dir > 0 else 0.0,
    )
    brier = (p_down - onehot[0]) ** 2 + (p_flat - onehot[1]) ** 2 + (p_up - onehot[2]) ** 2
    return {
        "actual_change": round(actual_change, 4),
        "actual_dir": actual_dir,
        "actual_bucket": actual_bucket,
        "hit_dir": hit,
        "brier": round(brier, 6),
    }


async def score_outcomes(
    session: AsyncSession, *, today: dt.date | None = None
) -> dict[str, int]:
    """给所有已到期未评分的预测写 outcome；返回 {scored, skipped}。"""
    day = today or dt.datetime.now(dt.UTC).date()
    pending = await _unscored(session, day)
    scored = 0
    skipped = 0
    closes_cache: dict[tuple[str, dt.date], list[tuple[dt.date, float]]] = {}
    for row in pending:
        key = (row.variety_code, row.target_date)
        if key not in closes_cache:
            closes_cache[key] = await _recent_closes(session, row.variety_code, row.target_date)
        result = score_one(
            direction=int(row.direction),
            p_up=float(row.p_up),
            p_flat=float(row.p_flat),
            p_down=float(row.p_down),
            closes=closes_cache[key],
            target=row.target_date,
        )
        if result is None:
            skipped += 1
            continue
        await session.execute(
            text(
                "INSERT INTO outcome"
                " (prediction_id, actual_change, actual_dir, actual_bucket, hit_dir, brier)"
                " VALUES (:pid, :chg, :dir, :bucket, :hit, :brier)"
                " ON CONFLICT (prediction_id) DO NOTHING"
            ),
            {
                "pid": row.id,
                "chg": result["actual_change"],
                "dir": result["actual_dir"],
                "bucket": result["actual_bucket"],
                "hit": result["hit_dir"],
                "brier": result["brier"],
            },
        )
        scored += 1
    return {"scored": scored, "skipped": skipped}
