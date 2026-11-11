"""产品前端聚合接口：品种详情抽屉 + Scorecard（只读）。Scorecard 口径与回测一致。"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_dependency
from czce_schema.dashboard import (
    ModelInfo,
    NewsRef,
    PricePoint,
    ScorecardResponse,
    SignalPoint,
    VarietyDetailResponse,
    VarietyInfo,
    VarietyListResponse,
)
from czce_schema.news import RevisionItem, TriggerDoc
from czce_schema.predictions import PredictionItem
from jobs.predict.scorecard import compute_scorecard

router = APIRouter(prefix="/api/v1", tags=["dashboard"])

SessionDep = Annotated[AsyncSession, Depends(session_dependency)]


async def _active_fusion_version(session: AsyncSession) -> str | None:
    return await session.scalar(
        text(
            "SELECT version FROM model_registry"
            " WHERE kind = 'fusion' AND status = 'active'"
            " ORDER BY created_at DESC LIMIT 1"
        )
    )


def _row_to_prediction(row: Any) -> PredictionItem:
    raw = row.contributor
    contributor = json.loads(raw) if isinstance(raw, str) else raw or []
    return PredictionItem(
        variety_code=row.variety_code,
        target_date=row.target_date,
        asof=row.asof,
        direction=row.direction,
        bucket=row.bucket,
        p_up=float(row.p_up),
        p_flat=float(row.p_flat),
        p_down=float(row.p_down),
        exp_change=float(row.exp_change) if row.exp_change is not None else None,
        signal=row.signal,
        confidence=float(row.confidence),
        contributor=contributor,
        model_version=row.model_version,
        narrative=row.narrative,
    )


@router.get(
    "/varieties",
    response_model=VarietyListResponse,
    summary="可交易品种列表（按 sort_order 排序，供首页卡片使用）",
)
async def list_varieties(session: SessionDep) -> VarietyListResponse:
    rows = (
        await session.execute(
            text(
                "SELECT code, name, sector FROM variety"
                " WHERE is_tradable ORDER BY sort_order, code"
            )
        )
    ).all()
    return VarietyListResponse(
        varieties=[VarietyInfo(code=row.code, name=row.name, sector=row.sector) for row in rows]
    )


@router.get(
    "/varieties/{code}/detail",
    response_model=VarietyDetailResponse,
    summary="品种详情抽屉聚合：最新预测 + 信号/价格历史 + 利多利空 + 修订 + 校准指标",
)
async def variety_detail(session: SessionDep, code: str) -> VarietyDetailResponse:
    variety = (
        await session.execute(
            text("SELECT code, name, sector FROM variety WHERE code = :code"), {"code": code}
        )
    ).first()
    if variety is None:
        raise HTTPException(status_code=404, detail=f"未知品种：{code}")

    fusion = await _active_fusion_version(session)
    target = await session.scalar(
        text("SELECT max(target_date) FROM prediction WHERE variety_code = :code"),
        {"code": code},
    )

    prediction: PredictionItem | None = None
    serving: str | None = None
    if target is not None:
        serving = fusion or f"rule-{target:%Y%m%d}-01"
        row = (
            await session.execute(
                text(
                    "SELECT variety_code, target_date, asof, direction, bucket,"
                    "       p_up, p_flat, p_down, exp_change, signal, confidence,"
                    "       contributor::text AS contributor, model_version, narrative"
                    " FROM prediction"
                    " WHERE variety_code = :code AND target_date = :d AND model_version = :mv"
                ),
                {"code": code, "d": target, "mv": serving},
            )
        ).first()
        if row is not None:
            prediction = _row_to_prediction(row)

    if fusion:
        history_rows = (
            await session.execute(
                text(
                    "SELECT target_date, signal, direction, bucket, confidence"
                    " FROM prediction WHERE variety_code = :code AND model_version = :mv"
                    " ORDER BY target_date DESC LIMIT 30"
                ),
                {"code": code, "mv": fusion},
            )
        ).all()
    else:
        history_rows = (
            await session.execute(
                text(
                    "SELECT target_date, signal, direction, bucket, confidence"
                    " FROM prediction"
                    " WHERE variety_code = :code AND model_version LIKE 'rule-%'"
                    " ORDER BY target_date DESC LIMIT 30"
                ),
                {"code": code},
            )
        ).all()
    signal_history = [
        SignalPoint(
            target_date=row.target_date,
            signal=row.signal,
            direction=row.direction,
            bucket=row.bucket,
            confidence=float(row.confidence),
        )
        for row in reversed(history_rows)
    ]

    price_rows = (
        await session.execute(
            text(
                "SELECT trade_date, close FROM daily_bar"
                " WHERE variety_code = :code ORDER BY trade_date DESC LIMIT 60"
            ),
            {"code": code},
        )
    ).all()
    prices = [
        PricePoint(date=row.trade_date, close=float(row.close))
        for row in reversed(price_rows)
    ]

    news_rows = (
        await session.execute(
            text(
                "SELECT d.id AS doc_id, d.title, d.url, d.source, d.published_at,"
                "       e.direction, e.strength, e.chain_stage, e.is_surprise, e.evidence"
                " FROM news_extract e JOIN news_doc d ON d.id = e.doc_id"
                " WHERE e.variety_code = :code AND e.direction <> 0"
                "   AND d.published_at >= now() - interval '7 days'"
                " ORDER BY e.strength DESC, d.published_at DESC LIMIT 20"
            ),
            {"code": code},
        )
    ).all()

    def _news_ref(row: Any) -> NewsRef:
        return NewsRef(
            doc_id=row.doc_id,
            title=row.title,
            url=row.url,
            source=row.source,
            published_at=row.published_at,
            direction=row.direction,
            strength=row.strength,
            chain_stage=row.chain_stage,
            is_surprise=row.is_surprise,
            evidence=row.evidence,
        )

    bullish = [_news_ref(r) for r in news_rows if r.direction > 0][:3]
    bearish = [_news_ref(r) for r in news_rows if r.direction < 0][:3]

    revision_rows = (
        await session.execute(
            text(
                "SELECT r.id, r.prediction_id, p.variety_code, p.target_date, r.at,"
                "       r.prev_signal, r.new_signal, r.prev_direction, r.new_direction,"
                "       r.reason, r.is_surprise,"
                "       d.id AS doc_id, d.title AS doc_title, d.url AS doc_url,"
                "       d.source AS doc_source, d.published_at AS doc_published_at"
                " FROM prediction_revision r"
                " JOIN prediction p ON p.id = r.prediction_id"
                " LEFT JOIN news_doc d ON d.id = r.trigger_doc_id"
                " WHERE p.variety_code = :code"
                " ORDER BY r.id DESC LIMIT 10"
            ),
            {"code": code},
        )
    ).all()
    revisions = [
        RevisionItem(
            id=row.id,
            prediction_id=row.prediction_id,
            variety_code=row.variety_code,
            target_date=row.target_date,
            at=row.at,
            prev_signal=row.prev_signal,
            new_signal=row.new_signal,
            prev_direction=row.prev_direction,
            new_direction=row.new_direction,
            reason=row.reason,
            is_surprise=row.is_surprise,
            trigger_doc=(
                TriggerDoc(
                    id=row.doc_id,
                    title=row.doc_title,
                    url=row.doc_url,
                    source=row.doc_source,
                    published_at=row.doc_published_at,
                )
                if row.doc_id is not None
                else None
            ),
        )
        for row in revision_rows
    ]

    model: ModelInfo | None = None
    if fusion:
        reg = (
            await session.execute(
                text(
                    "SELECT version, kind, status, metrics FROM model_registry"
                    " WHERE version = :v"
                ),
                {"v": fusion},
            )
        ).first()
        if reg is not None:
            metrics = reg.metrics or {}
            holdout = metrics.get("holdout", {}) if isinstance(metrics, dict) else {}
            model = ModelInfo(
                version=reg.version,
                kind=reg.kind,
                status=reg.status,
                calibration={
                    "ece": holdout.get("ece"),
                    "brier_direction": holdout.get("brier_direction"),
                    "direction_accuracy": holdout.get("direction_accuracy"),
                    "window": "holdout-60",
                },
            )
    elif serving:
        run = (
            await session.execute(
                text(
                    "SELECT metrics FROM backtest_run"
                    " ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).first()
        calibration: dict[str, float | int | str | None] = {}
        if run is not None and isinstance(run.metrics, dict):
            holdout = run.metrics.get("holdout", {})
            calibration = {
                "ece": holdout.get("ece"),
                "brier_direction": holdout.get("brier_direction"),
                "direction_accuracy": holdout.get("direction_accuracy"),
                "window": "backtest-holdout-60",
            }
        model = ModelInfo(
            version=serving, kind="rule", status="active", calibration=calibration
        )

    return VarietyDetailResponse(
        variety=VarietyInfo(code=variety.code, name=variety.name, sector=variety.sector),
        prediction=prediction,
        signal_history=signal_history,
        prices=prices,
        bullish=bullish,
        bearish=bearish,
        revisions=revisions,
        model=model,
    )


@router.get(
    "/scorecard",
    response_model=ScorecardResponse,
    summary="事后评分看板：滚动窗口命中率 / 分品种 / 最新一日明细（与回测同口径）",
)
async def scorecard(
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=400)] = 60,
) -> ScorecardResponse:
    return await compute_scorecard(session, days=days)
