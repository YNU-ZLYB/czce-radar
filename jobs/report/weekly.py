"""周报：本周模型表现 vs 上周，是否触发重训/回滚。

数据来源：scorecard_snapshot 表（每日 15:30 CST 由 scorecard_daily 写入）。
比较最近一个快照（本周）与至少 7 天前的快照（上周）。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _recommendation(
    acc_delta: float | None,
    brier_delta: float | None,
    brier_this: float | None,
) -> str:
    """简单规则：Brier 超 0.25 或命中率环比下降 >0.05 → 建议重训；否则持有。"""
    if brier_this is not None and brier_this > 0.25:
        return "retrain"
    if acc_delta is not None and acc_delta < -0.05:
        return "retrain"
    if brier_delta is not None and brier_delta > 0.03:
        return "retrain"
    return "hold"


async def generate_weekly_report(session: AsyncSession) -> dict[str, Any]:
    """生成周报：取最近快照与至少 7 天前的快照对比。

    返回 dict（供日志 / API 输出），包含本周/上周指标与建议。
    """
    rows = (
        await session.execute(
            text(
                "SELECT run_date, model_version, n, coverage, direction_n,"
                "       direction_accuracy, brier, scored_through"
                " FROM scorecard_snapshot"
                " ORDER BY run_date DESC LIMIT 14"
            )
        )
    ).all()
    if not rows:
        return {"status": "no_data", "recommendation": "hold"}

    this_week = rows[0]
    last_week = None
    cutoff = this_week.run_date - dt.timedelta(days=7)
    for r in rows[1:]:
        if r.run_date <= cutoff:
            last_week = r
            break

    acc_this = (
        float(this_week.direction_accuracy)
        if this_week.direction_accuracy is not None
        else None
    )
    acc_last = (
        float(last_week.direction_accuracy)
        if last_week is not None and last_week.direction_accuracy is not None
        else None
    )
    brier_this = (
        float(this_week.brier) if this_week.brier is not None else None
    )
    brier_last = (
        float(last_week.brier)
        if last_week is not None and last_week.brier is not None
        else None
    )

    acc_delta = (
        acc_this - acc_last if (acc_this is not None and acc_last is not None) else None
    )
    brier_delta = (
        brier_this - brier_last
        if (brier_this is not None and brier_last is not None)
        else None
    )

    recommendation = _recommendation(acc_delta, brier_delta, brier_this)

    return {
        "this_week": {
            "run_date": this_week.run_date.isoformat(),
            "model_version": this_week.model_version,
            "n": int(this_week.n),
            "coverage": float(this_week.coverage),
            "direction_accuracy": acc_this,
            "brier": brier_this,
        },
        "last_week": (
            {
                "run_date": last_week.run_date.isoformat(),
                "model_version": last_week.model_version,
                "n": int(last_week.n),
                "coverage": float(last_week.coverage),
                "direction_accuracy": acc_last,
                "brier": brier_last,
            }
            if last_week is not None
            else None
        ),
        "delta": {
            "direction_accuracy": acc_delta,
            "brier": brier_delta,
        },
        "recommendation": recommendation,
    }
