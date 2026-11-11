"""FastAPI 应用入口。

职责边界：只读数据库、参数校验、SSE 广播；不写业务表、不跑采集、不调 LLM。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from app.api.routes import (
    admin_models,
    dashboard,
    health,
    metrics,
    news,
    predictions,
    sources,
)
from app.core.config import Settings, load_settings
from app.core.db import dispose_db, init_db
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import TraceIdMiddleware
from app.core.redis_client import close_redis, init_redis

logger = get_logger("app.main")


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or load_settings()
    configure_logging(cfg)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        init_db(cfg)
        init_redis(cfg)
        logger.info("app_started", env=cfg.app_env, version=cfg.version)
        try:
            yield
        finally:
            await close_redis()
            await dispose_db()
            logger.info("app_stopped")

    app = FastAPI(
        title="CZCE Radar API",
        version=cfg.version,
        description="郑商所品种预测研判系统 · 接口层",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
    )
    app.state.settings = cfg

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(TraceIdMiddleware)

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(sources.router)
    app.include_router(predictions.router)
    app.include_router(admin_models.router)
    app.include_router(news.router)
    app.include_router(dashboard.router)
    return app


app = create_app()
