"""新浪期货历史日线采集器（国内品种，一次返回全部历史）。

接口实测（2026-11-10）：
https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_t=/InnerFuturesNewService.getDailyKLine?symbol=SR0
返回 `/*<script>...*/ var _t=([{d,o,h,l,c,v,p,s},...])`：
d=日期 o/h/l/c=OHLC v=成交量 p=持仓 s=结算价
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import ClassVar

import httpx

from jobs.collectors.base import BaseCollector, RawItem

_DAILY_HOST = "https://stock2.finance.sina.com.cn"
_JSON_RE = re.compile(r"\((\[.*\])\)", re.DOTALL)


def parse_daily_kline(body: str) -> list[RawItem]:
    match = _JSON_RE.search(body)
    if match is None:
        raise ValueError("日线响应中未找到 JSON 数组")
    records = json.loads(match.group(1))
    items: list[RawItem] = []
    for row in records:
        try:
            dt.date.fromisoformat(str(row["d"]))  # 日期不合法直接跳过
            payload = {
                "open": row["o"],
                "high": row["h"],
                "low": row["l"],
                "close": row["c"],
                "settle": row.get("s") or None,
                "volume": row.get("v") or None,
                "oi": row.get("p") or None,
            }
            items.append(
                RawItem(
                    source="sina",
                    external_id=row["d"],
                    payload=payload,
                )
            )
        except (KeyError, ValueError):
            continue
    return items


class SinaDailyKlineCollector(BaseCollector):
    code: ClassVar[str] = "sina_daily"
    name: ClassVar[str] = "新浪期货历史日线"
    kind: ClassVar[str] = "quote"
    interval_sec: ClassVar[int] = 86400

    def __init__(self, http: httpx.AsyncClient, symbol: str) -> None:
        super().__init__(http)
        self.symbol = symbol

    async def fetch(self) -> list[RawItem]:
        url = (
            f"{_DAILY_HOST}/futures/api/jsonp.php/var%20_t=/"
            f"InnerFuturesNewService.getDailyKLine?symbol={self.symbol}"
        )
        response = await self._http.get(url, headers={"Referer": "https://finance.sina.com.cn"})
        response.raise_for_status()
        return parse_daily_kline(response.text)


def today_cn() -> dt.date:
    return dt.datetime.now(dt.UTC).astimezone(dt.timezone(dt.timedelta(hours=8))).date()
