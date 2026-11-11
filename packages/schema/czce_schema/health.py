"""健康检查契约。"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ComponentStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class LiveResponse(BaseModel):
    """存活探针：只说明进程还能响应，不触碰任何外部依赖。"""

    status: Literal["alive"]
    version: str
    time: dt.datetime


class HealthResponse(BaseModel):
    """就绪状态：db / redis 决定是否可对外服务，worker 仅作展示。"""

    status: Literal["ok", "degraded"] = Field(
        description="ok 表示三态全部正常；degraded 表示至少一项异常"
    )
    db: ComponentStatus
    redis: ComponentStatus
    worker: ComponentStatus
    version: str
    time: dt.datetime
    detail: dict[str, str] | None = None
