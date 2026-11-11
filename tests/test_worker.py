from __future__ import annotations

import time

from czce_schema.constants import WORKER_HEARTBEAT_KEY
from jobs.heartbeat import write_heartbeat
from jobs.worker import shutdown, startup


class RecordingRedis:
    def __init__(self) -> None:
        self.store: dict[str, tuple[str, int | None]] = {}
        self.deleted: list[str] = []

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = (value, ex)

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.store.pop(key, None)


async def test_write_heartbeat_sets_key_with_ttl() -> None:
    redis = RecordingRedis()
    before = time.time()
    await write_heartbeat({"redis": redis, "heartbeat_ttl_sec": 45})
    value, ttl = redis.store[WORKER_HEARTBEAT_KEY]
    assert ttl == 45
    assert float(value) >= before


async def test_write_heartbeat_uses_default_ttl() -> None:
    redis = RecordingRedis()
    await write_heartbeat({"redis": redis})
    assert redis.store[WORKER_HEARTBEAT_KEY][1] == 90


async def test_startup_writes_immediate_heartbeat() -> None:
    redis = RecordingRedis()
    await startup({"redis": redis, "heartbeat_ttl_sec": 30})
    assert WORKER_HEARTBEAT_KEY in redis.store


async def test_shutdown_removes_heartbeat() -> None:
    redis = RecordingRedis()
    await startup({"redis": redis, "heartbeat_ttl_sec": 30})
    await shutdown({"redis": redis})
    assert WORKER_HEARTBEAT_KEY not in redis.store


async def test_shutdown_is_safe_without_redis() -> None:
    await shutdown({})
