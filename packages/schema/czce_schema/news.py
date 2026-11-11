"""资讯修订与 LLM 成本接口契约。"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class TriggerDoc(BaseModel):
    id: int
    title: str
    url: str | None = None
    source: str
    published_at: dt.datetime | None = None


class RevisionItem(BaseModel):
    id: int
    prediction_id: int
    variety_code: str
    target_date: dt.date
    at: dt.datetime
    prev_signal: int | None = None
    new_signal: int | None = None
    prev_direction: int | None = None
    new_direction: int | None = None
    reason: str
    is_surprise: bool
    trigger_doc: TriggerDoc | None = None


class RevisionListResponse(BaseModel):
    revisions: list[RevisionItem]
    alerts: list[str] = Field(default_factory=list, description="单品种单日修订 >3 次的品种")


class LlmStatsResponse(BaseModel):
    date: dt.date
    calls: int
    cache_hits: int
    cache_hit_rate: float
    cost_cny: float
    budget_cny: float
    degraded: bool
    llm_enabled: bool
