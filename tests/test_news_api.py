"""M5 接口测试：修订历史（含原文跳转链接/告警）与 LLM 成本统计。"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.db import session_dependency
from app.main import create_app
from tests.conftest import make_settings

TARGET = dt.date(2031, 2, 3)


@pytest.fixture()
def api_client(migrated_pg_url: str) -> Iterator[TestClient]:
    app = create_app(make_settings())
    engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override() -> Iterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[session_dependency] = override
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    asyncio.run(_cleanup(migrated_pg_url))


async def _cleanup(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(
                text(
                    "DELETE FROM prediction_revision WHERE prediction_id IN"
                    " (SELECT id FROM prediction WHERE target_date = :d)"
                ),
                {"d": TARGET},
            )
            await session.execute(
                text("DELETE FROM prediction WHERE target_date = :d"), {"d": TARGET}
            )
            await session.execute(text("DELETE FROM news_doc WHERE source = 'test_api'"))
            await session.execute(
                text(
                    "DELETE FROM llm_call_log"
                    " WHERE (created_at AT TIME ZONE 'Asia/Shanghai')::date = :d"
                ),
                {"d": TARGET},
            )
            await session.commit()
    finally:
        await engine.dispose()


async def _seed(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM prediction_revision"))
    await session.execute(text("DELETE FROM prediction WHERE target_date = :d"), {"d": TARGET})
    await session.execute(text("DELETE FROM news_doc WHERE source = 'test_api'"))
    await session.execute(
        text(
            "DELETE FROM llm_call_log"
            " WHERE (created_at AT TIME ZONE 'Asia/Shanghai')::date = :d"
        ),
        {"d": TARGET},
    )
    doc_ids: list[int] = []
    for i in range(4):
        doc_ids.append(
            int(
                await session.scalar(
                    text(
                        "INSERT INTO news_doc (source, external_id, title, content, url,"
                        " published_at, fetched_at, content_hash)"
                        " VALUES ('test_api', :eid, :t, '港口库存大幅高于预期', :url, :p, :p, :h)"
                        " RETURNING id"
                    ),
                    {
                        "eid": f"api-{i}",
                        "t": f"甲醇超预期累库第{i}条",
                        "url": f"https://example.com/news/api-{i}",
                        "p": dt.datetime(2031, 2, 3, 2, i, tzinfo=dt.UTC),
                        "h": f"{i:040d}",
                    },
                )
            )
        )
    pid = await session.scalar(
        text(
            "INSERT INTO prediction (variety_code, target_date, horizon, asof, direction,"
            " bucket, p_up, p_flat, p_down, exp_change, signal, confidence, contributor,"
            " model_version) VALUES ('MA', :d, '1d', :a, 1, '偏多', 0.5, 0.3, 0.2, 1.0,"
            " 20, 0.5, '[]'::jsonb, :v) RETURNING id"
        ),
        {
            "d": TARGET,
            "a": dt.datetime(2031, 2, 3, 0, 30, tzinfo=dt.UTC),
            "v": f"rule-{TARGET:%Y%m%d}-01",
        },
    )
    for i, doc_id in enumerate(doc_ids):
        await session.execute(
            text(
                "INSERT INTO prediction_revision (prediction_id, at, prev_signal, new_signal,"
                " prev_direction, new_direction, reason, trigger_doc_id, is_surprise)"
                " VALUES (:p, :at, 20, -5, 1, -1, :r, :doc, TRUE)"
            ),
            {
                "p": pid,
                "at": dt.datetime(2031, 2, 3, 3, i, tzinfo=dt.UTC),
                "r": f"预期差资讯第 {i} 条",
                "doc": doc_id,
            },
        )
    await session.execute(
        text(
            "INSERT INTO llm_call_log (purpose, model, prompt_tokens, completion_tokens,"
            " cost_cny, cache_hit, created_at) VALUES"
            " ('extract_event', 'fake', 100, 50, 0.5, FALSE, :c),"
            " ('extract_event', 'fake', 0, 0, 0, TRUE, :c)"
        ),
        {"c": dt.datetime(2031, 2, 3, 2, 0, tzinfo=dt.UTC)},
    )
    await session.commit()


def test_revisions_api_exposes_trigger_doc_and_alert(
    api_client: TestClient, db_session: AsyncSession
) -> None:
    asyncio.run(_seed(db_session))
    response = api_client.get("/api/v1/revisions", params={"date": TARGET.isoformat()})
    assert response.status_code == 200
    body = response.json()
    assert len(body["revisions"]) == 4
    first = body["revisions"][0]
    assert first["trigger_doc"]["url"] == "https://example.com/news/api-0"
    assert first["trigger_doc"]["title"] == "甲醇超预期累库第0条"
    assert first["new_direction"] == -1
    assert body["alerts"] == ["MA"]  # 单品种单日修订 >3


def test_llm_stats_api(api_client: TestClient, db_session: AsyncSession) -> None:
    asyncio.run(_seed(db_session))
    response = api_client.get(
        "/api/v1/admin/llm-stats",
        params={"date": TARGET.isoformat()},
        headers={"X-API-Token": "test-token"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["calls"] == 2
    assert body["cache_hits"] == 1
    assert body["cache_hit_rate"] == 0.5
    assert body["cost_cny"] == 0.5
    assert body["budget_cny"] == 5.0
    assert body["degraded"] is False
    assert body["llm_enabled"] is False

    response = api_client.get("/api/v1/admin/llm-stats", headers={"X-API-Token": "bad"})
    assert response.status_code == 401
