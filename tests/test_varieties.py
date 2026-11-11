from __future__ import annotations

from collections import Counter

from app.models import Base, Variety
from czce_schema.constants import DELISTED_SECTOR, SECTOR_ORDER
from czce_schema.varieties import TRADABLE_CODES, VARIETY_SEED


def test_variety_count_and_uniqueness() -> None:
    codes = [item.code for item in VARIETY_SEED]
    assert len(codes) == len(set(codes)) == 23
    assert len(TRADABLE_CODES) == 21
    assert {v.code for v in VARIETY_SEED if not v.is_tradable} == {"ZC", "WH"}


def test_sector_distribution() -> None:
    counter = Counter(item.sector for item in VARIETY_SEED if item.is_tradable)
    assert counter == {
        "能源化工": 10,
        "油脂油料": 4,
        "软商品": 3,
        "黑色": 2,
        "农副": 2,
    }
    assert set(counter) == set(SECTOR_ORDER)
    assert all(v.sector == DELISTED_SECTOR for v in VARIETY_SEED if not v.is_tradable)


def test_sort_order_is_unique_and_increasing_by_sector() -> None:
    orders = [item.sort_order for item in VARIETY_SEED]
    assert len(orders) == len(set(orders))
    assert orders == sorted(orders)


def test_seed_values_fit_column_lengths() -> None:
    for item in VARIETY_SEED:
        assert len(item.code) <= 8
        assert len(item.name) <= 32
        assert len(item.sector) <= 24


def test_orm_table_matches_contract() -> None:
    assert Variety.__tablename__ == "variety"
    table = Base.metadata.tables["variety"]
    assert set(table.columns.keys()) == {
        "code",
        "name",
        "sector",
        "is_tradable",
        "sort_order",
        "listed_at",
        "delisted_at",
        "note",
        "linkage_q",
    }
    assert table.primary_key.columns.keys() == ["code"]
    assert not table.c.code.nullable
    assert not table.c.is_tradable.nullable
    assert table.c.code.type.length == 8
