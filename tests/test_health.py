from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.core.db import session_dependency
from app.core.redis_client import redis_dependency
from app.main import create_app
from tests.conftest import FailingSession, FakeRedis, FakeSession, make_settings


def _client_with(session: object, redis: object) -> TestClient:
    application = create_app(make_settings())
    application.dependency_overrides[session_dependency] = lambda: session
    application.dependency_overrides[redis_dependency] = lambda: redis
    return TestClient(application)


def test_live_ok(client: TestClient) -> None:
    response = client.get("/live")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "alive"
    assert body["version"]


def test_live_ignores_broken_dependencies() -> None:
    """存活探针不能依赖外部资源，否则 db 抖动会引发容器反复重启。"""
    with _client_with(FailingSession(), FakeRedis(ping_ok=False)) as c:
        assert c.get("/live").status_code == 200
        assert c.get("/ready").status_code == 503


def test_ready_all_ok(client: TestClient) -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert (body["db"], body["redis"], body["worker"]) == ("ok", "ok", "ok")
    assert body["detail"] is None
    assert body["version"]


def test_ready_db_error_returns_503() -> None:
    with _client_with(FailingSession(), FakeRedis(heartbeat=str(time.time()))) as c:
        response = c.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["db"] == "error"
    assert body["detail"]["db"].startswith("db:")


def test_ready_worker_stale_heartbeat_keeps_serving() -> None:
    """worker 是独立进程：它掉线只降级，不应让 API 被判为不可用。"""
    stale = str(time.time() - 3600)
    with _client_with(FakeSession(), FakeRedis(heartbeat=stale)) as c:
        response = c.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["worker"] == "error"


def test_ready_worker_missing_heartbeat_keeps_serving() -> None:
    with _client_with(FakeSession(), FakeRedis(heartbeat=None)) as c:
        response = c.get("/ready")
    assert response.status_code == 200
    assert response.json()["worker"] == "error"


def test_ready_redis_down_returns_503() -> None:
    with _client_with(FakeSession(), FakeRedis(ping_ok=False)) as c:
        response = c.get("/ready")
    body = response.json()
    assert response.status_code == 503
    assert body["status"] == "degraded"
    assert body["redis"] == "error"
    assert body["worker"] == "error"


def test_trace_id_passthrough(client: TestClient) -> None:
    response = client.get("/ready", headers={"X-Request-ID": "abc-123"})
    assert response.headers["X-Request-ID"] == "abc-123"


def test_trace_id_generated_when_missing(client: TestClient) -> None:
    response = client.get("/ready")
    trace_id = response.headers["X-Request-ID"]
    assert len(trace_id) == 32


def test_404_uses_unified_error_shape(client: TestClient) -> None:
    response = client.get("/not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["trace_id"]
