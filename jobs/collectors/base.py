"""采集器框架：BaseCollector 只负责「拿到」，落库与状态记录交给 runner。

约定：
- 每个 collector 一个 code，对应 source 表主键
- fetch() 纯网络操作，不做任何清洗以外的加工
- run() 负责：限速守卫 → 重试(3 次) → 调 ingest 回调落库 → fetch_log + source 状态
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RATE_LIMIT_FRACTION = 0.8  # 距上次运行不足 interval 的 80% 则跳过本轮


@dataclass(slots=True)
class RawItem:
    """采集器产出的原始数据单元；清洗归一由 ingest 负责。"""

    source: str
    external_id: str
    ts: dt.datetime | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FetchResult:
    status: str  # ok / empty / error / skip
    fetched: int = 0
    inserted: int = 0
    error: str | None = None
    duration_ms: int = 0


class IngestFn(Protocol):
    async def __call__(self, conn: AsyncConnection, items: list[RawItem]) -> int: ...


class BaseCollector(ABC):
    code: ClassVar[str]
    name: ClassVar[str]
    kind: ClassVar[str]
    interval_sec: ClassVar[int] = 60

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @abstractmethod
    async def fetch(self) -> list[RawItem]: ...

    async def run(
        self,
        conn: AsyncConnection,
        ingest: IngestFn | None = None,
        *,
        last_run_at: dt.datetime | None = None,
    ) -> FetchResult:
        """执行一轮采集：限速 → 重试 → 落库 → 记录。任何异常都不会向外传播。"""
        now = dt.datetime.now(dt.UTC)
        if (
            last_run_at is not None
            and (now - last_run_at).total_seconds() < self.interval_sec * RATE_LIMIT_FRACTION
        ):
            return FetchResult(status="skip")

        started = time.perf_counter()
        items: list[RawItem] | None = None
        error: str | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                items = await self.fetch()
                error = None
                break
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                if attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
        duration_ms = int((time.perf_counter() - started) * 1000)

        inserted = 0
        status = "error"
        if items is not None:
            if ingest is not None:
                inserted = await ingest(conn, items)
            status = "ok" if items else "empty"

        await self._record(conn, status, len(items or []), inserted, error, duration_ms)
        return FetchResult(status, len(items or []), inserted, error, duration_ms)

    async def _record(
        self,
        conn: AsyncConnection,
        status: str,
        fetched: int,
        inserted: int,
        error: str | None,
        duration_ms: int,
    ) -> None:
        await conn.execute(
            text(
                "INSERT INTO fetch_log (source_code, status, fetched, inserted, error,"
                " duration_ms, started_at)"
                " VALUES (:code, :status, :fetched, :inserted, :error, :duration, :started)"
            ),
            {
                "code": self.code,
                "status": status,
                "fetched": fetched,
                "inserted": inserted,
                "error": error,
                "duration": duration_ms,
                "started": dt.datetime.now(dt.UTC),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO source (code, name, kind, interval_sec, enabled,"
                " last_status, last_error, last_run_at)"
                " VALUES (:code, :name, :kind, :interval, TRUE, :status, :error, :run)"
                " ON CONFLICT (code) DO UPDATE SET"
                " last_status = EXCLUDED.last_status,"
                " last_error = EXCLUDED.last_error,"
                " last_run_at = EXCLUDED.last_run_at"
            ),
            {
                "code": self.code,
                "name": self.name,
                "kind": self.kind,
                "interval": self.interval_sec,
                "status": status,
                "error": error,
                "run": dt.datetime.now(dt.UTC),
            },
        )
        if error:
            logger.warning("collector_error source=%s error=%s", self.code, error)
