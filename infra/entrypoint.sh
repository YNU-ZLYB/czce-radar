#!/bin/sh
# 容器启动入口：先确保数据库结构与代码版本一致，再执行真正的进程。
# 迁移只在 API 容器执行（RUN_MIGRATIONS=1），worker 容器设为 0，避免并发 DDL。
set -eu

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  echo "[entrypoint] alembic upgrade head"
  alembic upgrade head
else
  # worker：等 API 完成迁移，避免定时任务在表结构就绪前启动（Render 无启动顺序保证）
  echo "[entrypoint] waiting for migrations"
  python - <<'PY'
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.db import normalise_database_url


async def wait_for_migrations() -> None:
    # 与 API 使用同一条 URL 归一路径（sslmode→ssl 等），避免直连 DSN 解析差异
    engine = create_async_engine(normalise_database_url(os.environ["DATABASE_URL"]))
    try:
        for _ in range(60):
            try:
                async with engine.connect() as conn:
                    ready = await conn.scalar(
                        text("SELECT 1 FROM alembic_version LIMIT 1")
                    )
                if ready is not None:
                    print("[entrypoint] migrations ready")
                    return
            except Exception:
                pass
            await asyncio.sleep(2)
        sys.exit("[entrypoint] migrations not ready after 120s")
    finally:
        await engine.dispose()


asyncio.run(wait_for_migrations())
PY
fi

exec "$@"
