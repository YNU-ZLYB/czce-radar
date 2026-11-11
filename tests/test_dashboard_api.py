"""M6 聚合接口测试：品种详情 / Scorecard / 资讯抽取列表。"""

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

TARGET = dt.date(2031, 5, 6)


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
    asyncio.run(_cleanup(migrated_pg_url))
    asyncio.run(engine.dispose())


async def _cleanup(url: str) -> None:
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(
                text("DELETE FROM outcome WHERE prediction_id IN"
                     " (SELECT id FROM prediction WHERE target_date = :d)"),
                {"d": TARGET},
            )
            await session.execute(
                text("DELETE FROM prediction_revision WHERE prediction_id IN"
                     " (SELECT id FROM prediction WHERE target_date = :d)"),
                {"d": TARGET},
            )
            await session.execute(
                text("DELETE FROM prediction WHERE target_date = :d"), {"d": TARGET}
            )
            await session.execute(text("DELETE FROM news_extract WHERE doc_id IN"
                                       " (SELECT id FROM news_doc WHERE source = 'test_m6')"))
            await session.execute(text("DELETE FROM news_doc WHERE source = 'test_m6'"))
            await session.execute(
                text("DELETE FROM daily_bar WHERE variety_code = 'MA' AND trade_date >= :d"),
                {"d": TARGET - dt.timedelta(days=40)},
            )
            await session.commit()
    finally:
        await engine.dispose()


async def _seed(session: AsyncSession) -> None:
    await _seed_prices(session)
    await session.execute(
        text(
            "INSERT INTO prediction (id, variety_code, target_date, horizon, asof, direction,"
            " bucket, p_up, p_flat, p_down, exp_change, signal, confidence, contributor,"
            " model_version)"
            " VALUES (900001, 'MA', :d, '1d', :asof, 1, '偏多', 0.55, 0.30, 0.15, 0.8,"
            " 25, 0.55, CAST(:c AS jsonb), 'rule-20310506-01')"
        ),
        {
            "d": TARGET,
            "asof": dt.datetime(2031, 5, 6, 0, 30, tzinfo=dt.UTC),
            "c": json.dumps([
                {"name": "布伦特原油", "value": 1.2, "weight": 0.45,
                 "q": 0.58, "contribution": 0.31}
            ]),
        },
    )
    await session.execute(
        text(
            "INSERT INTO outcome (prediction_id, actual_change, actual_dir, actual_bucket,"
            " hit_dir, brier) VALUES (900001, 1.2, 1, '偏多', true, 0.26)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO news_doc (id, source, external_id, title, content, url, published_at,"
            " fetched_at, content_hash)"
            " VALUES (910001, 'test_m6', 'm6-1', '甲醇港口库存大幅下降', '正文',"
            " 'https://example.com/n1', :pub, :pub, 'm6hash1')"
        ),
        {"pub": dt.datetime.now(dt.UTC)},
    )
    await session.execute(
        text(
            "INSERT INTO news_extract (doc_id, variety_code, direction, strength, chain_stage,"
            " is_surprise, method, evidence)"
            " VALUES (910001, 'MA', 1, 4, 'mid', true, 'rule', '库存大幅下降')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO prediction_revision (prediction_id, at, prev_signal, new_signal,"
            " prev_direction, new_direction, reason, trigger_doc_id, is_surprise)"
            " VALUES (900001, :at, 10, 25, 0, 1, '预期差：库存大降', 910001, true)"
        ),
        {"at": dt.datetime.now(dt.UTC)},
    )
    await session.commit()


async def _seed_prices(session: AsyncSession) -> None:
    for i in range(24):
        day = TARGET - dt.timedelta(days=24 - i)
        close = 3000.0 + i
        await session.execute(
            text(
                "INSERT INTO daily_bar (variety_code, trade_date, open, high, low, close,"
                " source) VALUES ('MA', :d, :c, :c, :c, :c, 'test')"
            ),
            {"d": day, "c": close},
        )


def test_variety_detail_bundle(api_client: TestClient, db_session: AsyncSession) -> None:
    asyncio.run(_seed(db_session))
    response = api_client.get("/api/v1/varieties/MA/detail")
    assert response.status_code == 200
    body = response.json()
    assert body["variety"]["code"] == "MA"
    assert body["variety"]["name"] == "甲醇"
    assert body["prediction"]["bucket"] == "偏多"
    assert body["prediction"]["model_version"] == "rule-20310506-01"
    assert len(body["prices"]) == 24
    assert body["prices"][0]["date"] < body["prices"][-1]["date"]
    assert body["signal_history"][-1]["signal"] == 25  # 历史按时间升序，最新在最后
    assert len(body["bullish"]) == 1
    assert body["bullish"][0]["url"] == "https://example.com/n1"
    assert body["bearish"] == []
    assert len(body["revisions"]) == 1
    assert body["revisions"][0]["trigger_doc"]["url"] == "https://example.com/n1"
    assert body["model"]["version"] == "rule-20310506-01"
    assert body["model"]["kind"] == "rule"


def test_list_varieties(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/varieties")
    assert response.status_code == 200
    body = response.json()
    assert len(body["varieties"]) == 21
    codes = {v["code"] for v in body["varieties"]}
    assert {"MA", "SR", "AP"} <= codes
    assert "ZC" not in codes  # 已下市不返回


def test_variety_detail_unknown_404(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/varieties/ZZ/detail")
    assert response.status_code == 404


def test_scorecard_endpoint(api_client: TestClient, db_session: AsyncSession) -> None:
    asyncio.run(_seed(db_session))
    response = api_client.get("/api/v1/scorecard", params={"days": 400})
    assert response.status_code == 200
    body = response.json()
    assert body["model_version"] == "rule-*"
    assert body["overall"]["n"] >= 1
    mine = [v for v in body["by_variety"] if v["variety_code"] == "MA"]
    assert mine and mine[0]["n"] >= 1
    assert body["latest"] is not None
    item = [i for i in body["latest"]["items"] if i["variety_code"] == "MA"]
    if body["latest"]["date"] == TARGET.isoformat():
        assert item[0]["hit"] is True
        assert item[0]["actual_change"] == pytest.approx(1.2)


def test_news_extracts_endpoint(api_client: TestClient, db_session: AsyncSession) -> None:
    asyncio.run(_seed(db_session))
    response = api_client.get("/api/v1/news/extracts", params={"days": 2})
    assert response.status_code == 200
    body = response.json()
    mine = [e for e in body["extracts"] if e["doc"]["id"] == 910001]
    assert len(mine) == 1
    assert mine[0]["variety_name"] == "甲醇"
    assert mine[0]["direction"] == 1
    assert mine[0]["doc"]["url"] == "https://example.com/n1"
