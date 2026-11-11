"""FRED 广义美元指数（DTWEXBGS）日线采集。

背景：新浪 ``hf_DX`` 自 2026 年起返回空，美元指数改由圣路易斯联储 FRED 提供。
FRED CSV 端点无需 API Key，日频、按美国工作日发布（发布滞后约 1 周）。
数据写入 ``foreign_daily(ext_code='DX')``，与新浪外盘日线同表，特征层无感知。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import TYPE_CHECKING, ClassVar

from jobs.collectors.base import BaseCollector, RawItem

if TYPE_CHECKING:
    import httpx

FRED_HOST = "https://fred.stlouisfed.org/graph/fredgraph.csv"
SERIES_ID = "DTWEXBGS"
EXT_CODE = "DX"
DEFAULT_START = dt.date(2022, 1, 1)


def parse_fred_csv(body: str, start: dt.date = DEFAULT_START) -> list[RawItem]:
    """解析 FRED CSV（observation_date,SERIES）。缺失值 ``.`` 跳过。"""
    reader = csv.reader(io.StringIO(body))
    header = next(reader, None)
    if header is None or len(header) < 2:
        return []

    items: list[RawItem] = []
    for row in reader:
        if len(row) < 2:
            continue
        try:
            day = dt.date.fromisoformat(row[0].strip())
            value = float(row[1])
        except ValueError:
            continue  # 含 "." 缺失值
        if day < start or value <= 0:
            continue
        items.append(
            RawItem(
                source="fred_dx",
                external_id=day.isoformat(),
                ts=None,
                payload={"ext_code": EXT_CODE, "trade_date": day, "close": value},
            )
        )
    items.sort(key=lambda i: i.external_id or "")
    return items


class FredDollarIndexCollector(BaseCollector):
    code: ClassVar[str] = "fred_dx"
    name: ClassVar[str] = "FRED 美元指数（DTWEXBGS）"
    kind: ClassVar[str] = "foreign_daily"
    interval_sec: ClassVar[int] = 86400

    def __init__(self, http: httpx.AsyncClient) -> None:
        super().__init__(http)
        self._start = DEFAULT_START

    async def fetch(self) -> list[RawItem]:
        response = await self._http.get(
            FRED_HOST, params={"id": SERIES_ID, "cosd": self._start.isoformat()}
        )
        response.raise_for_status()
        items = parse_fred_csv(response.text, self._start)
        if not items:
            msg = "FRED 返回空序列"
            raise ValueError(msg)
        return items
