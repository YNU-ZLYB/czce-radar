FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml alembic.ini ./
COPY apps/api ./apps/api
COPY jobs ./jobs
COPY packages ./packages
COPY migrations ./migrations
COPY infra/entrypoint.sh /usr/local/bin/czce-entrypoint

ARG PIP_INDEX_URL=""
RUN pip install ${PIP_INDEX_URL:+--index-url ${PIP_INDEX_URL}} . \
    && chmod +x /usr/local/bin/czce-entrypoint

RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 存活探针不依赖 db/redis，避免外部依赖抖动导致容器被反复重启
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/live || exit 1

ENTRYPOINT ["/usr/local/bin/czce-entrypoint"]

# 默认启动 API；worker 在 compose / Render 中覆盖 command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
