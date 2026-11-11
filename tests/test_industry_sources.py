"""产业数据源测试：FRED 美元指数、郑商所仓单解析与幂等入库。"""

from __future__ import annotations

import datetime as dt
import io

import httpx
import pytest
from openpyxl import Workbook
from sqlalchemy import text

from czce_schema.symbols import BEIJING
from jobs.collectors.czce_receipt import (
    CzceReceiptCollector,
    parse_receipt_xlsx,
    receipt_url,
)
from jobs.collectors.fred import parse_fred_csv
from jobs.ingest.foreign import upsert_fred_series
from jobs.ingest.industry import upsert_receipts

FRED_BODY = """observation_date,DTWEXBGS
2026-11-02,99.5
2026-11-03,100.5
2026-11-04,.
2026-11-05,101.0
"""


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["品种：白糖SR     单位：张"])
    ws.append(
        [
            "仓库编号",
            "仓库简称",
            "年度",
            "等级",
            "品牌",
            "仓单数量(完税)",
            "仓单数量(保税)",
            "当日增减",
        ]
    )
    ws.append(["0101", "中储A", "", "", "", 100, 20, 5])
    ws.append(["总计", "", "", "", "", 100, 20, 5])
    # 有效预报表：不是仓单，必须跳过
    ws.append(["品种：花生PK     单位：张"])
    ws.append(["机构编号", "机构简称", "预报数量", "当日增减", "升贴水"])
    ws.append(["0103", "机构B", 10, 0, 0])
    ws.append(["总计", "", 10, 0, ""])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_fred_csv_skips_missing_and_old() -> None:
    items = parse_fred_csv(FRED_BODY, start=dt.date(2026, 11, 3))
    assert [i.external_id for i in items] == ["2026-11-03", "2026-11-05"]
    assert items[0].payload["close"] == 100.5
    assert items[0].payload["ext_code"] == "DX"
    assert items[0].source == "fred_dx"


def test_parse_fred_csv_empty_body() -> None:
    assert parse_fred_csv("") == []
    assert parse_fred_csv("observation_date,DTWEXBGS\n") == []


def test_parse_receipt_xlsx_sums_tax_free_and_bonded() -> None:
    items = parse_receipt_xlsx(_xlsx_bytes(), dt.date(2026, 11, 6))
    assert len(items) == 1  # PK 只有预报表，被跳过
    item = items[0]
    assert item.payload["code"] == "SR"
    assert item.payload["receipt"] == 120.0
    assert item.payload["delta"] == 5.0
    assert item.external_id == "2026-11-06"


def test_receipt_url_format() -> None:
    assert receipt_url(dt.date(2026, 11, 6)).endswith("/2026/20261106/FutureDataWhsheet.xlsx")


async def test_receipt_collector_falls_back_to_previous_day() -> None:
    payload = _xlsx_bytes()
    today = dt.datetime.now(dt.UTC).astimezone(BEIJING).date()

    def handler(request: httpx.Request) -> httpx.Response:
        if today.strftime("%Y%m%d") in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = CzceReceiptCollector(client)
        items = await collector.fetch()
    assert collector.day == today - dt.timedelta(days=1)
    assert [i.payload["code"] for i in items] == ["SR"]


async def test_upsert_fred_recomputes_change_pct(db_session) -> None:  # type: ignore[no-untyped-def]
    await db_session.execute(text("DELETE FROM foreign_daily WHERE ext_code = 'DX'"))
    first = parse_fred_csv(FRED_BODY, start=dt.date(2026, 11, 2))
    assert await upsert_fred_series(db_session, first) == 3
    # 再补一天：整条序列重算，首日 null，其余相对前一交易日
    more = parse_fred_csv(
        "observation_date,DTWEXBGS\n2026-11-02,99.5\n2026-11-03,100.5\n2026-11-05,101.0\n2026-11-06,102.0\n",
        start=dt.date(2026, 11, 2),
    )
    assert await upsert_fred_series(db_session, more) == 4
    rows = (
        await db_session.execute(
            text(
                "SELECT trade_date, change_pct FROM foreign_daily"
                " WHERE ext_code = 'DX' ORDER BY trade_date"
            )
        )
    ).all()
    assert len(rows) == 4
    assert rows[0].change_pct is None
    assert float(rows[1].change_pct) == pytest.approx((100.5 / 99.5 - 1) * 100, abs=0.001)
    assert float(rows[3].change_pct) == pytest.approx((102 / 101 - 1) * 100, abs=0.001)
    await db_session.rollback()


async def test_upsert_receipts_idempotent(db_session) -> None:  # type: ignore[no-untyped-def]
    items = parse_receipt_xlsx(_xlsx_bytes(), dt.date(2026, 11, 6))
    assert await upsert_receipts(db_session, items) == 1
    assert await upsert_receipts(db_session, items) == 1
    rows = (
        await db_session.execute(
            text(
                "SELECT variety_code, trade_date, value, unit FROM industry_metric"
                " WHERE kind = 'warehouse_receipt'"
            )
        )
    ).all()
    assert len(rows) == 1  # 同键只覆盖不新增
    assert rows[0].variety_code == "SR"
    assert float(rows[0].value) == 120.0
    assert rows[0].unit == "张"
    await db_session.rollback()
