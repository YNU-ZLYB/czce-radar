"""产业链知识库：YAML 加载与校验。

YAML 文件位于 ``data/knowledge/<code>.yaml``，结构见《02-数据与预测引擎》§8.1。
这里只做「读与验」，入库逻辑在 importer。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from czce_schema.symbols import DOMESTIC_SINA_SYMBOLS, FOREIGN_SYMBOLS

#: 仓单/库存/基差等产业指标的合法 kind（与 industry_metric.kind 对应）
METRIC_KINDS = {"warehouse_receipt", "inventory", "spot", "basis", "margin", "opening_rate"}


class DriverKind(StrEnum):
    """驱动因子的机理类型。"""

    COST = "cost"
    DEMAND = "demand"
    SUBSTITUTE = "substitute"
    POLICY = "policy"
    WEATHER = "weather"


class Driver(BaseModel):
    """一条传导边：上游/外盘/关联品种 → 本品种。"""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=48)
    kind: DriverKind
    #: 外盘代码（须在 FOREIGN_SYMBOLS），与 proxy_variety 二选一或都没有
    proxy_ext: str | None = None
    #: 国内关联品种代码（须在 21 品种内）
    proxy_variety: str | None = None
    #: 关联产业指标 kind（如 warehouse_receipt），有指标时特征层取数
    metric: str | None = None
    lag_days: int = Field(default=0, ge=0, le=30)
    #: 先验传导权重 0~1（来自 v1 手册，后续数据校准）
    prior: float = Field(ge=0.0, le=1.0)
    mechanism: str = Field(min_length=4)

    @field_validator("proxy_ext")
    @classmethod
    def _check_ext(cls, v: str | None) -> str | None:
        if v is not None and v not in FOREIGN_SYMBOLS:
            msg = f"未知外盘代码: {v}（合法: {sorted(FOREIGN_SYMBOLS)}）"
            raise ValueError(msg)
        return v

    @field_validator("proxy_variety")
    @classmethod
    def _check_variety(cls, v: str | None) -> str | None:
        if v is not None and v not in DOMESTIC_SINA_SYMBOLS:
            msg = f"未知品种代码: {v}（合法: {sorted(DOMESTIC_SINA_SYMBOLS)}）"
            raise ValueError(msg)
        return v

    @field_validator("metric")
    @classmethod
    def _check_metric(cls, v: str | None) -> str | None:
        if v is not None and v not in METRIC_KINDS:
            msg = f"未知指标 kind: {v}（合法: {sorted(METRIC_KINDS)}）"
            raise ValueError(msg)
        return v


class SeasonalStage(BaseModel):
    """季节性规律：某几个月的固定偏移（人工核实后生效）。"""

    model_config = ConfigDict(frozen=True)

    stage: str = Field(min_length=1, max_length=32)
    months: list[int] = Field(min_length=1, max_length=12)
    #: 信号分偏移，正=偏多，负=偏空
    bias: float = Field(ge=-10.0, le=10.0)
    #: 人工把握 0~1，≤0.5 视为弱规律
    confidence: float = Field(ge=0.0, le=1.0)
    note: str | None = None

    @field_validator("months")
    @classmethod
    def _check_months(cls, v: list[int]) -> list[int]:
        if not all(1 <= m <= 12 for m in v):
            msg = f"月份越界: {v}"
            raise ValueError(msg)
        return sorted(set(v))


class KnowledgeDoc(BaseModel):
    """一份品种知识 YAML 的完整结构。"""

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=2, max_length=8)
    name: str = Field(min_length=1, max_length=32)
    sector: str
    #: 产业链节点，键限定 upstream/midstream/downstream
    chain: dict[str, list[str]]
    drivers: list[Driver] = Field(min_length=1)
    seasonality: list[SeasonalStage] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    #: 品种级外盘传导校准系数（v1 回测实证；0=未校准/证伪）
    linkage_q: float = Field(default=0.0, ge=0.0, le=1.0)
    #: 用户核实签字：True 才允许参与模型特征权重
    reviewed: bool = False
    #: 核实人标记（user / ai）；reviewed=True 时写入 seasonality.reviewed_by
    reviewed_by: str | None = Field(default=None, max_length=16)

    @field_validator("code")
    @classmethod
    def _check_code(cls, v: str) -> str:
        if v not in DOMESTIC_SINA_SYMBOLS:
            msg = f"code {v} 不在 21 个可交易品种内"
            raise ValueError(msg)
        return v

    @field_validator("chain")
    @classmethod
    def _check_chain(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        allowed = {"upstream", "midstream", "downstream"}
        if not v or not set(v) <= allowed:
            msg = f"chain 键须为 {allowed} 的子集且非空"
            raise ValueError(msg)
        if not any(v.values()):
            msg = "chain 至少要有一个节点"
            raise ValueError(msg)
        return v


def load_knowledge_dir(directory: str | Path) -> list[KnowledgeDoc]:
    """加载目录下全部 ``*.yaml`` 并校验；code 重复即报错。"""
    root = Path(directory)
    if not root.is_dir():
        msg = f"知识库目录不存在: {root}"
        raise FileNotFoundError(msg)

    docs: list[KnowledgeDoc] = []
    seen: set[str] = set()
    for path in sorted(root.glob("*.yaml")):
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc = KnowledgeDoc.model_validate(raw)
        if doc.code in seen:
            msg = f"知识库 code 重复: {doc.code}（{path.name}）"
            raise ValueError(msg)
        seen.add(doc.code)
        docs.append(doc)
    if not docs:
        msg = f"知识库目录为空: {root}"
        raise ValueError(msg)
    return docs
