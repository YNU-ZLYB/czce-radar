"""预测接口契约（/api/v1/predictions）。"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class ContributorItem(BaseModel):
    name: str
    value: float | None = Field(default=None, description="驱动取值（%，季节性为 null）")
    weight: float = Field(description="先验权重")
    q: float = Field(description="品种级校准系数")
    contribution: float = Field(description="对信号/期望涨跌幅的贡献")


class PredictionItem(BaseModel):
    variety_code: str
    target_date: dt.date
    asof: dt.datetime
    direction: int = Field(description="-1/0/1")
    bucket: str = Field(description="强空/偏空/震荡/偏多/强多")
    p_up: float
    p_flat: float
    p_down: float
    exp_change: float | None = None
    signal: int = Field(description="-100..100")
    confidence: float
    contributor: list[ContributorItem]
    model_version: str
    narrative: str | None = None


class PredictionListResponse(BaseModel):
    predictions: list[PredictionItem]
