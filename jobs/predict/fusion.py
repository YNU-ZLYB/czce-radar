"""融合层（《02》§5.3）：Layer A 规则 + Layer B GBM 按样本外 logloss 加权。

- final_p = w_A × p_A + w_B × p_B（权重在训练时按最近 60 样本外 logloss 固化进 registry）
- 任一模型缺失 → 单模型输出，置信度 × 0.8（§5.1 兜底条款）
- 融合概率决定 direction 与 confidence；bucket 由 (direction, 获胜概率) 推出，
  与 rules.direction_of 的映射保持一致；exp_change / signal / contributor 仍来自规则层
  （展示口径不变）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from jobs.predict.rules import (
    BUCKET_DOWN,
    BUCKET_FLAT,
    BUCKET_STRONG_DOWN,
    BUCKET_STRONG_UP,
    BUCKET_UP,
)

logger = logging.getLogger(__name__)

Conn = AsyncConnection | AsyncSession

STRONG_PROB = 0.6  # 获胜概率 ≥0.6 记强档，与 σ 强档带的语义对齐（确定性、可测）


@dataclass(frozen=True, slots=True)
class FusionModel:
    version: str
    status: str  # active / shadow
    params: dict[str, Any]


async def load_fusion_models(conn: Conn) -> list[FusionModel]:
    """读出 active + shadow 的融合模型（每个预测周期实时查询，切换即时生效）。"""
    rows = (
        await conn.execute(
            text(
                "SELECT version, status, params FROM model_registry"
                " WHERE kind = 'fusion' AND status IN ('active', 'shadow')"
                " ORDER BY created_at DESC"
            )
        )
    ).all()
    return [FusionModel(version=r.version, status=r.status, params=r.params or {}) for r in rows]


async def load_edges(conn: Conn, code: str) -> list[dict[str, Any]]:
    rows = (
        await conn.execute(
            text(
                "SELECT e.proxy_ext, e.prior, v.linkage_q"
                " FROM knowledge_edge e JOIN variety v ON v.code = e.variety_code"
                " WHERE e.variety_code = :c AND e.proxy_ext IS NOT NULL"
            ),
            {"c": code},
        )
    ).all()
    return [
        {"ext": r.proxy_ext, "prior": float(r.prior), "q": float(r.linkage_q)} for r in rows
    ]


def fuse_probs(
    p_rule: tuple[float, float, float],
    p_gbm: list[float] | None,
    w_rule: float,
    w_gbm: float,
) -> list[float]:
    """(p_down, p_flat, p_up) 加权融合；GBM 缺失时退化为规则概率（调用方负责置信度折减）。"""
    if p_gbm is None or w_gbm <= 0:
        return [p_rule[0], p_rule[1], p_rule[2]]
    fused = [w_rule * r + w_gbm * g for r, g in zip(p_rule, p_gbm, strict=True)]
    total = sum(fused)
    return [v / total for v in fused] if total > 0 else [1 / 3, 1 / 3, 1 / 3]


def direction_from_probs(p: list[float]) -> int:
    """argmax(p_down, p_flat, p_up) → -1/0/1。"""
    idx = max(range(3), key=lambda i: p[i])
    return -1 if idx == 0 else 0 if idx == 1 else 1


def bucket_from_probs(p: list[float]) -> str:
    """由融合概率推出 bucket，保证 bucket↔direction 永远一致。"""
    direction = direction_from_probs(p)
    if direction == 0:
        return BUCKET_FLAT
    strong = max(p) >= STRONG_PROB
    if direction > 0:
        return BUCKET_STRONG_UP if strong else BUCKET_UP
    return BUCKET_STRONG_DOWN if strong else BUCKET_DOWN


async def activate_model(conn: Conn, version: str) -> Any:
    """把指定版本置为 active，其余 fusion/gbm 版本退回 retired（调用方负责事务）。

    返回切换后的 (version, status) 行；版本不存在返回 None。
    """
    row = (
        await conn.execute(
            text("SELECT version, status FROM model_registry WHERE version = :v"),
            {"v": version},
        )
    ).first()
    if row is None:
        return None
    await conn.execute(
        text(
            "UPDATE model_registry SET status = 'retired'"
            " WHERE kind IN ('fusion', 'gbm') AND status = 'active' AND version <> :v"
        ),
        {"v": version},
    )
    await conn.execute(
        text("UPDATE model_registry SET status = 'active' WHERE version = :v"),
        {"v": version},
    )
    return (
        await conn.execute(
            text("SELECT version, status FROM model_registry WHERE version = :v"),
            {"v": version},
        )
    ).first()
