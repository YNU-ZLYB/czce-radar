"""外盘历史日线回补：新浪全球期货日线 → foreign_daily（传导回测的输入）。

用法：
    python -m scripts.backfill_foreign            # 9 个新浪外盘 + FRED 美元指数
    python -m scripts.backfill_foreign OIL CT     # 只回补指定外盘代码

说明：新浪 ``GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=OIL``
（不带 ``hf_`` 前缀）一次返回该品种全部历史；写入后按整条序列重算 change_pct，
与 ``upsert_fred_series`` 共用同一套幂等逻辑。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import sys

import httpx

from app.core.config import load_settings
from app.core.db import create_engine, create_session_factory
from czce_schema.symbols import SINA_FOREIGN_CODES
from jobs.collectors.base import RawItem
from jobs.collectors.fred import FredDollarIndexCollector
from jobs.ingest.foreign import upsert_fred_series

_HOST = "https://stock2.finance.sina.com.cn"
_PATH = "/futures/api/jsonp.php/var%20_t=/GlobalFuturesService.getGlobalFuturesDailyKLine"
_JSON_RE = re.compile(r"\((\[.*\])\)", re.DOTALL)


def parse_global_daily(body: str, ext_code: str) -> list[RawItem]:
    match = _JSON_RE.search(body)
    if match is None:
        return []
    records = json.loads(match.group(1))
    items: list[RawItem] = []
    for row in records:
        try:
            day = dt.date.fromisoformat(str(row["date"]))
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if close <= 0:
            continue
        items.append(
            RawItem(
                source="sina_global",
                external_id=day.isoformat(),
                ts=None,
                payload={"ext_code": ext_code, "trade_date": day, "close": close},
            )
        )
    items.sort(key=lambda i: i.external_id or "")
    return items


async def backfill(codes: list[str], with_fred: bool = True) -> int:
    settings = load_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    errors = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as http:
            for ext in codes:
                try:
                    response = await http.get(
                        f"{_HOST}{_PATH}",
                        params={"symbol": ext},
                        headers={"Referer": "https://finance.sina.com.cn"},
                    )
                    response.raise_for_status()
                    items = parse_global_daily(response.text, ext)
                    if not items:
                        errors += 1
                        print(f"[empty] {ext}")
                        continue
                    async with session_factory() as session, session.begin():
                        await upsert_fred_series(session, items)
                    first = items[0].external_id
                    last = items[-1].external_id
                    print(f"[ok] {ext}: {len(items)} 天（{first} ~ {last}）")
                except Exception as exc:
                    errors += 1
                    print(f"[error] {ext}: {type(exc).__name__}: {exc}")

            if with_fred:
                try:
                    collector = FredDollarIndexCollector(http)
                    items = await collector.fetch()
                    async with session_factory() as session, session.begin():
                        await upsert_fred_series(session, items)
                    print(f"[ok] DX(FRED): {len(items)} 天")
                except Exception as exc:
                    errors += 1
                    print(f"[error] DX: {type(exc).__name__}: {exc}")
    finally:
        await engine.dispose()
    return 1 if errors else 0


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    codes = args or list(SINA_FOREIGN_CODES)
    raise SystemExit(asyncio.run(backfill(codes)))


if __name__ == "__main__":
    main()
