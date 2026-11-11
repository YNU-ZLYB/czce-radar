"""Worker 心跳与注册任务。

心跳写入 Redis（带 TTL）：API 的 /ready 通过读取心跳判断 worker 是否存活。
"""

from __future__ import annotations

import time
from typing import Any

from redis.asyncio import Redis

from czce_schema.constants import WORKER_HEARTBEAT_KEY

DEFAULT_TTL_SEC = 90


async def write_heartbeat(ctx: dict[str, Any]) -> None:
    redis: Redis = ctx["redis"]
    ttl = int(ctx.get("heartbeat_ttl_sec", DEFAULT_TTL_SEC))
    await redis.set(WORKER_HEARTBEAT_KEY, str(time.time()), ex=ttl)
