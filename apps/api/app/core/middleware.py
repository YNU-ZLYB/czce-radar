"""请求级 trace_id 与访问日志。"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger

logger = get_logger("app.request")

TRACE_ID_HEADER = "X-Request-ID"
_SKIP_LOG_PATHS = {"/live", "/ready", "/openapi.json", "/docs", "/redoc"}


class TraceIdMiddleware(BaseHTTPMiddleware):
    """为每个请求生成/透传 trace_id，并记录耗时。"""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(TRACE_ID_HEADER, "")
        trace_id = incoming.strip()[:64] or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            trace_id=trace_id,
            method=request.method,
            path=request.url.path,
        )
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("request_failed", duration_ms=_ms_since(started))
            raise
        response.headers[TRACE_ID_HEADER] = trace_id
        if request.url.path not in _SKIP_LOG_PATHS:
            logger.info(
                "request_done",
                status_code=response.status_code,
                duration_ms=_ms_since(started),
            )
        return response


def _ms_since(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
