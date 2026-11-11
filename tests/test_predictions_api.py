"""预测接口测试：按日/品种过滤、最新日默认、贡献者字段完整。"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import session_dependency
from app.main import create_app
from tests.conftest import make_settings

TARGET = dt.date(2030, 1, 2)


@pytest.fixture()
def api_client(migrated_pg_url: str) -> Iterator[TestClient]:
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
    await session.execute(
        text("DELETE FROM prediction WHERE variety_code = 'MA' AND target_date = :d"),
        {"d": TARGET},
    )
    contributor = [
        {
            "name": "布伦特原油",
            "value": -1.31,
            "weight": 0.45,
            "q": 0.58,
            "contribution": -0.34,
        },
        {
            "name": "季节性·11月冬季限气",
            "value": None,
            "weight": 1.0,
            "q": 0.5,
            "contribution": 3.0,
        },
    ]
    await session.execute(
        text(
            "INSERT INTO prediction (variety_code, target_date, horizon, asof, direction,"
            " bucket, p_up, p_flat, p_down, exp_change, signal, confidence, contributor,"
            " model_version, narrative)"
            " VALUES ('MA', :d, '1d', :asof, -1, '偏空', 0.21, 0.27, 0.52, -1.42,"
            " -31, 0.52, CAST(:c AS jsonb), 'rule-20300102-01', '测试解读')"
        ),
        {
            "d": TARGET,
            "asof": dt.datetime(2030, 1, 2, 0, 30, tzinfo=dt.UTC),
            "c": json.dumps(contributor, ensure_ascii=False),
        },
    )
    await session.commit()


def _get(client: TestClient, **params: object) -> dict:  # type: ignore[type-arg]
    response = client.get("/api/v1/predictions", params=params)
    assert response.status_code == 200
    return response.json()  # type: ignore[no-any-return]


def test_predictions_by_date_and_variety(
    api_client: TestClient, db_session: AsyncSession
) -> None:
    asyncio.run(_seed(db_session))
    body = _get(api_client, date=TARGET.isoformat(), variety="MA")
    assert len(body["predictions"]) == 1
    item = body["predictions"][0]
    assert item["variety_code"] == "MA"
    assert item["bucket"] == "偏空"
    assert item["direction"] == -1
    assert item["signal"] == -31
    assert item["p_up"] == pytest.approx(0.21)
    assert item["model_version"] == "rule-20300102-01"
    assert item["narrative"] == "测试解读"
    assert item["contributor"][0]["name"] == "布伦特原油"
    assert set(item["contributor"][0]) == {"name", "value", "weight", "q", "contribution"}


def test_predictions_default_latest_date(
    api_client: TestClient, db_session: AsyncSession
) -> None:
    asyncio.run(_seed(db_session))
    body = _get(api_client)
    assert any(p["target_date"] == TARGET.isoformat() for p in body["predictions"])


def test_predictions_unknown_variety_returns_empty(
    api_client: TestClient, db_session: AsyncSession
) -> None:
    asyncio.run(_seed(db_session))
    body = _get(api_client, date=TARGET.isoformat(), variety="ZZ")
    assert body["predictions"] == []
