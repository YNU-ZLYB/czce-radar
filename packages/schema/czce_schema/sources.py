"""数据源状态契约（/api/v1/sources）。"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class SourceStatus(BaseModel):
    code: str
    name: str
    kind: str = Field(description="quote/news/metric 等")
    interval_sec: int | None = None
    enabled: bool = True
    last_status: str | None = Field(default=None, description="ok/empty/error/skip")
    last_error: str | None = None
    last_run_at: dt.datetime | None = None
    last_fetched: int | None = Field(default=None, description="最近一次抓取条数")
    last_inserted: int | None = Field(default=None, description="最近一次新插入条数")


class SourceListResponse(BaseModel):
    sources: list[SourceStatus]
