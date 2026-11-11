"""seed varieties (21 tradable + 2 delisted)

Revision ID: 0002_seed_varieties
Revises: 0001_initial
Create Date: 2026-11-09

种子数据在此内联冻结：历史迁移必须可复现，禁止引用可能演变的运行时代码。
`tests/test_migrations.py::test_seed_migration_is_frozen` 会校验本列表与
`czce_schema.varieties.VARIETY_SEED` 一致；若品种清单变更，应新增迁移而非改这里。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_seed_varieties"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FROZEN_VARIETY_SEED: list[tuple[str, str, str, bool, int, str | None]] = [
    # (code, name, sector, is_tradable, sort_order, note)
    ("MA", "甲醇", "能源化工", True, 10, None),
    ("TA", "PTA", "能源化工", True, 20, None),
    ("PX", "对二甲苯", "能源化工", True, 30, None),
    ("PR", "瓶片", "能源化工", True, 40, None),
    ("PF", "短纤", "能源化工", True, 50, None),
    ("SH", "烧碱", "能源化工", True, 60, None),
    ("UR", "尿素", "能源化工", True, 70, None),
    ("SA", "纯碱", "能源化工", True, 80, None),
    ("FG", "玻璃", "能源化工", True, 90, None),
    ("PL", "丙烯", "能源化工", True, 100, None),
    ("OI", "菜油", "油脂油料", True, 110, None),
    ("RM", "菜粕", "油脂油料", True, 120, None),
    ("RS", "菜籽", "油脂油料", True, 130, None),
    ("PK", "花生", "油脂油料", True, 140, None),
    ("SR", "白糖", "软商品", True, 150, None),
    ("CF", "棉花", "软商品", True, 160, None),
    ("CY", "棉纱", "软商品", True, 170, None),
    ("SF", "硅铁", "黑色", True, 180, None),
    ("SM", "锰硅", "黑色", True, 190, None),
    ("AP", "苹果", "农副", True, 200, None),
    ("CJ", "红枣", "农副", True, 210, None),
    ("ZC", "动力煤", "已下市", False, 900, "已停止交易，仅保留历史主数据"),
    ("WH", "小麦", "已下市", False, 910, "已停止交易，仅保留历史主数据"),
]

_variety = sa.table(
    "variety",
    sa.column("code", sa.String),
    sa.column("name", sa.String),
    sa.column("sector", sa.String),
    sa.column("is_tradable", sa.Boolean),
    sa.column("sort_order", sa.Integer),
    sa.column("listed_at", sa.Date),
    sa.column("delisted_at", sa.Date),
    sa.column("note", sa.Text),
)


def upgrade() -> None:
    op.bulk_insert(
        _variety,
        [
            {
                "code": code,
                "name": name,
                "sector": sector,
                "is_tradable": is_tradable,
                "sort_order": sort_order,
                "listed_at": None,
                "delisted_at": None,
                "note": note,
            }
            for code, name, sector, is_tradable, sort_order, note in FROZEN_VARIETY_SEED
        ],
    )


def downgrade() -> None:
    codes = [row[0] for row in FROZEN_VARIETY_SEED]
    op.execute(_variety.delete().where(_variety.c.code.in_(codes)))
