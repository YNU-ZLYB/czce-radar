"""迁移可离线生成 SQL：CI 无需真实数据库即可验证迁移链。"""

from __future__ import annotations

import io
import os
from importlib import import_module
from pathlib import Path

from alembic import command
from alembic.config import Config

from czce_schema.varieties import VARIETY_SEED

ROOT = Path(__file__).resolve().parents[1]


def test_revision_chain_is_linear() -> None:
    first = import_module("migrations.versions.0001_initial")
    second = import_module("migrations.versions.0002_seed_varieties")
    latest = import_module("migrations.versions.0006_prediction")
    previous = import_module("migrations.versions.0005_industry_metric_kind_widen")
    assert first.revision == "0001_initial"
    assert first.down_revision is None
    assert second.revision == "0002_seed_varieties"
    assert second.down_revision == first.revision
    assert latest.revision == "0006_prediction"
    assert latest.down_revision == previous.revision


def test_seed_migration_is_frozen() -> None:
    """迁移内的种子必须与运行时种子一致；变更品种清单须新增迁移。"""
    second = import_module("migrations.versions.0002_seed_varieties")
    frozen = [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in second.FROZEN_VARIETY_SEED]
    live = [(v.code, v.name, v.sector, v.is_tradable, v.sort_order, v.note) for v in VARIETY_SEED]
    assert frozen == live


def test_offline_upgrade_produces_sql_with_seed() -> None:
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    buffer = io.StringIO()
    cfg.output_buffer = buffer  # type: ignore[attr-defined]
    command.upgrade(cfg, "head", sql=True)
    sql = buffer.getvalue()
    assert "CREATE TABLE variety" in sql
    assert "INSERT INTO variety" in sql
    assert "'MA'" in sql and "'ZC'" in sql
    # M3 预测表
    assert "CREATE TABLE prediction" in sql
    assert "CREATE TABLE outcome" in sql
    assert "CREATE TABLE backtest_run" in sql
    assert "uq_prediction_variety_target_horizon_model" in sql
