from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_dependency
from app.main import create_app
from tests.conftest import make_settings


@pytest.fixture()
def api_client(migrated_pg_url: str) -> Iterator[TestClient]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    app = create_app(make_settings())
    engine = create_async_engine(migrated_pg_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override() -> Iterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[session_dependency] = override
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


async def _seed(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM fetch_log WHERE source_code = 'test_src'"))
    await session.execute(text("DELETE FROM source WHERE code = 'test_src'"))
    await session.execute(
        text(
            "INSERT INTO source (code, name, kind, interval_sec, enabled,"
            " last_status, last_run_at)"
            " VALUES ('test_src', '测试源', 'quote', 60, TRUE, 'ok', :run)"
        ),
        {"run": dt.datetime.now(dt.UTC)},
    )
    await session.execute(
        text(
            "INSERT INTO fetch_log (source_code, status, fetched, inserted, started_at)"
            " VALUES ('test_src', 'empty', 0, 0, :t1)"
        ),
        {"t1": dt.datetime.now(dt.UTC) - dt.timedelta(seconds=120)},
    )
    await session.execute(
        text(
            "INSERT INTO fetch_log (source_code, status, fetched, inserted, started_at)"
            " VALUES ('test_src', 'ok', 31, 5, :t2)"
        ),
        {"t2": dt.datetime.now(dt.UTC)},
    )
    await session.commit()


def test_sources_endpoint_returns_latest_fetch(
    api_client: TestClient, db_session: AsyncSession
) -> None:
    asyncio.run(_seed(db_session))
    response = api_client.get("/api/v1/sources")
    assert response.status_code == 200
    body = response.json()
    source = next(s for s in body["sources"] if s["code"] == "test_src")
    assert source["kind"] == "quote"
    assert source["last_status"] == "ok"
    assert source["last_fetched"] == 31  # 取最近一次 fetch_log，而非聚合
    assert source["last_inserted"] == 5
    assert source["last_run_at"] is not None
