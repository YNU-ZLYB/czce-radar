from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_missing_database_url_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)
    assert "database_url" in str(exc_info.value)


def test_rejects_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            database_url="sqlite+aiosqlite:///tmp.db",
            redis_url="redis://localhost:6379/0",
        )
    assert "DATABASE_URL" in str(exc_info.value)


def test_rejects_non_redis_url() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://u:p@localhost:5432/db",
            redis_url="http://localhost:6379",
        )


def test_defaults_are_sane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",
        redis_url="redis://localhost:6379/0",
    )
    assert settings.worker_heartbeat_ttl_sec == 90
    assert settings.app_env == "local"
    assert settings.version
