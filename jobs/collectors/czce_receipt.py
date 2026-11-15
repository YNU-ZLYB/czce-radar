"""郑商所仓单日报采集（21 个品种一次拉全）。

数据源：``https://www.czce.com.cn/cn/DFSStaticFiles/Future/YYYY/YYYYMMDD/FutureDataWhsheet.xlsx``
（2026 年起为 ``.xlsx``；2025 及以前为 ``.xls``，仅回补用）。
官网正文页有瑞数反爬，但该静态文件直链可下载，无需 JS。

表结构：每个品种若干张表（仓库仓单 / 厂库仓单 / 有效预报），
每张表表头含「仓单数量(完税)」「仓单数量(保税)」「当日增减」，
取「总计」行按表头列位求和；「预报数量」表不属仓单，自动跳过。
"""

from __future__ import annotations

import datetime as dt
import io
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, ClassVar

import xlrd
from openpyxl import load_workbook

from czce_schema.symbols import BEIJING
from czce_schema.varieties import VARIETY_SEED
from jobs.collectors.base import BaseCollector, RawItem

if TYPE_CHECKING:
    import httpx

CXCE_TEMPLATE = (
    "https://www.czce.com.cn/cn/DFSStaticFiles/Future/{year}/{ymd}/FutureDataWhsheet.{ext}"
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
MAX_LOOKBACK_DAYS = 6

#: 2026 起官网只给 .xlsx；更早的日期只有 .xls（BIFF8）
EXTS: tuple[str, ...] = ("xlsx", "xls")
XLSX_MAGIC = b"PK\x03\x04"
XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

NAME_TO_CODE: dict[str, str] = {seed.name: seed.code for seed in VARIETY_SEED}
#: 表头形如「品种：白糖SR     单位：张」/「品种：PTA     单位：张」，按名称前缀匹配
_NAME_BY_LENGTH = sorted(NAME_TO_CODE.items(), key=lambda kv: -len(kv[0]))


def _match_code(header: str) -> str | None:
    body = header.split("品种：", 1)[-1].split("单位", 1)[0].strip()
    for name, code in _NAME_BY_LENGTH:
        if body.startswith(name):
            return code
    return None


def receipt_url(day: dt.date, ext: str = "xlsx") -> str:
    return CXCE_TEMPLATE.format(year=day.year, ymd=day.strftime("%Y%m%d"), ext=ext)


def _as_float(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def _iter_xlsx_rows(data: bytes) -> Iterator[tuple[object, ...]]:
    workbook = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    try:
        sheet = workbook.active
        if sheet is None:
            return
        yield from sheet.iter_rows(values_only=True)
    finally:
        workbook.close()


def _iter_xls_rows(data: bytes) -> Iterator[list[object]]:
    book = xlrd.open_workbook(file_contents=data)
    sheet = book.sheet_by_index(0)
    for index in range(sheet.nrows):
        yield sheet.row_values(index)


def _parse_rows(rows: Iterable[Iterable[object]], day: dt.date) -> list[RawItem]:
    totals: dict[str, list[float]] = {}
    code: str | None = None
    receipt_cols: list[int] = []
    delta_col: int | None = None

    for row in rows:
        cells = [str(cell).strip() if cell is not None else "" for cell in row]
        head = cells[0]
        if head.startswith("品种："):
            code = _match_code(head)
            receipt_cols, delta_col = [], None
            continue
        if any(cell.startswith("仓单数量") for cell in cells):
            receipt_cols = [i for i, cell in enumerate(cells) if cell.startswith("仓单数量")]
            delta_col = next(
                (i for i, cell in enumerate(cells) if cell.startswith("当日增减")),
                None,
            )
            continue
        if head in {"总计", "合计"} and code and receipt_cols:
            acc = totals.setdefault(code, [0.0, 0.0])
            acc[0] += sum(_as_float(row[i]) for i in receipt_cols if i < len(row))
            if delta_col is not None and delta_col < len(row):
                acc[1] += _as_float(row[delta_col])

    return [
        RawItem(
            source="czce_receipt",
            external_id=day.isoformat(),
            ts=None,
            payload={
                "code": variety_code,
                "receipt": receipt,
                "delta": delta,
                "trade_date": day,
            },
        )
        for variety_code, (receipt, delta) in sorted(totals.items())
    ]


def parse_receipt(data: bytes, day: dt.date) -> list[RawItem]:
    """按文件头自动分派，兼容 2026 起的 xlsx 与更早的 xls。"""
    if data.startswith(XLSX_MAGIC):
        return _parse_rows(_iter_xlsx_rows(data), day)
    if data.startswith(XLS_MAGIC):
        return _parse_rows(_iter_xls_rows(data), day)
    return []


def parse_receipt_xlsx(data: bytes, day: dt.date) -> list[RawItem]:
    """解析仓单日报 XLSX，返回每品种一条（value=仓单数量合计）。"""
    return _parse_rows(_iter_xlsx_rows(data), day)


async def fetch_day(http: httpx.AsyncClient, day: dt.date) -> list[RawItem]:
    """拉取单日仓单，xlsx 缺失时回退 xls；两天都拿不到就返回空。"""
    for ext in EXTS:
        response = await http.get(receipt_url(day, ext), headers={"User-Agent": UA})
        if response.status_code != 200 or len(response.content) < 1024:
            continue
        try:
            items = parse_receipt(response.content, day)
        except Exception:
            continue
        if items:
            return items
    return []


class CzceReceiptCollector(BaseCollector):
    code: ClassVar[str] = "czce_receipt"
    name: ClassVar[str] = "郑商所仓单日报"
    kind: ClassVar[str] = "industry_metric"
    interval_sec: ClassVar[int] = 86400

    def __init__(self, http: httpx.AsyncClient) -> None:
        super().__init__(http)
        self.day: dt.date | None = None

    async def fetch(self) -> list[RawItem]:
        today = dt.datetime.now(dt.UTC).astimezone(BEIJING).date()
        for offset in range(MAX_LOOKBACK_DAYS):
            day = today - dt.timedelta(days=offset)
            items = await fetch_day(self._http, day)
            if items:
                self.day = day
                return items
        msg = f"近 {MAX_LOOKBACK_DAYS} 天无仓单文件（xlsx/xls 均不可用）"
        raise ValueError(msg)
