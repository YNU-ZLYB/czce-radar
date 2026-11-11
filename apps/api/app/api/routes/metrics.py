"""/metrics：Prometheus 文本格式，关键看板指标。

只读数据库；所有指标为服务端计算快照（非实时 push），适合 Prometheus 每 30~60s 拉取。
指标前缀统一 `czce_`，避免与其他服务冲突。
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, load_settings
from app.core.db import session_dependency
from czce_schema.constants import WORKER_HEARTBEAT_KEY

router = APIRouter(tags=["system"])

SessionDep = Annotated[AsyncSession, Depends(session_dependency)]


def _settings_dep() -> Settings:
    return load_settings()


SettingsDep = Annotated[Settings, Depends(_settings_dep)]


def _fmt(value: float | int | None) -> str:
    if value is None:
        return "NaN"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


async def _fetch_metrics(session: AsyncSession) -> list[str]:
    """采集成功率：每个源最近 24h 的 ok/error 计数与成功率。"""
    rows = (
        await session.execute(
            text(
                "SELECT source_code, status, count(*) AS c"
                " FROM fetch_log"
                " WHERE started_at >= now() - interval '24 hours'"
                " GROUP BY source_code, status"
                " ORDER BY source_code, status"
            )
        )
    ).all()
    totals: dict[str, int] = {}
    oks: dict[str, int] = {}
    for r in rows:
        totals[r.source_code] = totals.get(r.source_code, 0) + int(r.c)
        if r.status == "ok":
            oks[r.source_code] = int(r.c)

    lines = [
        "# HELP czce_fetch_total 最近 24h 采集次数（按源与状态）",
        "# TYPE czce_fetch_total counter",
    ]
    for r in rows:
        lines.append(
            f'czce_fetch_total{{source="{r.source_code}",status="{r.status}"}} {int(r.c)}'
        )

    lines += [
        "# HELP czce_fetch_success_rate 最近 24h 采集成功率（ok / total）",
        "# TYPE czce_fetch_success_rate gauge",
    ]
    for source, total in sorted(totals.items()):
        rate = oks.get(source, 0) / total if total else 0.0
        lines.append(f'czce_fetch_success_rate{{source="{source}"}} {rate:.6f}')
    return lines


async def _prediction_metrics(session: AsyncSession) -> list[str]:
    """今日预测产出数（按目标日）。"""
    today = dt.datetime.now(dt.UTC).date()
    rows = (
        await session.execute(
            text(
                "SELECT target_date, count(*) AS c FROM prediction"
                " WHERE target_date >= :d GROUP BY target_date ORDER BY target_date DESC LIMIT 7"
            ),
            {"d": today - dt.timedelta(days=6)},
        )
    ).all()
    lines = [
        "# HELP czce_prediction_count 每日预测产出数（按目标日）",
        "# TYPE czce_prediction_count gauge",
    ]
    for r in rows:
        lines.append(f'czce_prediction_count{{date="{r.target_date}"}} {int(r.c)}')
    return lines


async def _outcome_metrics(session: AsyncSession) -> list[str]:
    """滚动 60d 方向命中率与 Brier。"""
    since = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=120)
    row = (
        await session.execute(
            text(
                "SELECT count(*) AS n,"
                " count(*) FILTER (WHERE p.direction <> 0 AND o.actual_change <> 0"
                "   AND p.direction = sign(o.actual_change)) AS hits,"
                " count(*) FILTER (WHERE p.direction <> 0) AS dir_n,"
                " avg(o.brier) AS brier"
                " FROM outcome o JOIN prediction p ON p.id = o.prediction_id"
                " WHERE p.target_date >= :since"
            ),
            {"since": since},
        )
    ).first()
    n = int(row.n or 0) if row is not None else 0
    dir_n = int(row.dir_n or 0) if row is not None else 0
    hits = int(row.hits or 0) if row is not None else 0
    acc = hits / dir_n if dir_n else None
    brier = float(row.brier) if (row is not None and row.brier is not None) else None
    return [
        "# HELP czce_outcome_direction_accuracy 滚动 60d 方向命中率",
        "# TYPE czce_outcome_direction_accuracy gauge",
        f"czce_outcome_direction_accuracy {_fmt(acc)}",
        "# HELP czce_outcome_brier 滚动 60d 三分类 Brier",
        "# TYPE czce_outcome_brier gauge",
        f"czce_outcome_brier {_fmt(brier)}",
        "# HELP czce_outcome_n 滚动 60d 已评分样本数",
        "# TYPE czce_outcome_n gauge",
        f"czce_outcome_n {n}",
    ]


async def _llm_metrics(session: AsyncSession) -> list[str]:
    """当日 LLM 成本与缓存命中率。"""
    today = dt.datetime.now(dt.UTC).date()
    row = (
        await session.execute(
            text(
                "SELECT count(*) AS calls, count(*) FILTER (WHERE cache_hit) AS hits,"
                " COALESCE(sum(cost_cny), 0) AS cost"
                " FROM llm_call_log"
                " WHERE (created_at AT TIME ZONE 'Asia/Shanghai')::date = :d"
            ),
            {"d": today},
        )
    ).first()
    calls = int(row.calls or 0) if row is not None else 0
    hits = int(row.hits or 0) if row is not None else 0
    cost = float(row.cost or 0) if row is not None else 0.0
    rate = hits / calls if calls else 0.0
    return [
        "# HELP czce_llm_calls_total 当日 LLM 调用次数",
        "# TYPE czce_llm_calls_total counter",
        f"czce_llm_calls_total {calls}",
        "# HELP czce_llm_cost_cny_total 当日 LLM 成本（元）",
        "# TYPE czce_llm_cost_cny_total counter",
        f'czce_llm_cost_cny_total{{date="{today}"}} {cost:.6f}',
        "# HELP czce_llm_cache_hit_rate 当日 LLM 缓存命中率",
        "# TYPE czce_llm_cache_hit_rate gauge",
        f"czce_llm_cache_hit_rate {rate:.6f}",
    ]


async def _model_metrics(session: AsyncSession) -> list[str]:
    """当前 active 模型版本（多版本时只有一个为 1）。"""
    rows = (
        await session.execute(
            text(
                "SELECT version, kind, status FROM model_registry"
                " WHERE kind IN ('fusion', 'gbm', 'rule')"
                " ORDER BY created_at DESC"
            )
        )
    ).all()
    lines = [
        "# HELP czce_model_registry 模型注册表状态（active=1 / shadow=0.5 / retired=0）",
        "# TYPE czce_model_registry gauge",
    ]
    status_value = {"active": 1, "shadow": 0.5, "retired": 0}
    for r in rows:
        val = status_value.get(r.status, 0)
        metric = (
            f'czce_model_registry{{version="{r.version}",kind="{r.kind}",'
            f'status="{r.status}"}} {val}'
        )
        lines.append(metric)
    return lines


async def _heartbeat_metric(session: AsyncSession) -> list[str]:
    """worker 心跳距今年龄（秒）；Redis 不可用时返回 NaN。"""
    try:
        from app.core.redis_client import get_redis_client

        redis = get_redis_client()
        raw = await redis.get(WORKER_HEARTBEAT_KEY)
    except Exception:
        return [
            "# HELP czce_worker_heartbeat_age_seconds worker 心跳距今年龄（秒）",
            "# TYPE czce_worker_heartbeat_age_seconds gauge",
            "czce_worker_heartbeat_age_seconds NaN",
        ]
    if raw is None:
        age: float | None = None
    else:
        try:
            age = (
                dt.datetime.now(dt.UTC)
                - dt.datetime.fromtimestamp(float(raw), tz=dt.UTC)
            ).total_seconds()
        except (ValueError, TypeError):
            age = None
    return [
        "# HELP czce_worker_heartbeat_age_seconds worker 心跳距今年龄（秒）",
        "# TYPE czce_worker_heartbeat_age_seconds gauge",
        f"czce_worker_heartbeat_age_seconds {_fmt(age)}",
    ]


@router.get("/metrics", summary="Prometheus 指标（采集/预测/LLM/命中率/模型/心跳）")
async def metrics(
    session: SessionDep,
    settings: SettingsDep,
) -> PlainTextResponse:
    parts: list[str] = []
    parts.append(f"# czce_radar metrics (env={settings.app_env}, version={settings.version})")
    parts.extend(await _fetch_metrics(session))
    parts.extend(await _prediction_metrics(session))
    parts.extend(await _outcome_metrics(session))
    parts.extend(await _llm_metrics(session))
    parts.extend(await _model_metrics(session))
    parts.extend(await _heartbeat_metric(session))
    body = "\n".join(parts) + "\n"
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4; charset=utf-8")
