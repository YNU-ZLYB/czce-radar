from __future__ import annotations

from app.core.db import normalise_database_url, render_database_url


def test_plain_postgresql_gets_asyncpg_driver() -> None:
    url = normalise_database_url("postgresql://u:p@localhost:5432/db")
    assert url.drivername == "postgresql+asyncpg"
    assert url.database == "db"


def test_asyncpg_driver_is_kept() -> None:
    url = normalise_database_url("postgresql+asyncpg://u:p@localhost:5432/db")
    assert url.drivername == "postgresql+asyncpg"


def test_sslmode_is_translated_for_supabase() -> None:
    raw = (
        "postgresql://postgres.abc:pwd@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres"
        "?sslmode=require"
    )
    url = normalise_database_url(raw)
    assert url.query["ssl"] == "require"
    assert "sslmode" not in url.query


def test_sslmode_verify_full_is_mapped() -> None:
    url = normalise_database_url("postgresql://u:p@h:5432/d?sslmode=verify-full")
    assert url.query["ssl"] == "verify-full"


def test_existing_ssl_param_wins_over_sslmode() -> None:
    url = normalise_database_url("postgresql://u:p@h:5432/d?sslmode=require&ssl=prefer")
    assert url.query["ssl"] == "prefer"
    assert "sslmode" not in url.query


def test_other_query_params_are_preserved() -> None:
    url = normalise_database_url("postgresql://u:p@h:5432/d?application_name=czce")
    assert url.query["application_name"] == "czce"


def test_password_with_special_chars_survives() -> None:
    url = normalise_database_url("postgresql://u:p%40ss%23word@h:5432/d")
    assert url.password == "p@ss#word"


def test_render_escapes_percent_for_alembic_ini() -> None:
    rendered = render_database_url("postgresql://u:p%40ss@h:5432/d?sslmode=require")
    assert "%%" in rendered
    assert "sslmode" not in rendered
