"""历史日线回补脚本：21 个品种 × 全部可得历史（新浪一次返回自上市以来的日线）。

用法：
    python -m scripts.backfill_daily            # 回补全部品种
    python -m scripts.backfill_daily SR MA      # 只回补指定品种
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys

import httpx

from app.core.config import load_settings
from app.core.db import create_engine, create_session_factory
from czce_schema.symbols import DOMESTIC_SINA_SYMBOLS
from jobs.collectors.sina_daily import SinaDailyKlineCollector
from jobs.ingest.daily import normalize_daily_bar, upsert_daily_bars


async def backfill(codes: list[str]) -> int:
    settings = load_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    total = 0
    errors = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
            for code in codes:
                symbol = DOMESTIC_SINA_SYMBOLS.get(code)
                if symbol is None:
                    print(f"[skip] {code}: 未知品种代码")
                    continue
                collector = SinaDailyKlineCollector(http, symbol)
                try:
                    items = await collector.fetch()
                except Exception as exc:
                    errors += 1
                    print(f"[error] {code}: {type(exc).__name__}: {exc}")
                    continue
                rows = [r for r in (normalize_daily_bar(code, item) for item in items) if r]
                if not rows:
                    errors += 1
                    print(f"[empty] {code}")
                    continue
                async with session_factory() as session, session.begin():
                    inserted = await upsert_daily_bars(session, rows)
                total += len(rows)
                first = rows[0]["trade_date"].isoformat()
                last = rows[-1]["trade_date"].isoformat()
                print(f"[ok] {code}: {len(rows)} 天（{first} ~ {last}），upsert {inserted}")
    finally:
        await engine.dispose()
    if errors:
        print(f"完成，但有 {errors} 个品种失败")
    return 1 if errors else 0


def main() -> int:
    codes = sys.argv[1:] or list(DOMESTIC_SINA_SYMBOLS)
    started = dt.datetime.now(dt.UTC)
    print(f"回补品种：{','.join(codes)}")
    exit_code = asyncio.run(backfill(codes))
    print(f"耗时 {(dt.datetime.now(dt.UTC) - started).total_seconds():.1f}s")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
