from __future__ import annotations

import datetime as dt

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from jobs.collectors.base import RawItem
from jobs.ingest.quotes import ingest_quotes, normalize_quote, partition_dates

TS = dt.datetime(2026, 11, 9, 7, 0, tzinfo=dt.UTC)


def domestic(price: str = "5195.000", symbol: str = "SR0") -> RawItem:
    return RawItem(
        source="sina",
        external_id=symbol,
        ts=TS,
        payload={
            "market": "D",
            "variety_code": symbol[:-1],
            "name": "白糖连续",
            "price": price,
            "prev_close": "5209.000",
            "open": "5185.000",
            "high": "5207.000",
            "low": "5161.000",
            "volume": "366349",
            "oi": "648578.000",
        },
    )


def foreign(price: str = "89.797") -> RawItem:
    return RawItem(
        source="sina",
        external_id="OIL",
        ts=TS,
        payload={
            "market": "F",
            "variety_code": None,
            "price": price,
            "prev_close": "92.570",
            "open": "92.450",
            "high": "92.840",
            "low": "89.660",
            "volume": "74587",
            "oi": "0",
        },
    )


async def _count(conn: AsyncConnection) -> int:
    value = await conn.scalar(text("SELECT count(*) FROM quote_1m WHERE source = 'sina'"))
    return int(value or 0)


async def test_ingest_is_idempotent(db_session: AsyncConnection) -> None:
    items = [domestic(), foreign()]
    assert await ingest_quotes(db_session, items) == 2
    assert await _count(db_session) == 2
    # 同一批数据重复执行：行数不变（幂等）
    assert await ingest_quotes(db_session, items) == 0
    assert await _count(db_session) == 2


async def test_ingest_creates_partition(db_session: AsyncConnection) -> None:
    await ingest_quotes(db_session, [domestic()])
    name = await db_session.scalar(
        text("SELECT tablename FROM pg_tables WHERE tablename = 'quote_1m_20261109'")
    )
    assert name == "quote_1m_20261109"


async def test_ingest_drops_invalid_rows(db_session: AsyncConnection) -> None:
    bad_zero = domestic(price="0")
    bad_negative = domestic(price="-5")
    good = foreign()
    inserted = await ingest_quotes(db_session, [bad_zero, bad_negative, good])
    assert inserted == 1
    assert await _count(db_session) == 1


def test_normalize_rejects_bad_market_and_missing_ts() -> None:
    item = RawItem(
        source="sina",
        external_id="SR0",
        payload={"market": "X", "price": "1"},
    )
    assert normalize_quote(item) is None
    item2 = RawItem(source="sina", external_id="SR0", payload={"market": "D", "price": "1"})
    assert normalize_quote(item2) is None


def test_partition_dates_are_utc_dates() -> None:
    rows = [{"ts": dt.datetime(2026, 11, 9, 23, 0, tzinfo=dt.UTC)}]
    assert partition_dates(rows) == [dt.date(2026, 11, 9)]
