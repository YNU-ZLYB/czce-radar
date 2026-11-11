"""M4 GBM/校准/融合测试：类别权重、isotonic、logloss 加权、切换与回滚。"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jobs.predict.fusion import (
    activate_model,
    bucket_from_probs,
    direction_from_probs,
    fuse_probs,
)
from jobs.train.gbm import (
    IsotonicCalibrator,
    class_weights,
    fusion_weights,
    logloss,
    predict_proba,
    train_booster,
)


def test_class_weights_balanced() -> None:
    w = class_weights([0, 0, 0, 1, 1, 2])
    assert w[0] == pytest.approx(6 / 9)
    assert w[1] == pytest.approx(6 / 6)
    assert w[2] == pytest.approx(6 / 3)


def test_fusion_weights_prefer_lower_logloss() -> None:
    w_rule, w_gbm = fusion_weights(0.9, 0.6)
    assert w_gbm > w_rule
    assert w_rule + w_gbm == pytest.approx(1.0)


def test_isotonic_calibrator_renormalizes() -> None:
    probas = [[0.7, 0.2, 0.1], [0.2, 0.6, 0.2], [0.1, 0.2, 0.7]] * 20
    y = [0, 1, 2] * 20
    cal = IsotonicCalibrator().fit(probas, y)
    out = cal.transform([[0.65, 0.25, 0.1], [0.05, 0.1, 0.85]])
    for row in out:
        assert sum(row) == pytest.approx(1.0)
        assert min(row) >= 0.0
    assert out[0][0] > out[0][2]
    assert out[1][2] > out[1][0]


def test_train_booster_learns_separable_signal() -> None:
    """线性可分数据上 GBM 应显著优于随机（早停 ≤400 轮）。"""
    x = [[float(i % 3), float(i)] for i in range(240)]
    y = [i % 3 for i in range(240)]
    x_train, y_train = x[:200], y[:200]
    x_valid, y_valid = x[200:], y[200:]
    booster = train_booster(x_train, y_train, x_valid, y_valid, ["a", "b"])
    assert booster.best_iteration <= 400  # 早停轮数上限（num_trees = 轮数 × 3 类）
    probs = predict_proba(booster, x_valid)
    assert len(probs) == len(y_valid)
    assert all(len(p) == 3 for p in probs)
    acc = sum(
        1 for p, v in zip(probs, y_valid, strict=True) if max(range(3), key=lambda i: p[i]) == v
    ) / len(y_valid)
    assert acc > 0.8


def test_fuse_probs_and_bucket_consistency() -> None:
    fused = fuse_probs((0.1, 0.2, 0.7), [0.05, 0.15, 0.8], 0.5, 0.5)
    assert sum(fused) == pytest.approx(1.0)
    assert direction_from_probs(fused) == 1
    assert bucket_from_probs(fused) == "强多"  # max ≥ 0.6

    weak = fuse_probs((0.2, 0.35, 0.45), None, 0.5, 0.5)  # GBM 缺失 → 纯规则
    assert weak == [0.2, 0.35, 0.45]
    assert direction_from_probs(weak) == 1
    assert bucket_from_probs(weak) == "偏多"  # max < 0.6

    flat = fuse_probs((0.2, 0.6, 0.2), None, 1.0, 0.0)
    assert direction_from_probs(flat) == 0
    assert bucket_from_probs(flat) == "震荡"


def test_logloss_prefers_correct() -> None:
    good = logloss([[0.1, 0.1, 0.8]], [2])
    bad = logloss([[0.8, 0.1, 0.1]], [2])
    assert good < bad


async def test_engine_fusion_degraded_when_artifact_missing(db_session) -> None:  # type: ignore[no-untyped-def]
    """融合模型产物缺失 → 该版本行退化为规则概率且置信度 ×0.8（§5.1 兜底）。"""
    import json

    from jobs.predict.engine import predict_variety
    from jobs.predict.rules import score

    session = db_session
    features = {
        "price": {"close": 3421.0, "ret_1d": 0.3, "ret_5d": 1.1, "vol_20d": 1.8, "pos_20d": 0.4},
        "foreign": {},
        "linkage": [],
        "industry": {},
        "seasonality": {"month": 11, "bias": 2.0, "confidence": 0.5, "stage": "冬季限气"},
        "news": {},
        "event": {"is_event_day": False},
    }
    asof = dt.datetime(2026, 11, 10, 0, 25, tzinfo=dt.UTC)
    await session.execute(
        text(
            "INSERT INTO feature_snapshot (variety_code, asof, version, features)"
            " VALUES ('MA', :asof, 'f1', CAST(:f AS jsonb))"
        ),
        {"asof": asof, "f": json.dumps(features, ensure_ascii=False)},
    )
    await session.execute(text("DELETE FROM model_registry"))
    params = {
        "ext_codes": [],
        "feature_names": ["ret_1d"],
        "pooled_feature_names": ["ret_1d"],
        "models": {
            "MA": {
                "artifact": {"booster": "/nonexistent/x.txt", "calibrator": "/nonexistent/x.pkl"},
                "pooled": False,
            }
        },
        "fusion": {"MA": {"w_rule": 0.4, "w_gbm": 0.6}},
    }
    await session.execute(
        text(
            "INSERT INTO model_registry (version, kind, params, status, metrics)"
            " VALUES ('gbm-20261110-99', 'fusion', CAST(:p AS jsonb), 'shadow', '{}')"
        ),
        {"p": json.dumps(params, ensure_ascii=False)},
    )
    await session.flush()

    versions = await predict_variety(session, "MA")
    assert versions == ["rule-20261110-01", "gbm-20261110-99"]
    await session.flush()

    rows_list = (
        await session.execute(
            text(
                "SELECT model_version, p_up::float8 AS p_up, p_down::float8 AS p_down,"
                " confidence::float8 AS confidence FROM prediction"
                " WHERE variety_code = 'MA' AND target_date = '2026-11-10'"
            )
        )
    ).all()
    rows = {r.model_version: r for r in rows_list}
    expected = score(features)
    assert rows["rule-20261110-01"].p_up == pytest.approx(expected.p_up)
    assert rows["gbm-20261110-99"].p_up == pytest.approx(expected.p_up)  # 退化 = 规则概率
    assert rows["gbm-20261110-99"].confidence == pytest.approx(
        round(expected.confidence * 0.8, 4), abs=1e-4
    )
    await session.rollback()


async def test_activate_model_switch_and_rollback(db_session) -> None:  # type: ignore[no-untyped-def]
    session = db_session
    for version, status in (("gbm-20261110-01", "active"), ("gbm-20261109-01", "retired")):
        await session.execute(
            text(
                "INSERT INTO model_registry (version, kind, status, metrics)"
                " VALUES (:v, 'fusion', :s, '{}')"
            ),
            {"v": version, "s": status},
        )
    await session.flush()

    # 切换到旧版本（回滚）：旧版本 active，新版本 retired
    row = await activate_model(session, "gbm-20261109-01")
    assert row is not None and row.version == "gbm-20261109-01"
    states = dict(
        (
            await session.execute(text("SELECT version, status FROM model_registry"))
        ).all()
    )
    assert states == {"gbm-20261109-01": "active", "gbm-20261110-01": "retired"}

    missing = await activate_model(session, "gbm-does-not-exist")
    assert missing is None
    await session.rollback()


async def test_admin_models_auth_and_importance(
    migrated_pg_url: str, db_session: AsyncSession
) -> None:
    """admin 接口：无 Token 401；有 Token 可查列表/导出重要性；激活不存在版本 404。"""
    import json
    from collections.abc import AsyncIterator

    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.db import session_dependency
    from app.main import create_app
    from tests.conftest import make_settings

    session = db_session
    await session.execute(text("DELETE FROM model_registry"))
    params = {"importance": {"MA": {"f_OIL_chg_1d": 12.3}}}
    await session.execute(
        text(
            "INSERT INTO model_registry (version, kind, params, status, metrics)"
            " VALUES ('gbm-20261110-01', 'fusion', CAST(:p AS jsonb), 'shadow', '{}')"
        ),
        {"p": json.dumps(params)},
    )
    await session.commit()

    engine = create_async_engine(migrated_pg_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = make_settings()
    app = create_app(settings)

    async def override() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s

    app.dependency_overrides[session_dependency] = override
    try:
        with TestClient(app) as client:
            assert client.get("/api/v1/admin/models").status_code == 401

            headers = {"X-API-Token": settings.api_token}
            resp = client.get("/api/v1/admin/models", headers=headers)
            assert resp.status_code == 200
            assert resp.json()["models"][0]["version"] == "gbm-20261110-01"

            imp = client.get("/api/v1/admin/models/gbm-20261110-01/importance", headers=headers)
            assert imp.status_code == 200
            assert imp.json()["importance"]["MA"]["f_OIL_chg_1d"] == 12.3
            assert "spark_6h" not in json.dumps(imp.json())

            missing = client.post(
                "/api/v1/admin/models/gbm-nope/activate", headers=headers
            )
            assert missing.status_code == 404
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()
        await session.execute(text("DELETE FROM model_registry"))
        await session.commit()


async def test_rollback_drill_api_immediate(
    migrated_pg_url: str, db_session: AsyncSession
) -> None:
    """M7 DoD：旧模型版本通过 API 回滚后立即生效（同请求内验证，远低于 1 分钟）。"""
    from collections.abc import AsyncIterator

    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.db import session_dependency
    from app.main import create_app
    from tests.conftest import make_settings

    session = db_session
    await session.execute(text("DELETE FROM model_registry"))
    await session.execute(
        text(
            "INSERT INTO model_registry (version, kind, status, metrics) VALUES"
            " ('gbm-new-01', 'fusion', 'active', '{}'),"
            " ('gbm-old-01', 'fusion', 'retired', '{}')"
        )
    )
    await session.commit()

    engine = create_async_engine(migrated_pg_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = make_settings()
    app = create_app(settings)

    async def override() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s

    app.dependency_overrides[session_dependency] = override
    try:
        with TestClient(app) as client:
            headers = {"X-API-Token": settings.api_token}
            # 回滚到旧版本
            resp = client.post(
                "/api/v1/admin/models/gbm-old-01/activate", headers=headers
            )
            assert resp.status_code == 200
            assert resp.json()["version"] == "gbm-old-01"

            # 立即验证：旧版本 active，新版本 retired（同请求内，<1s）
            rows = dict(
                (
                    await session.execute(
                        text("SELECT version, status FROM model_registry")
                    )
                ).all()
            )
            assert rows == {"gbm-old-01": "active", "gbm-new-01": "retired"}
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()
        await session.execute(text("DELETE FROM model_registry"))
        await session.commit()


async def test_predictions_api_filters_active_version(
    migrated_pg_url: str, db_session: AsyncSession
) -> None:
    """有 active 融合模型时 /predictions 只返回该版本；切换后立即变更（DoD ③）。"""
    from collections.abc import AsyncIterator

    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.db import session_dependency
    from app.main import create_app
    from tests.conftest import make_settings

    day = dt.date(2026, 11, 10)
    session = db_session
    await session.execute(text("DELETE FROM prediction WHERE target_date = :d"), {"d": day})
    await session.execute(text("DELETE FROM model_registry"))
    for version, status in (("gbm-20261110-01", "shadow"), ("gbm-20261109-01", "active")):
        await session.execute(
            text(
                "INSERT INTO model_registry (version, kind, status, metrics)"
                " VALUES (:v, 'fusion', :s, '{}')"
            ),
            {"v": version, "s": status},
        )
    for version in ("gbm-20261110-01", "gbm-20261109-01", "rule-20261110-01"):
        await session.execute(
            text(
                "INSERT INTO prediction (variety_code, target_date, asof, direction, bucket,"
                " p_up, p_flat, p_down, signal, confidence, contributor, model_version)"
                " VALUES ('MA', :d, :a, 1, '偏多', 0.5, 0.3, 0.2, 20, 0.5, '[]', :v)"
            ),
            {"d": day, "a": dt.datetime(2026, 11, 10, 0, 30, tzinfo=dt.UTC), "v": version},
        )
    await session.commit()

    # API 侧用独立 engine（TestClient 跑在自己的事件循环里，不能共享连接）
    engine = create_async_engine(migrated_pg_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(make_settings())

    async def override() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s

    app.dependency_overrides[session_dependency] = override
    try:
        with TestClient(app) as client:
            resp = client.get(f"/api/v1/predictions?date={day.isoformat()}")
            assert resp.status_code == 200
            items = resp.json()["predictions"]
            assert len(items) == 1
            assert items[0]["model_version"] == "gbm-20261109-01"  # active 而非 shadow/rule

            # 切换版本 → 同一请求立即返回新版本（接口实时读 registry，无需重启）
            async with session.begin():
                await activate_model(session, "gbm-20261110-01")
            resp2 = client.get(f"/api/v1/predictions?date={day.isoformat()}")
            items2 = resp2.json()["predictions"]
            assert len(items2) == 1
            assert items2[0]["model_version"] == "gbm-20261110-01"
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()
        await session.execute(text("DELETE FROM prediction WHERE target_date = :d"), {"d": day})
        await session.execute(text("DELETE FROM model_registry"))
        await session.commit()
