"""Layer A 规则引擎测试：分档、概率、q=0、确定性、贡献者可追溯。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import text

from czce_schema.symbols import DOMESTIC_SINA_SYMBOLS
from jobs.knowledge.importer import import_knowledge
from jobs.knowledge.models import load_knowledge_dir
from jobs.predict.engine import predict_variety
from jobs.predict.rules import (
    BUCKET_DOWN,
    BUCKET_FLAT,
    BUCKET_STRONG_DOWN,
    BUCKET_STRONG_UP,
    BUCKET_UP,
    bands,
    classify_bucket,
    direction_of,
    probabilities,
    score,
)

REPO = Path(__file__).resolve().parents[1]
KNOWLEDGE = REPO / "data" / "knowledge"


def _features(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "price": {"vol_20d": 2.0},
        "foreign": {
            "OIL": {"chg_1d": -1.31, "chg_3d": -2.1, "session": "prior"},
            "NG": {"chg_1d": -0.62, "chg_3d": -1.0, "session": "prior"},
        },
        "linkage": [
            {
                "name": "布伦特原油",
                "ext": "OIL",
                "prior": 0.45,
                "q": 0.58,
                "chg_1d": -1.31,
                "contrib": -0.34,
            },
            {
                "name": "NYMEX 天然气",
                "ext": "NG",
                "prior": 0.30,
                "q": 0.58,
                "chg_1d": -0.62,
                "contrib": -0.11,
            },
        ],
        "seasonality": {
            "month": 11,
            "bias": 3.0,
            "stage": "冬季限气",
            "confidence": 0.5,
        },
        "news": {"sentiment_lag1": None, "count_lag1": None},
        "event": {"is_event_day": False, "tags": []},
    }
    base.update(overrides)
    return base


def test_bands_are_sigma_adaptive() -> None:
    assert bands(0.6) == (0.5, 2.0)  # 小波动品种由下限兜底
    assert bands(4.0) == (1.4, 4.0)  # 大波动品种按 σ 放大
    fallback = bands(None)
    assert fallback[0] == pytest.approx(0.525)
    assert fallback[1] == pytest.approx(2.0)


def test_bucket_boundaries() -> None:
    assert classify_bucket(2.01, 0.6) == BUCKET_STRONG_UP
    assert classify_bucket(1.0, 0.6) == BUCKET_UP
    assert classify_bucket(0.5, 0.6) == BUCKET_FLAT
    assert classify_bucket(-0.5, 0.6) == BUCKET_FLAT
    assert classify_bucket(-1.0, 0.6) == BUCKET_DOWN
    assert classify_bucket(-2.01, 0.6) == BUCKET_STRONG_DOWN
    assert classify_bucket(2.5, 4.0) == BUCKET_UP  # σ=4 时强档带=4.0
    assert direction_of(BUCKET_UP) == 1
    assert direction_of(BUCKET_FLAT) == 0
    assert direction_of(BUCKET_STRONG_DOWN) == -1


def test_probabilities_sum_to_one_and_follow_direction() -> None:
    for exp_change in (-3.0, -0.4, 0.0, 0.4, 3.0):
        p_up, p_flat, p_down = probabilities(exp_change, 2.0)
        assert p_up + p_flat + p_down == pytest.approx(1.0, abs=1e-9)
        assert min(p_up, p_flat, p_down) >= 0
    up = probabilities(3.0, 2.0)
    down = probabilities(-3.0, 2.0)
    assert up[0] > up[2]
    assert down[2] > down[0]


def test_q_zero_sources_hidden_from_contributors() -> None:
    features = _features()
    features["linkage"] = [
        *features["linkage"],  # type: ignore[misc]
        {
            "name": "COMEX 铜",
            "ext": "HG",
            "prior": 0.20,
            "q": 0.0,
            "chg_1d": 1.5,
            "contrib": 0.0,
        },
    ]
    out = score(features)
    names = [c["name"] for c in out.contributor]
    assert "COMEX 铜" not in names
    assert out.direction == direction_of(out.bucket)


def test_score_deterministic_and_contributors_reconcile() -> None:
    features = _features()
    first = score(features)
    second = score(features)
    assert first == second
    # 每项贡献之和应回到 exp_change（展示值 2 位小数，允许舍入误差）
    total = sum(float(c["contribution"]) for c in first.contributor)
    assert total == pytest.approx(first.exp_change, abs=0.05)
    for item in first.contributor:
        assert {"name", "value", "weight", "q", "contribution"} <= set(item)
    assert first.narrative  # 模板解读非空


def test_missing_foreign_and_event_reduce_confidence() -> None:
    base = score(_features())
    degraded = score(_features(), missing_foreign=3)
    event = score(_features(), is_event_day=True)
    assert degraded.confidence == pytest.approx(base.confidence * 0.7, abs=1e-4)
    assert event.confidence == pytest.approx(base.confidence * 0.8, abs=1e-4)


async def test_engine_persists_traceable_prediction(db_session) -> None:  # type: ignore[no-untyped-def]
    session = db_session
    await session.execute(text("DELETE FROM prediction WHERE variety_code = 'MA'"))
    await session.execute(text("DELETE FROM feature_snapshot WHERE variety_code = 'MA'"))
    await import_knowledge(
        session, [d for d in load_knowledge_dir(KNOWLEDGE) if d.code == "MA"]
    )
    features = _features()
    features["asof"] = "2026-11-10T00:25:00Z"
    features["version"] = "f1"
    await session.execute(
        text(
            "INSERT INTO feature_snapshot (variety_code, asof, version, features)"
            " VALUES ('MA', :asof, 'f1', CAST(:f AS jsonb))"
        ),
        {"asof": dt.datetime(2026, 11, 10, 0, 25, tzinfo=dt.UTC), "f": _json(features)},
    )
    out = await predict_variety(session, "MA")
    assert out == ["rule-20261110-01"]  # 无融合模型 → 纯规则版本
    await session.flush()

    row = (
        await session.execute(
            text(
                "SELECT direction, bucket, signal, contributor::text AS c,"
                " model_version, target_date FROM prediction WHERE variety_code = 'MA'"
            )
        )
    ).one()
    expected = score(features)  # 引擎无融合模型时等价于规则直出（missing_foreign=0）
    assert row.bucket == expected.bucket
    assert row.direction == expected.direction
    assert row.signal == expected.signal
    assert row.model_version == "rule-20261110-01"
    assert row.target_date == dt.date(2026, 11, 10)
    import json

    contributor = json.loads(row.c)
    assert contributor and all(
        {"name", "value", "weight", "q", "contribution"} <= set(item)
        for item in contributor
    )

    # 同版本幂等
    await predict_variety(session, "MA")
    count = await session.scalar(
        text("SELECT count(*) FROM prediction WHERE variety_code = 'MA'")
    )
    assert count == 1
    await session.rollback()


def _json(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


async def test_five_consecutive_days_21_varieties_no_null(db_session) -> None:  # type: ignore[no-untyped-def]
    """DoD ④的自动化替身：连续 5 个目标日 × 21 品种，全部产出且无空值。"""
    session = db_session
    days = [dt.date(2026, 11, 2) + dt.timedelta(days=i) for i in range(5)]
    versions = [f"rule-{d:%Y%m%d}-01" for d in days]
    await session.execute(
        text("DELETE FROM prediction WHERE model_version = ANY(:v)"), {"v": versions}
    )
    await session.execute(
        text("DELETE FROM feature_snapshot WHERE variety_code = ANY(:c) AND version = 'f1'"),
        {"c": list(DOMESTIC_SINA_SYMBOLS)},
    )
    features = _features()
    features["seasonality"] = {}
    for code in DOMESTIC_SINA_SYMBOLS:
        for day in days:
            await session.execute(
                text(
                    "INSERT INTO feature_snapshot (variety_code, asof, version, features)"
                    " VALUES (:c, :asof, 'f1', CAST(:f AS jsonb))"
                ),
                {
                    "c": code,
                    "asof": dt.datetime.combine(day, dt.time(0, 30), tzinfo=dt.UTC),
                    "f": _json(features),
                },
            )
            await predict_variety(session, code)
    await session.flush()

    count, nulls = (
        await session.execute(
            text(
                "SELECT count(*), count(*) FILTER (WHERE p_up IS NULL OR p_flat IS NULL"
                " OR p_down IS NULL OR bucket IS NULL OR confidence IS NULL"
                " OR contributor IS NULL)"
                " FROM prediction WHERE model_version = ANY(:v)"
            ),
            {"v": versions},
        )
    ).one()
    assert count == len(DOMESTIC_SINA_SYMBOLS) * len(days)
    assert nulls == 0
    await session.rollback()
