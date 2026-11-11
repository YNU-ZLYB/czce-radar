"""产品前端聚合接口契约（详情抽屉 / Scorecard / 简报）。"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field

from czce_schema.news import RevisionItem
from czce_schema.predictions import PredictionItem


class VarietyInfo(BaseModel):
    code: str
    name: str
    sector: str


class VarietyListResponse(BaseModel):
    varieties: list[VarietyInfo] = Field(default_factory=list)


class SignalPoint(BaseModel):
    target_date: dt.date
    signal: int
    direction: int
    bucket: str
    confidence: float


class PricePoint(BaseModel):
    date: dt.date
    close: float


class NewsRef(BaseModel):
    """利多/利空资讯摘录（含原文跳转）。"""

    doc_id: int
    title: str
    url: str | None = None
    source: str
    published_at: dt.datetime | None = None
    direction: int
    strength: int
    chain_stage: str | None = None
    is_surprise: bool
    evidence: str | None = None


class ModelInfo(BaseModel):
    version: str
    kind: str
    status: str
    calibration: dict[str, float | int | str | None] = Field(
        default_factory=dict, description="样本外校准指标（ECE/Brier/命中率与窗口）"
    )


class VarietyDetailResponse(BaseModel):
    variety: VarietyInfo
    prediction: PredictionItem | None = None
    signal_history: list[SignalPoint] = Field(default_factory=list)
    prices: list[PricePoint] = Field(default_factory=list)
    bullish: list[NewsRef] = Field(default_factory=list)
    bearish: list[NewsRef] = Field(default_factory=list)
    revisions: list[RevisionItem] = Field(default_factory=list)
    model: ModelInfo | None = None


class ScorecardDayItem(BaseModel):
    variety_code: str
    name: str
    bucket: str
    signal: int
    actual_change: float
    actual_bucket: str
    hit: bool


class ScorecardDay(BaseModel):
    date: dt.date
    n: int
    direction_accuracy: float | None = None
    items: list[ScorecardDayItem] = Field(default_factory=list)


class ScorecardVariety(BaseModel):
    variety_code: str
    name: str
    n: int
    coverage: float
    direction_accuracy: float | None = None


class ScorecardOverall(BaseModel):
    n: int
    coverage: float
    direction_n: int
    direction_accuracy: float | None = None
    brier: float | None = None


class ScorecardResponse(BaseModel):
    window_days: int
    model_version: str | None = None
    scored_through: dt.date | None = None
    overall: ScorecardOverall
    by_day: list[ScorecardDay] = Field(default_factory=list)
    by_variety: list[ScorecardVariety] = Field(default_factory=list)
    latest: ScorecardDay | None = None


class DocRef(BaseModel):
    id: int
    title: str
    url: str | None = None
    source: str
    published_at: dt.datetime | None = None


class ExtractItem(BaseModel):
    id: int
    variety_code: str
    variety_name: str
    direction: int
    strength: int
    chain_stage: str | None = None
    is_surprise: bool
    method: str
    evidence: str | None = None
    doc: DocRef | None = None


class ExtractListResponse(BaseModel):
    extracts: list[ExtractItem] = Field(default_factory=list)
