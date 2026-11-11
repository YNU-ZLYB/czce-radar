"""预测查询接口（只读）。"""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_dependency
from czce_schema.predictions import PredictionItem, PredictionListResponse

router = APIRouter(prefix="/api/v1", tags=["predictions"])

SessionDep = Annotated[AsyncSession, Depends(session_dependency)]

_QUERY = text(
    "SELECT p.variety_code, p.target_date, p.asof, p.direction, p.bucket,"
    "       p.p_up, p.p_flat, p.p_down, p.exp_change, p.signal, p.confidence,"
    "       p.contributor::text AS contributor, p.model_version, p.narrative"
    " FROM prediction p JOIN variety v ON v.code = p.variety_code"
    " WHERE p.target_date = :d"
    "   AND (CAST(:code AS varchar) IS NULL OR p.variety_code = CAST(:code AS varchar))"
    "   AND (CAST(:mv AS varchar) IS NULL OR p.model_version = CAST(:mv AS varchar))"
    " ORDER BY v.sort_order, p.variety_code"
)


def _row_to_item(row: Any) -> PredictionItem:
    raw = row.contributor
    contributor = json.loads(raw) if isinstance(raw, str) else raw or []
    return PredictionItem(
        variety_code=row.variety_code,
        target_date=row.target_date,
        asof=row.asof,
        direction=row.direction,
        bucket=row.bucket,
        p_up=float(row.p_up),
        p_flat=float(row.p_flat),
        p_down=float(row.p_down),
        exp_change=float(row.exp_change) if row.exp_change is not None else None,
        signal=row.signal,
        confidence=float(row.confidence),
        contributor=contributor,
        model_version=row.model_version,
        narrative=row.narrative,
    )


@router.get(
    "/predictions",
    response_model=PredictionListResponse,
    summary="按目标日查询预测（不传 date 则取最新一天；有 active 模型时只返回该版本）",
)
async def list_predictions(
    session: SessionDep,
    target_date: Annotated[dt.date | None, Query(alias="date")] = None,
    variety: Annotated[str | None, Query(description="品种代码，如 MA")] = None,
) -> PredictionListResponse:
    if target_date is None:
        target_date = await session.scalar(text("SELECT max(target_date) FROM prediction"))
    if target_date is None:
        return PredictionListResponse(predictions=[])

    # 融合模型上线后只展示 active 版本；否则展示当日规则版本（shadow 并行产出不展示，§5.4）
    active = await session.scalar(
        text(
            "SELECT version FROM model_registry"
            " WHERE kind = 'fusion' AND status = 'active'"
            " ORDER BY created_at DESC LIMIT 1"
        )
    )
    model_version = active or f"rule-{target_date:%Y%m%d}-01"
    rows = (
        await session.execute(_QUERY, {"d": target_date, "code": variety, "mv": model_version})
    ).all()
    return PredictionListResponse(predictions=[_row_to_item(row) for row in rows])
