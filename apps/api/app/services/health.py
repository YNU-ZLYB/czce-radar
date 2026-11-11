"""健康检查：db / redis / worker 三态。"""

from __future__ import annotations

import asyncio
import datetime as dt

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from czce_schema.constants import WORKER_HEARTBEAT_KEY
from czce_schema.health import ComponentStatus, HealthResponse

CHECK_TIMEOUT_SEC = 3.0


async def _check_db(session: AsyncSession) -> tuple[ComponentStatus, str | None]:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SEC):
            await session.execute(text("SELECT 1"))
        return ComponentStatus.OK, None
    except Exception as exc:
        return ComponentStatus.ERROR, f"db: {type(exc).__name__}"


async def _check_redis(client: Redis) -> tuple[ComponentStatus, str | None]:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SEC):
            pong = await client.ping()
        if pong:
            return ComponentStatus.OK, None
        return ComponentStatus.ERROR, "redis: ping 未返回 PONG"
    except Exception as exc:
        return ComponentStatus.ERROR, f"redis: {type(exc).__name__}"


async def _check_worker(client: Redis, ttl_sec: int) -> tuple[ComponentStatus, str | None]:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SEC):
            raw = await client.get(WORKER_HEARTBEAT_KEY)
        if raw is None:
            return ComponentStatus.ERROR, "worker: 无心跳记录"
        age = dt.datetime.now(dt.UTC).timestamp() - float(raw)
        if age <= ttl_sec:
            return ComponentStatus.OK, None
        return ComponentStatus.ERROR, f"worker: 心跳已过期 {int(age)}s"
    except Exception as exc:
        return ComponentStatus.ERROR, f"worker: {type(exc).__name__}"


async def build_health(
    session: AsyncSession,
    client: Redis,
    *,
    version: str,
    worker_ttl_sec: int,
) -> HealthResponse:
    (db_status, db_err), (redis_status, redis_err) = await asyncio.gather(
        _check_db(session),
        _check_redis(client),
    )
    if redis_status is ComponentStatus.OK:
        worker_status, worker_err = await _check_worker(client, worker_ttl_sec)
    else:
        worker_status, worker_err = (
            ComponentStatus.ERROR,
            "worker: Redis 不可用，无法确认心跳",
        )

    detail = {
        name: err
        for name, err in (("db", db_err), ("redis", redis_err), ("worker", worker_err))
        if err
    }
    all_ok = all(
        status is ComponentStatus.OK for status in (db_status, redis_status, worker_status)
    )
    return HealthResponse(
        status="ok" if all_ok else "degraded",
        db=db_status,
        redis=redis_status,
        worker=worker_status,
        version=version,
        time=dt.datetime.now(dt.UTC),
        detail=detail or None,
    )


def is_ready(report: HealthResponse) -> bool:
    """API 能否对外服务只看 db / redis。

    worker 是独立进程，它掉线不应该让平台反复重启 API；
    worker 状态只在 ``status`` 里降级展示，由前端提示。
    """
    return report.db is ComponentStatus.OK and report.redis is ComponentStatus.OK
