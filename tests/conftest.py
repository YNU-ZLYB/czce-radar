"""测试全局配置：先注入环境变量，再导入应用模块。"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "apps" / "api", ROOT, ROOT / "packages" / "schema"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("API_TOKEN", "test-token")

from app.core.config import Settings  # noqa: E402
from app.core.db import normalise_database_url, session_dependency  # noqa: E402
from app.core.redis_client import redis_dependency  # noqa: E402
from app.main import create_app  # noqa: E402


class FakeSession:
    async def execute(self, *args: object, **kwargs: object) -> None:
        return None


class FailingSession:
    async def execute(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("db down")


class FakeRedis:
    def __init__(self, heartbeat: str | None = None, ping_ok: bool = True) -> None:
        self.heartbeat = heartbeat
        self.ping_ok = ping_ok

    async def ping(self) -> bool:
        return self.ping_ok

    async def get(self, key: str) -> str | None:
        return self.heartbeat


def make_settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture(scope="session")
def migrated_pg_url() -> Iterator[str]:
    """真实 PostgreSQL（pgserver 内嵌库或 TEST_DATABASE_URL），并跑完全部迁移。"""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        import pgserver

        data_dir = "/tmp/czce_radar_test_pg"
        pgserver.get_server(data_dir)
        url = f"postgresql://postgres@/postgres?host={data_dir}"
    url = normalise_database_url(url).render_as_string(hide_password=False)
    os.environ["DATABASE_URL"] = url
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")
    yield url


@pytest.fixture()
async def db_session(migrated_pg_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(migrated_pg_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()  # 默认不持久化；需要落库的测试显式 commit
    await engine.dispose()


@pytest.fixture()
def client() -> Iterator[TestClient]:
    application = create_app(make_settings())
    application.dependency_overrides[session_dependency] = lambda: FakeSession()
    application.dependency_overrides[redis_dependency] = lambda: _fresh_redis()
    with TestClient(application) as test_client:
        yield test_client
    application.dependency_overrides.clear()


def _fresh_redis() -> FakeRedis:
    import time

    return FakeRedis(heartbeat=str(time.time()))
