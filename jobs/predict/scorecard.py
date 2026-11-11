"""事后评分看板计算（与回测同口径），供 API 与每日快照 cron 共用。

口径：
- direction_accuracy = 方向命中数 / 非零方向且实际涨跌非零的样本数
- coverage = 非震荡方向样本数 / 总样本数
- brier = 三分类 Brier 均值
"""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from czce_schema.dashboard import (
    ScorecardDay,
    ScorecardDayItem,
    ScorecardOverall,
    ScorecardResponse,
    ScorecardVariety,
)


def _sign(change: float) -> int:
    return 1 if change > 0 else -1 if change < 0 else 0


def _day_accuracy(items: list[Any]) -> float | None:
    usable = [r for r in items if r.direction != 0 and float(r.actual_change) != 0]
    if not usable:
        return None
    hits = sum(1 for r in usable if r.direction == _sign(float(r.actual_change)))
    return round(hits / len(usable), 4)


async def _active_fusion_version(session: AsyncSession) -> str | None:
    return await session.scalar(
        text(
            "SELECT version FROM model_registry"
            " WHERE kind = 'fusion' AND status = 'active'"
            " ORDER BY created_at DESC LIMIT 1"
        )
    )


async def compute_scorecard(
    session: AsyncSession, days: int = 60
) -> ScorecardResponse:
    """计算滚动窗口事后评分看板（与回测同口径）。

    fusion active 时取 fusion 版本；否则取 rule-*。
    """
    fusion = await _active_fusion_version(session)
    since = dt.datetime.now(dt.UTC).date() - dt.timedelta(
        days=days * 2
    )  # 自然日冗余含非交易日
    mv_filter = "p.model_version = :mv" if fusion else "p.model_version LIKE 'rule-%'"
    params: dict[str, Any] = {"since": since, "mv": fusion}
    rows = list(
        (
            await session.execute(
                text(
                    "SELECT p.target_date, p.variety_code, v.name, p.direction, p.bucket,"
                    "       p.signal, o.actual_change, o.actual_bucket, o.hit_dir, o.brier"
                    " FROM outcome o"
                    " JOIN prediction p ON p.id = o.prediction_id"
                    " JOIN variety v ON v.code = p.variety_code"
                    f" WHERE p.target_date >= :since AND {mv_filter}"
                    " ORDER BY p.target_date, v.sort_order"
                ),
                params,
            )
        ).all()
    )

    usable_rows = [r for r in rows if r.direction != 0]
    overall = ScorecardOverall(
        n=len(rows),
        coverage=round(len(usable_rows) / len(rows), 4) if rows else 0.0,
        direction_n=sum(1 for r in usable_rows if float(r.actual_change) != 0),
        direction_accuracy=_day_accuracy(rows),
        brier=round(sum(float(r.brier) for r in rows) / len(rows), 6) if rows else None,
    )

    by_day_map: dict[dt.date, list[Any]] = defaultdict(list)
    by_variety_map: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        by_day_map[row.target_date].append(row)
        by_variety_map[row.variety_code].append(row)

    by_day = [
        ScorecardDay(date=day, n=len(items), direction_accuracy=_day_accuracy(items))
        for day, items in sorted(by_day_map.items())
    ]
    by_variety = [
        ScorecardVariety(
            variety_code=code,
            name=items[0].name,
            n=len(items),
            coverage=round(
                sum(1 for r in items if r.direction != 0) / len(items), 4
            ),
            direction_accuracy=_day_accuracy(items),
        )
        for code, items in sorted(by_variety_map.items())
    ]

    latest: ScorecardDay | None = None
    if by_day_map:
        day = max(by_day_map)
        items = by_day_map[day]
        latest = ScorecardDay(
            date=day,
            n=len(items),
            direction_accuracy=_day_accuracy(items),
            items=[
                ScorecardDayItem(
                    variety_code=r.variety_code,
                    name=r.name,
                    bucket=r.bucket,
                    signal=r.signal,
                    actual_change=float(r.actual_change),
                    actual_bucket=r.actual_bucket,
                    hit=bool(r.hit_dir),
                )
                for r in items
            ],
        )

    return ScorecardResponse(
        window_days=days,
        model_version=fusion or "rule-*",
        scored_through=max(by_day_map) if by_day_map else None,
        overall=overall,
        by_day=by_day,
        by_variety=by_variety,
        latest=latest,
    )


async def save_scorecard_snapshot(
    session: AsyncSession, card: ScorecardResponse, run_date: dt.date
) -> dict[str, Any]:
    """把一次 scorecard 计算结果落库到 scorecard_snapshot（幂等 upsert）。

    用于滚动命中率看板的时间序列与周报对比。返回写入行的关键字段。
    """
    overall = card.overall
    by_variety_payload = [
        {
            "variety_code": v.variety_code,
            "name": v.name,
            "n": v.n,
            "coverage": v.coverage,
            "direction_accuracy": v.direction_accuracy,
        }
        for v in card.by_variety
    ]
    # 失误归因：最近一日中方向错（或 actual_change 为 0 未命中）的品种
    top_misses: list[dict[str, Any]] = []
    if card.latest is not None:
        top_misses = [
            {
                "variety_code": it.variety_code,
                "name": it.name,
                "bucket": it.bucket,
                "signal": it.signal,
                "actual_change": it.actual_change,
                "actual_bucket": it.actual_bucket,
            }
            for it in card.latest.items
            if not it.hit
        ][:10]

    await session.execute(
        text(
            "INSERT INTO scorecard_snapshot"
            " (run_date, model_version, window_days, scored_through, n, coverage,"
            "  direction_n, direction_accuracy, brier, by_variety, top_misses)"
            " VALUES (:run_date, :mv, :wd, :st, :n, :cov, :dn, :da, :brier,"
            " CAST(:bv AS jsonb), CAST(:tm AS jsonb))"
            " ON CONFLICT (run_date, model_version) DO UPDATE SET"
            "   n = EXCLUDED.n, coverage = EXCLUDED.coverage,"
            "   direction_n = EXCLUDED.direction_n,"
            "   direction_accuracy = EXCLUDED.direction_accuracy,"
            "   brier = EXCLUDED.brier, by_variety = EXCLUDED.by_variety,"
            "   top_misses = EXCLUDED.top_misses, scored_through = EXCLUDED.scored_through"
        ),
        {
            "run_date": run_date,
            "mv": card.model_version,
            "wd": card.window_days,
            "st": card.scored_through,
            "n": overall.n,
            "cov": overall.coverage,
            "dn": overall.direction_n,
            "da": overall.direction_accuracy,
            "brier": overall.brier,
            "bv": json.dumps(by_variety_payload, ensure_ascii=False),
            "tm": json.dumps(top_misses, ensure_ascii=False),
        },
    )
    return {
        "run_date": run_date.isoformat(),
        "model_version": card.model_version,
        "n": overall.n,
        "direction_accuracy": overall.direction_accuracy,
        "brier": overall.brier,
    }
