"""M7：/metrics Prometheus 端点测试。"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import session_dependency
from app.main import create_app
from tests.conftest import make_settings

NOW = dt.datetime.now(dt.UTC)


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
            await session.execute(text("DELETE FROM fetch_log WHERE source_code = 'test_src'"))
            await session.execute(
                text("DELETE FROM llm_call_log WHERE model = 'test-llm'")
            )
            await session.execute(text("DELETE FROM model_registry WHERE version LIKE 'test-%'"))
            await session.execute(
                text("DELETE FROM outcome WHERE prediction_id = 900901")
            )
            await session.execute(
                text("DELETE FROM prediction WHERE id = 900901")
            )
            await session.commit()
    finally:
        await engine.dispose()


async def _seed(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO fetch_log (source_code, status, fetched, inserted, started_at)"
            " VALUES ('test_src', 'ok', 10, 8, :now), ('test_src', 'error', 0, 0, :now)"
        ),
        {"now": NOW},
    )
    await session.execute(
        text(
            "INSERT INTO prediction (id, variety_code, target_date, horizon, asof, direction,"
            " bucket, p_up, p_flat, p_down, signal, confidence, contributor, model_version)"
            " VALUES (900901, 'MA', :d, '1d', :asof, 1, '偏多', 0.6, 0.3, 0.1, 30, 0.6,"
            " '[]'::jsonb, 'rule-test-metrics-01')"
        ),
        {"d": NOW.date(), "asof": NOW},
    )
    await session.execute(
        text(
            "INSERT INTO outcome (prediction_id, actual_change, actual_dir, actual_bucket,"
            " hit_dir, brier) VALUES (900901, 1.5, 1, '偏多', true, 0.22)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO llm_call_log (purpose, model, prompt_tokens, completion_tokens,"
            " cost_cny, cache_hit, latency_ms, created_at)"
            " VALUES ('extract', 'test-llm', 100, 50, 0.001, false, 200, :now),"
            "        ('extract', 'test-llm', 100, 50, 0.0, true, 10, :now)"
        ),
        {"now": NOW},
    )
    await session.execute(
        text(
            "INSERT INTO model_registry (version, kind, status, metrics)"
            " VALUES ('test-fusion-01', 'fusion', 'active', '{}')"
        )
    )
    await session.commit()


def test_metrics_returns_prometheus_format(
    api_client: TestClient, db_session: AsyncSession
) -> None:
    asyncio.run(_seed(db_session))
    response = api_client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text

    # 关键指标族存在
    assert "czce_fetch_total" in body
    assert 'czce_fetch_total{source="test_src",status="ok"}' in body
    assert 'czce_fetch_total{source="test_src",status="error"}' in body
    assert "czce_fetch_success_rate" in body
    assert "czce_prediction_count" in body
    assert "czce_outcome_direction_accuracy" in body
    assert "czce_outcome_brier" in body
    assert "czce_llm_calls_total" in body
    assert "czce_llm_cost_cny_total" in body
    assert "czce_llm_cache_hit_rate" in body
    assert "czce_model_registry" in body
    assert 'czce_model_registry{version="test-fusion-01",kind="fusion",status="active"} 1' in body
    assert "czce_worker_heartbeat_age_seconds" in body

    # 数值行可解析（不含 NaN 的核心指标）
    for line in body.splitlines():
        if line.startswith("czce_outcome_brier "):
            val = line.split()[-1]
            assert val != "NaN"
            assert float(val) == pytest.approx(0.22, abs=1e-4)


def test_metrics_no_data_returns_nan_or_zero(
    api_client: TestClient, db_session: AsyncSession
) -> None:
    """空库时命中率类指标返回 NaN，计数类返回 0，不报错。"""
    response = api_client.get("/metrics")
    assert response.status_code == 200
    body = response.text
    assert "czce_outcome_direction_accuracy" in body
    assert "czce_llm_calls_total 0" in body
