"""资讯修订查询 / SSE 推送 / LLM 成本统计。

只读数据库；SSE 用短轮询把 prediction_revision 新记录推给前端。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import get_session_factory, session_dependency
from czce_schema.dashboard import DocRef, ExtractItem, ExtractListResponse
from czce_schema.news import (
    LlmStatsResponse,
    RevisionItem,
    RevisionListResponse,
    TriggerDoc,
)
from jobs.news.revision import ALERT_THRESHOLD

router = APIRouter(prefix="/api/v1", tags=["news"])

SessionDep = Annotated[AsyncSession, Depends(session_dependency)]

_REVISION_QUERY = text(
    "SELECT r.id, r.prediction_id, p.variety_code, p.target_date, r.at,"
    "       r.prev_signal, r.new_signal, r.prev_direction, r.new_direction,"
    "       r.reason, r.is_surprise,"
    "       d.id AS doc_id, d.title AS doc_title, d.url AS doc_url,"
    "       d.source AS doc_source, d.published_at AS doc_published_at"
    " FROM prediction_revision r"
    " JOIN prediction p ON p.id = r.prediction_id"
    " LEFT JOIN news_doc d ON d.id = r.trigger_doc_id"
    " WHERE (CAST(:d AS date) IS NULL OR p.target_date = CAST(:d AS date))"
    "   AND (CAST(:code AS varchar) IS NULL OR p.variety_code = CAST(:code AS varchar))"
    "   AND r.id > :since"
    " ORDER BY r.id"
)


def _row_to_item(row: Any) -> RevisionItem:
    doc = None
    if row.doc_id is not None:
        doc = TriggerDoc(
            id=row.doc_id,
            title=row.doc_title,
            url=row.doc_url,
            source=row.doc_source,
            published_at=row.doc_published_at,
        )
    return RevisionItem(
        id=row.id,
        prediction_id=row.prediction_id,
        variety_code=row.variety_code,
        target_date=row.target_date,
        at=row.at,
        prev_signal=row.prev_signal,
        new_signal=row.new_signal,
        prev_direction=row.prev_direction,
        new_direction=row.new_direction,
        reason=row.reason,
        is_surprise=row.is_surprise,
        trigger_doc=doc,
    )


async def _revision_rows(
    session: AsyncSession,
    *,
    target_date: dt.date | None,
    variety: str | None,
    since: int = 0,
) -> list[Any]:
    rows = (
        await session.execute(
            _REVISION_QUERY, {"d": target_date, "code": variety, "since": since}
        )
    ).all()
    return list(rows)


@router.get(
    "/news/extracts",
    response_model=ExtractListResponse,
    summary="资讯结构化抽取列表（简报页；含原文跳转链接）",
)
async def list_extracts(
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=30)] = 2,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> ExtractListResponse:
    rows = (
        await session.execute(
            text(
                "SELECT e.id, e.variety_code, v.name AS variety_name, e.direction,"
                "       e.strength, e.chain_stage, e.is_surprise, e.method, e.evidence,"
                "       d.id AS doc_id, d.title AS doc_title, d.url AS doc_url,"
                "       d.source AS doc_source, d.published_at AS doc_published_at"
                " FROM news_extract e"
                " JOIN news_doc d ON d.id = e.doc_id"
                " JOIN variety v ON v.code = e.variety_code"
                " WHERE d.published_at >= now() - make_interval(days => :days)"
                " ORDER BY d.published_at DESC LIMIT :limit"
            ),
            {"days": days, "limit": limit},
        )
    ).all()
    items = [
        ExtractItem(
            id=row.id,
            variety_code=row.variety_code,
            variety_name=row.variety_name,
            direction=row.direction,
            strength=row.strength,
            chain_stage=row.chain_stage,
            is_surprise=row.is_surprise,
            method=row.method,
            evidence=row.evidence,
            doc=DocRef(
                id=row.doc_id,
                title=row.doc_title,
                url=row.doc_url,
                source=row.doc_source,
                published_at=row.doc_published_at,
            ),
        )
        for row in rows
    ]
    return ExtractListResponse(extracts=items)


@router.get(
    "/revisions",
    response_model=RevisionListResponse,
    summary="查询预测修订历史（含触发资讯原文链接与告警标记）",
)
async def list_revisions(
    session: SessionDep,
    target_date: Annotated[dt.date | None, Query(alias="date")] = None,
    variety: Annotated[str | None, Query(description="品种代码，如 MA")] = None,
) -> RevisionListResponse:
    rows = await _revision_rows(session, target_date=target_date, variety=variety)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.variety_code] = counts.get(row.variety_code, 0) + 1
    alerts = sorted(code for code, count in counts.items() if count > ALERT_THRESHOLD)
    return RevisionListResponse(
        revisions=[_row_to_item(row) for row in rows], alerts=alerts
    )


async def _event_stream(request: Request, since_id: int) -> AsyncIterator[str]:
    last = since_id
    idle = 0
    yield ": connected\n\n"
    while not await request.is_disconnected():
        factory = get_session_factory()
        async with factory() as session:
            rows = await _revision_rows(session, target_date=None, variety=None, since=last)
        if rows:
            idle = 0
            for row in rows:
                last = max(last, int(row.id))
                payload = _row_to_item(row).model_dump(mode="json")
                data = json.dumps(payload, ensure_ascii=False)
                yield f"id: {last}\nevent: revision\ndata: {data}\n\n"
        else:
            idle += 1
            if idle % 5 == 1:
                yield ": keep-alive\n\n"
        await asyncio.sleep(3)


@router.get("/stream/revisions", summary="SSE：订阅预测修订（断线重连后自动补齐）")
async def stream_revisions(
    request: Request,
    since_id: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    return StreamingResponse(
        _event_stream(request, since_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _settings_dep() -> Settings:
    from app.core.config import load_settings

    return load_settings()


@router.get(
    "/admin/llm-stats",
    response_model=LlmStatsResponse,
    summary="LLM 当日调用成本与缓存命中率（成本治理，需 X-API-Token）",
)
async def llm_stats(
    session: SessionDep,
    settings: Annotated[Settings, Depends(_settings_dep)],
    target_date: Annotated[dt.date | None, Query(alias="date")] = None,
    x_api_token: Annotated[str | None, Header()] = None,
) -> LlmStatsResponse:
    if x_api_token != settings.api_token:
        raise HTTPException(status_code=401, detail="无效的管理 Token")
    day = target_date or dt.datetime.now(dt.UTC).date()
    row = (
        await session.execute(
            text(
                "SELECT count(*) AS calls, count(*) FILTER (WHERE cache_hit) AS hits,"
                "       COALESCE(sum(cost_cny), 0) AS cost"
                " FROM llm_call_log"
                " WHERE (created_at AT TIME ZONE 'Asia/Shanghai')::date = :d"
            ),
            {"d": day},
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=500, detail="统计查询失败")
    calls = int(row.calls or 0)
    hits = int(row.hits or 0)
    cost = float(row.cost or 0)
    return LlmStatsResponse(
        date=day,
        calls=calls,
        cache_hits=hits,
        cache_hit_rate=round(hits / calls, 3) if calls else 0.0,
        cost_cny=round(cost, 4),
        budget_cny=settings.llm_daily_budget_cny,
        degraded=cost >= settings.llm_daily_budget_cny,
        llm_enabled=bool(settings.llm_api_key),
    )
