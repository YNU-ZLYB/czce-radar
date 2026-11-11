"""模型注册表管理接口（admin，需 X-API-Token）。

- GET  /api/v1/admin/models            版本列表（指标、状态）
- POST /api/v1/admin/models/{version}/activate   切换/回滚：立即生效（引擎每次预测实时读 registry）
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import session_dependency
from jobs.predict.fusion import activate_model

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

SessionDep = Annotated[AsyncSession, Depends(session_dependency)]


def _settings_dep() -> Settings:
    from app.core.config import load_settings

    return load_settings()


async def require_token(
    settings: Annotated[Settings, Depends(_settings_dep)],
    x_api_token: Annotated[str | None, Header()] = None,
) -> None:
    if x_api_token != settings.api_token:
        raise HTTPException(status_code=401, detail="无效的管理 Token")


@router.get("/models", dependencies=[Depends(require_token)])
async def list_models(session: SessionDep) -> dict[str, Any]:
    rows = (
        await session.execute(
            text(
                "SELECT version, kind, status, train_start, train_end, metrics, created_at"
                " FROM model_registry ORDER BY created_at DESC"
            )
        )
    ).all()
    return {
        "models": [
            {
                "version": r.version,
                "kind": r.kind,
                "status": r.status,
                "train_start": r.train_start.isoformat() if r.train_start else None,
                "train_end": r.train_end.isoformat() if r.train_end else None,
                "metrics": r.metrics,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/models/{version}/importance", dependencies=[Depends(require_token)])
async def model_importance(version: str, session: SessionDep) -> dict[str, Any]:
    """导出特征重要性（gain）；display_only 字段保证不在其中（训练侧硬断言）。"""
    params = (
        await session.execute(
            text("SELECT params FROM model_registry WHERE version = :v"), {"v": version}
        )
    ).scalar()
    if params is None:
        raise HTTPException(status_code=404, detail=f"版本不存在：{version}")
    return {"version": version, "importance": (params or {}).get("importance", {})}


@router.post("/models/{version}/activate", dependencies=[Depends(require_token)])
async def activate(version: str, session: SessionDep) -> dict[str, Any]:
    """切换 active 版本（其余 fusion/gbm 自动 retired）；回滚 = 对旧版本再调一次。"""
    async with session.begin():
        row = await activate_model(session, version)
    if row is None:
        raise HTTPException(status_code=404, detail=f"版本不存在：{version}")
    return {"version": row.version, "status": row.status}
