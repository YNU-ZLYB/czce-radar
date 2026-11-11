"""Redis 客户端（队列与短期缓存）。Redis 丢数据不影响正确性。"""

from __future__ import annotations

from redis.asyncio import Redis

from app.core.config import Settings

_client: Redis | None = None


def init_redis(settings: Settings) -> Redis:
    global _client
    _client = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
        health_check_interval=30,
    )
    return _client


def get_redis_client() -> Redis:
    if _client is None:
        raise RuntimeError("Redis 尚未初始化：请先调用 init_redis()")
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def redis_dependency() -> Redis:
    return get_redis_client()
