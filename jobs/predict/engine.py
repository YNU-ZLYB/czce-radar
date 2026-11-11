"""预测引擎：读最新 FeatureSnapshot → Layer A 打分 → （可选）Layer C 融合 → upsert prediction。

边界：引擎只读特征快照与知识库，不采集、不调 LLM、不使用 display_only。
融合行为（《02》§5.3）：
- 无 active/shadow 融合模型 → 纯规则路径（model_version=rule-YYYYMMDD-01，M3 行为）
- 有融合模型 → 每个模型版本各写一行（active 展示、shadow 并行产出不展示）
- GBM 产物缺失/加载失败 → 该版本退化为规则概率，置信度 × 0.8
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.models.prediction import Prediction
from jobs.features.build import FEATURE_VERSION
from jobs.predict.fusion import (
    FusionModel,
    bucket_from_probs,
    direction_from_probs,
    fuse_probs,
    load_edges,
    load_fusion_models,
)
from jobs.predict.rules import RuleOutput, score
from jobs.train.dataset import Edge, feature_dict
from jobs.train.gbm import IsotonicCalibrator, load_artifacts, predict_proba

logger = logging.getLogger(__name__)

Conn = AsyncConnection | AsyncSession
DEFAULT_HORIZON = "1d"

#: (version, code) → (booster, calibrator)；None 表示加载失败（本次进程内不再重试）
_ARTIFACT_CACHE: dict[tuple[str, str], tuple[Any, IsotonicCalibrator] | None] = {}


def model_version_for(day: dt.date) -> str:
    return f"rule-{day:%Y%m%d}-01"


async def _missing_foreign(conn: Conn, code: str, features: dict[str, Any]) -> int:
    ext_codes = (
        await conn.execute(
            text(
                "SELECT DISTINCT proxy_ext FROM knowledge_edge"
                " WHERE variety_code = :c AND proxy_ext IS NOT NULL"
            ),
            {"c": code},
        )
    ).scalars()
    foreign = features.get("foreign") or {}
    missing = 0
    for ext in ext_codes:
        item = foreign.get(ext)
        if not item or item.get("chg_1d") is None:
            missing += 1
    return missing


def _vectorize(
    model: FusionModel,
    code: str,
    features: dict[str, Any],
    edges: list[dict[str, Any]],
    pooled: bool,
) -> list[float]:
    """从 f1 快照构造与训练一致的固定长度向量（缺失 → NaN）。"""
    params = model.params
    season = features.get("seasonality") or {}
    news = features.get("news") or {}
    event = features.get("event") or {}
    feats = feature_dict(
        price=features.get("price") or {},
        foreign=features.get("foreign") or {},
        edges=[Edge(ext=e["ext"], prior=e["prior"], q=e["q"]) for e in edges],
        industry=features.get("industry") or {},
        season_bias=season.get("bias"),
        season_conf=season.get("confidence"),
        month=int(season.get("month") or 0),
        news_sentiment_lag1=news.get("sentiment_lag1"),
        news_count_lag1=news.get("count_lag1"),
        is_event_day=bool(event.get("is_event_day")),
        ext_codes=params.get("ext_codes") or [],
    )
    names = params["pooled_feature_names"] if pooled else params["feature_names"]
    vec: list[float] = []
    for n in names:
        if n.startswith("var_"):
            vec.append(1.0 if code == n[4:] else 0.0)
            continue
        v = feats.get(n)
        vec.append(float("nan") if v is None else float(v))
    return vec


def _gbm_probs(
    model: FusionModel, code: str, features: dict[str, Any], edges: list[dict[str, Any]]
) -> list[float] | None:
    """加载产物并输出校准后 [p_down, p_flat, p_up]；任何失败返回 None。"""
    entry = (model.params.get("models") or {}).get(code)
    if entry is None:
        return None
    cache_key = (model.version, code)
    if cache_key not in _ARTIFACT_CACHE:
        try:
            _ARTIFACT_CACHE[cache_key] = load_artifacts(entry["artifact"])
        except Exception:
            logger.warning("gbm_artifact_load_failed version=%s code=%s", model.version, code,
                           exc_info=True)
            _ARTIFACT_CACHE[cache_key] = None
    artifacts = _ARTIFACT_CACHE[cache_key]
    if artifacts is None:
        return None
    booster, calibrator = artifacts
    try:
        vec = _vectorize(model, code, features, edges, bool(entry.get("pooled")))
        raw = predict_proba(booster, [vec])
        return calibrator.transform(raw)[0]
    except Exception:
        logger.warning("gbm_predict_failed version=%s code=%s", model.version, code, exc_info=True)
        return None


async def _upsert_prediction(
    conn: Conn,
    *,
    code: str,
    target_date: dt.date,
    asof: dt.datetime,
    direction: int,
    bucket: str,
    p_down: float,
    p_flat: float,
    p_up: float,
    confidence: float,
    out: RuleOutput,
    model_version: str,
) -> None:
    stmt = pg_insert(Prediction).values(
        variety_code=code,
        target_date=target_date,
        horizon=DEFAULT_HORIZON,
        asof=asof,
        direction=direction,
        bucket=bucket,
        p_up=round(p_up, 4),
        p_flat=round(p_flat, 4),
        p_down=round(p_down, 4),
        exp_change=out.exp_change,
        signal=out.signal,
        confidence=round(confidence, 4),
        contributor=out.contributor,
        model_version=model_version,
        narrative=out.narrative,
    )
    await conn.execute(
        stmt.on_conflict_do_update(
            constraint="uq_prediction_variety_target_horizon_model",
            set_={
                "asof": stmt.excluded.asof,
                "direction": stmt.excluded.direction,
                "bucket": stmt.excluded.bucket,
                "p_up": stmt.excluded.p_up,
                "p_flat": stmt.excluded.p_flat,
                "p_down": stmt.excluded.p_down,
                "exp_change": stmt.excluded.exp_change,
                "signal": stmt.excluded.signal,
                "confidence": stmt.excluded.confidence,
                "contributor": stmt.excluded.contributor,
                "narrative": stmt.excluded.narrative,
            },
        )
    )


async def predict_variety(conn: Conn, code: str) -> list[str] | None:
    """为单品种生成并 upsert 预测（每个生效模型版本一行）；没有快照时返回 None。"""
    snap = (
        await conn.execute(
            text(
                "SELECT asof, features FROM feature_snapshot"
                " WHERE variety_code = :c AND version = :v"
                " ORDER BY asof DESC LIMIT 1"
            ),
            {"c": code, "v": FEATURE_VERSION},
        )
    ).first()
    if snap is None or not snap.features:
        return None

    features: dict[str, Any] = snap.features
    asof = snap.asof.astimezone(dt.UTC)
    target_date = asof.date()
    missing = await _missing_foreign(conn, code, features)
    out = score(features, missing_foreign=missing)
    is_event = bool((features.get("event") or {}).get("is_event_day"))

    # 1) 永远先写规则基线行（无 active 融合时它就是展示版本，保证产出连续）
    rule_version = model_version_for(target_date)
    await _upsert_prediction(
        conn,
        code=code,
        target_date=target_date,
        asof=asof,
        direction=out.direction,
        bucket=out.bucket,
        p_down=out.p_down,
        p_flat=out.p_flat,
        p_up=out.p_up,
        confidence=out.confidence,
        out=out,
        model_version=rule_version,
    )
    versions = [rule_version]

    # 2) 每个 active/shadow 融合模型各写一行（active 展示；shadow 并行产出不展示）
    models = await load_fusion_models(conn)
    if models:
        edges = await load_edges(conn, code)
    for model in models:
        p_gbm = _gbm_probs(model, code, features, edges)
        fw = (model.params.get("fusion") or {}).get(code) or {}
        w_rule = float(fw.get("w_rule", 1.0))
        w_gbm = float(fw.get("w_gbm", 0.0))
        fused = fuse_probs((out.p_down, out.p_flat, out.p_up), p_gbm, w_rule, w_gbm)
        confidence = max(fused)
        if p_gbm is None:
            confidence *= 0.8  # §5.1：B 缺失降级兜底
        if missing > 2:
            confidence *= 0.7
        if is_event:
            confidence *= 0.8
        await _upsert_prediction(
            conn,
            code=code,
            target_date=target_date,
            asof=asof,
            direction=direction_from_probs(fused),
            bucket=bucket_from_probs(fused),
            p_down=fused[0],
            p_flat=fused[1],
            p_up=fused[2],
            confidence=min(confidence, 1.0),
            out=out,
            model_version=model.version,
        )
        versions.append(model.version)
    return versions


async def predict_varieties(
    session_factory: async_sessionmaker[AsyncSession], codes: list[str]
) -> dict[str, Any]:
    """批量预测（每个品种独立事务，单品种失败不拖累其余）。"""
    produced: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    for code in sorted(codes):
        try:
            async with session_factory() as session, session.begin():
                versions = await predict_variety(session, code)
            if versions is None:
                skipped.append(code)
            else:
                produced.append(code)
        except Exception:
            logger.exception("predict_failed code=%s", code)
            failed.append(code)
    return {"produced": produced, "skipped": skipped, "failed": failed}
