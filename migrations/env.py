"""Alembic 环境：异步引擎 + 从环境变量读取连接串。"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import load_settings
from app.core.db import render_database_url
from app.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", render_database_url(load_settings().database_url))

target_metadata = Base.metadata


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    kwargs = {
        "target_metadata": target_metadata,
        "compare_type": True,
        "compare_server_default": True,
        "literal_binds": url is not None,
    }
    if connection is not None:
        context.configure(connection=connection, **kwargs)
    else:
        kwargs["dialect_opts"] = {"paramstyle": "named"}
        context.configure(url=url, **kwargs)


def run_migrations_offline() -> None:
    _configure(url=config.get_main_option("sqlalchemy.url"))
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
