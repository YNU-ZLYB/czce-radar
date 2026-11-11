"""新浪实时行情采集器（国内 21 品种 + 外盘 10 代码，60s）。

接口实测（2026-11-10）：
- https://hq.sinajs.cn/list=nf_SR0,hf_OIL  → GBK 文本，每行一个 var
- nf_ 字段: [0]名称 [1]时间HHMMSS [2]开 [3]高 [4]低 [5]昨收 [6]买 [7]卖
  [8]最新 [9]结算 [10]昨结算 [13]持仓 [14]成交量 [17]日期
- hf_ 字段: [0]现价 [4]高 [5]低 [6]时间 [7]昨结算 [8]开 [9]持仓
  [12]日期 [13]名称 [14]成交量（时间为交易所当地时刻）
"""

from __future__ import annotations

import datetime as dt
import re
from typing import ClassVar

from czce_schema.symbols import (
    BEIJING,
    DOMESTIC_SINA_SYMBOLS,
    FOREIGN_SYMBOLS,
    SINA_FOREIGN_CODES,
)
from jobs.collectors.base import BaseCollector, RawItem

SINA_HOST = "https://hq.sinajs.cn"
REFERER = "https://finance.sina.com.cn"

_LINE_RE = re.compile(r'hq_str_(nf_|hf_)(\w+)="([^"]*)"')
_MAX_FUTURE_SKEW = dt.timedelta(minutes=5)


def _parse_beijing(date_str: str, hhmmss: str) -> dt.datetime:
    """国内交易所时间（北京）→ UTC。"""
    t = dt.datetime.strptime(hhmmss.zfill(6), "%H%M%S").time()
    local = dt.datetime.combine(dt.date.fromisoformat(date_str), t, tzinfo=BEIJING)
    return local.astimezone(dt.UTC)


def parse_sina_response(body: str, *, now: dt.datetime | None = None) -> list[RawItem]:
    """把新浪返回文本解析成 RawItem。未知符号、空行一律忽略。

    外盘闭市时段返回的是「最后成交时刻」，跨日后会显得来自未来；
    此时把 ts 回退为轮询时刻（闭市最后价持续有效，1m 粒度可接受）。
    """
    now = now or dt.datetime.now(dt.UTC)
    limit = now + _MAX_FUTURE_SKEW
    domestic_by_symbol = {sym: code for code, sym in DOMESTIC_SINA_SYMBOLS.items()}
    items: list[RawItem] = []
    for match in _LINE_RE.finditer(body):
        prefix, raw_symbol, quoted = match.groups()
        fields = quoted.split(",")
        if not any(fields):
            continue
        if prefix == "nf_":
            symbol = raw_symbol
            if symbol not in domestic_by_symbol:
                continue
            try:
                ts = _parse_beijing(fields[17], fields[1])
                payload = {
                    "name": fields[0],
                    "price": fields[8],
                    "prev_close": fields[10],  # 昨结算
                    "open": fields[2],
                    "high": fields[3],
                    "low": fields[4],
                    "volume": fields[14],
                    "oi": fields[13],
                }
            except (ValueError, IndexError):
                continue
            items.append(
                RawItem(
                    source="sina",
                    external_id=symbol,
                    ts=ts,
                    payload={"market": "D", "variety_code": domestic_by_symbol[symbol], **payload},
                )
            )
        else:
            ext_code = raw_symbol
            info = FOREIGN_SYMBOLS.get(ext_code)
            if info is None:
                continue
            try:
                local_tz = info.tz
                local = dt.datetime.strptime(
                    f"{fields[12]} {fields[6]}", "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=local_tz)
                payload = {
                    "name": fields[13],
                    "price": fields[0],
                    "prev_close": fields[7],  # 昨结算
                    "open": fields[8],
                    "high": fields[4],
                    "low": fields[5],
                    "volume": fields[14] if len(fields) > 14 else None,
                    "oi": fields[9],
                }
            except (ValueError, IndexError):
                continue
            items.append(
                RawItem(
                    source="sina",
                    external_id=ext_code,
                    ts=local.astimezone(dt.UTC) if local.astimezone(dt.UTC) <= limit else now,
                    payload={"market": "F", "variety_code": None, **payload},
                )
            )
    # 行情时间不得来自未来（容忍时钟偏差 5 分钟）；国内行情直接丢弃
    return [item for item in items if item.ts is None or item.ts <= limit]


class SinaQuoteCollector(BaseCollector):
    code: ClassVar[str] = "sina_quotes"
    name: ClassVar[str] = "新浪实时行情（国内主力+外盘）"
    kind: ClassVar[str] = "quote"
    interval_sec: ClassVar[int] = 60

    async def fetch(self) -> list[RawItem]:
        symbols = [
            *(f"nf_{sym}" for sym in DOMESTIC_SINA_SYMBOLS.values()),
            *(FOREIGN_SYMBOLS[code].sina_symbol for code in SINA_FOREIGN_CODES),
        ]
        response = await self._http.get(
            f"{SINA_HOST}/list={','.join(symbols)}",
            headers={"Referer": REFERER},
        )
        response.raise_for_status()
        body = response.content.decode("gbk", errors="replace")
        return parse_sina_response(body)
