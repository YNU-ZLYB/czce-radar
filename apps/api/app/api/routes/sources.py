"""数据源状态接口（只读）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_dependency
from czce_schema.sources import SourceListResponse, SourceStatus

router = APIRouter(prefix="/api/v1", tags=["sources"])

SessionDep = Annotated[AsyncSession, Depends(session_dependency)]

_QUERY = text("""
    SELECT s.code, s.name, s.kind, s.interval_sec, s.enabled,
           s.last_status, s.last_error, s.last_run_at,
           f.fetched AS last_fetched, f.inserted AS last_inserted
    FROM source s
    LEFT JOIN LATERAL (
        SELECT fetched, inserted
        FROM fetch_log fl
        WHERE fl.source_code = s.code
        ORDER BY fl.started_at DESC
        LIMIT 1
    ) f ON TRUE
    ORDER BY s.kind, s.code
""")


@router.get(
    "/sources",
    response_model=SourceListResponse,
    summary="数据源状态与最近成功时间",
)
async def list_sources(session: SessionDep) -> SourceListResponse:
    result = await session.execute(_QUERY)
    sources = [
        SourceStatus(
            code=row.code,
            name=row.name,
            kind=row.kind,
            interval_sec=row.interval_sec,
            enabled=row.enabled,
            last_status=row.last_status,
            last_error=row.last_error,
            last_run_at=row.last_run_at,
            last_fetched=row.last_fetched,
            last_inserted=row.last_inserted,
        )
        for row in result.all()
    ]
    return SourceListResponse(sources=sources)
