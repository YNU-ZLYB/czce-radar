"""SQLAlchemy 2.0 异步数据库接入。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings

# libpq 的 sslmode 语义 → asyncpg 的 ssl 参数取值。
# 云数据库（Supabase 等）强制 SSL，连接串通常带 ?sslmode=require；
# asyncpg 不认 sslmode，必须在建引擎前转换。
_SSLMODE_TO_ASYNCPG = {
    "disable": "disable",
    "allow": "prefer",
    "prefer": "prefer",
    "require": "require",
    "verify-ca": "verify-ca",
    "verify-full": "verify-full",
}


def normalise_database_url(raw: str) -> URL:
    """把配置里的连接串转换成 asyncpg 可直接使用的形式。

    1. ``postgresql://`` → ``postgresql+asyncpg://``
    2. ``?sslmode=X`` → ``?ssl=Y``（并移除 sslmode，否则 asyncpg 会报未知参数）
    """
    url = make_url(raw)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+asyncpg")

    query = {key: value for key, value in url.query.items()}
    sslmode = query.pop("sslmode", None)
    if isinstance(sslmode, tuple):
        sslmode = sslmode[-1]
    if sslmode is not None and "ssl" not in query:
        query["ssl"] = _SSLMODE_TO_ASYNCPG.get(str(sslmode), str(sslmode))

    return url.set(query=query)


def render_database_url(raw: str) -> str:
    """供 Alembic 等字符串入口使用；``%`` 需转义以兼容 configparser。"""
    return normalise_database_url(raw).render_as_string(hide_password=False).replace("%", "%%")


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        normalise_database_url(settings.database_url),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_recycle=1800,
        echo=False,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


engine: AsyncEngine | None = None
SessionFactory: async_sessionmaker[AsyncSession] | None = None


def init_db(settings: Settings) -> None:
    """由应用启动阶段调用，确保配置已完成校验。"""
    global engine, SessionFactory
    engine = create_engine(settings)
    SessionFactory = create_session_factory(engine)


async def dispose_db() -> None:
    global engine, SessionFactory
    if engine is not None:
        await engine.dispose()
    engine = None
    SessionFactory = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if SessionFactory is None:
        raise RuntimeError("数据库尚未初始化：请先调用 init_db()")
    return SessionFactory


async def session_dependency() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求一个会话。"""
    async with get_session_factory()() as session:
        yield session
