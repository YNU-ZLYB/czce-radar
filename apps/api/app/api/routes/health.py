"""系统探针：/live 给平台，/ready 给业务与前端。"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import session_dependency
from app.core.redis_client import redis_dependency
from app.services.health import build_health, is_ready
from czce_schema.health import HealthResponse, LiveResponse

router = APIRouter(tags=["system"])

SessionDep = Annotated[AsyncSession, Depends(session_dependency)]
RedisDep = Annotated[Redis, Depends(redis_dependency)]


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get(
    "/live",
    response_model=LiveResponse,
    summary="存活探针",
    description="不触碰数据库与 Redis，进程能响应即返回 200，供平台健康检查使用。",
)
async def live(settings: SettingsDep) -> LiveResponse:
    return LiveResponse(
        status="alive",
        version=settings.version,
        time=dt.datetime.now(dt.UTC),
    )


@router.get(
    "/ready",
    response_model=HealthResponse,
    summary="就绪状态（db / redis / worker 三态）",
    responses={503: {"description": "数据库或 Redis 不可用，API 当前无法对外服务"}},
)
async def ready(
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> JSONResponse:
    report = await build_health(
        session,
        redis,
        version=settings.version,
        worker_ttl_sec=settings.worker_heartbeat_ttl_sec,
    )
    status_code = 200 if is_ready(report) else 503
    return JSONResponse(status_code=status_code, content=report.model_dump(mode="json"))
