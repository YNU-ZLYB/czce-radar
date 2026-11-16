"""仓单因子验证：回补郑商所仓单历史，再检验它对次日方向的预测力。

用法：
    python -m scripts.receipt_edge --years 4            # 回补 + 检验
    python -m scripts.receipt_edge --years 4 --no-backfill

为什么要先验证：仓单增加 = 可交割供给增加 = 理论利空，但这只是产业链常识，
不是统计结论。没有 edge 就进模型，等于拿真钱赌一个假设。

输出每品种：有效样本数、方向命中率（仓单增→次日跌）、IC（秩相关）。
只读 daily_bar，写入 industry_metric(kind=warehouse_receipt)。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

from app.core.config import load_settings
from app.core.db import create_engine, create_session_factory
from jobs.collectors.czce_receipt import fetch_day
from jobs.ingest.industry import upsert_receipts

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "backtest"
KIND = "warehouse_receipt"
LOOKBACK = 5
MIN_SAMPLES = 100
CONCURRENCY = 8


async def _safe_fetch(http: httpx.AsyncClient, day: dt.date) -> list[Any]:
    try:
        return await fetch_day(http, day)
    except Exception:
        return []


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return None if dx == 0 or dy == 0 else num / (dx * dy)


def evaluate(
    receipts: dict[dt.date, float],
    closes: dict[dt.date, float],
    dead_zone: float = 0.0,
) -> dict[str, Any]:
    """仓单 5 日变化率 vs 次日涨跌。方向按「仓单增 → 利空」计。"""
    days = sorted(receipts)
    xs: list[float] = []
    ys: list[float] = []
    hit = total = flat = 0
    for day in days:
        later = [d for d in closes if d > day]
        if not later:
            break
        nxt = later[0]
        base_day = _nth_before(days, day, LOOKBACK)
        if base_day is None:
            continue
        base = receipts.get(base_day, 0.0)
        curr = receipts[day]
        close_now, close_nxt = closes.get(day), closes.get(nxt)
        if base <= 0 or close_now is None or close_nxt is None or close_now <= 0:
            continue
        growth = curr / base - 1
        ret = (close_nxt / close_now - 1) * 100
        if abs(ret) < dead_zone:
            continue
        # 仓单未变化 = 无信号，既不算命中也不算失误
        if abs(growth) < 1e-12:
            flat += 1
            continue
        xs.append(-growth)  # 仓单增加 → 负向信号
        ys.append(ret)
        total += 1
        if (-growth) * ret > 0:
            hit += 1
    return {
        "n": total,
        "hit": (hit / total) if total else None,
        "ic": _spearman(xs, ys),
        "receipt_max": max(receipts.values()) if receipts else 0.0,
        "flat": flat,
    }


def _nth_before(days: list[dt.date], day: dt.date, back: int) -> dt.date | None:
    idx = days.index(day)
    return days[idx - back] if idx - back >= 0 else None


async def _backfill(
    http: httpx.AsyncClient,
    conn: Any,
    days: list[dt.date],
    have: set[dt.date],
) -> int:
    todo = [d for d in days if d not in have and d.weekday() < 5]
    done = 0
    # 并发下载、串行写库：郑商所单文件 200KB 左右，纯串行回补 4 年要十几分钟
    for start in range(0, len(todo), CONCURRENCY):
        chunk = todo[start : start + CONCURRENCY]
        results = await asyncio.gather(*(_safe_fetch(http, day) for day in chunk))
        for items in results:
            if not items:
                continue
            await upsert_receipts(conn, items)
            done += 1
        if done and done % 100 < CONCURRENCY:
            print(f"  ...已回补 {done} 个交易日（{chunk[-1]}）", flush=True)
    return done


async def run(years: int, backfill: bool, dead_zone: float) -> Path:
    settings = load_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    today = dt.date.today()
    since = today.replace(year=today.year - years)
    try:
        async with session_factory() as session:
            days = [
                r[0]
                for r in (
                    await session.execute(
                        text(
                            "SELECT DISTINCT trade_date FROM daily_bar"
                            " WHERE trade_date >= :a AND trade_date <= :b"
                            " ORDER BY trade_date"
                        ),
                        {"a": since, "b": today},
                    )
                ).all()
            ]
            have = {
                r[0]
                for r in (
                    await session.execute(
                        text(
                            "SELECT DISTINCT trade_date FROM industry_metric"
                            " WHERE kind = :k AND trade_date >= :a"
                        ),
                        {"k": KIND, "a": since},
                    )
                ).all()
            }
            if backfill:
                print(f"回补仓单：{len(days) - len(have)} 个交易日待补", flush=True)
                # 前面的 SELECT 已隐式开启事务，这里直接复用连接，结束时统一 commit
                conn = await session.connection()
                async with httpx.AsyncClient(timeout=30) as http:
                    done = await _backfill(http, conn, days, have)
                await session.commit()
                print(f"回补完成：写入 {done} 个交易日", flush=True)

            closes_raw = (
                await session.execute(
                    text(
                        "SELECT variety_code, trade_date, close FROM daily_bar"
                        " WHERE trade_date >= :a ORDER BY variety_code, trade_date"
                    ),
                    {"a": since - dt.timedelta(days=30)},
                )
            ).all()
            receipts_raw = (
                await session.execute(
                    text(
                        "SELECT variety_code, trade_date, value FROM industry_metric"
                        " WHERE kind = :k AND trade_date >= :a"
                    ),
                    {"k": KIND, "a": since - dt.timedelta(days=30)},
                )
            ).all()

        closes: dict[str, dict[dt.date, float]] = defaultdict(dict)
        for row in closes_raw:
            closes[row.variety_code][row.trade_date] = float(row.close)
        receipts: dict[str, dict[dt.date, float]] = defaultdict(dict)
        for row in receipts_raw:
            receipts[row.variety_code][row.trade_date] = float(row.value)

        results = {
            code: evaluate(receipts[code], closes[code], dead_zone) for code in sorted(receipts)
        }
        return _render(results, since, today, dead_zone)
    finally:
        await engine.dispose()


def _render(
    results: dict[str, dict[str, Any]], since: dt.date, until: dt.date, dead_zone: float
) -> Path:
    lines = [
        f"# 仓单因子验证 · {until.isoformat()}",
        "",
        f"- 区间：{since.isoformat()} ~ {until.isoformat()}",
        f"- 因子：仓单 {LOOKBACK} 日变化率；假设「仓单增 → 次日跌」",
        f"- 方向命中排除 |次日涨跌| < {dead_zone}% 的样本；IC 为秩相关",
        "",
        "| 品种 | 有效样本 | 持平天数 | 方向命中% | IC | 仓单峰值 | 判定 |",
        "|---|---|---|---|---|---|---|",
    ]
    print(f"{'品种':<6}{'样本':>8}{'持平':>7}{'命中%':>9}{'IC':>9}{'峰值':>10}  判定")
    for code, r in sorted(results.items(), key=lambda kv: -(kv[1]["ic"] or -9)):
        n, hit, ic = r["n"], r["hit"], r["ic"]
        flat = r["flat"]
        hit_s = "—" if hit is None else f"{hit * 100:.1f}"
        ic_s = "—" if ic is None else f"{ic:+.3f}"
        if n < MIN_SAMPLES:
            verdict = "样本不足"
        elif r["receipt_max"] <= 0:
            verdict = "仓单恒为 0，无信息"
        elif r["receipt_max"] < 100:
            verdict = "仓单量级过小，不可用"
        elif ic is not None and ic >= 0.03 and hit is not None and hit >= 0.53:
            verdict = "有弱 edge"
        elif ic is not None and ic <= -0.03:
            verdict = "方向相反，需复核"
        else:
            verdict = "无 edge"
        lines.append(
            f"| {code} | {n} | {flat} | {hit_s} | {ic_s} | {r['receipt_max']:.0f} | {verdict} |"
        )
        print(f"{code:<6}{n:>8}{flat:>7}{hit_s:>9}{ic_s:>9}{r['receipt_max']:>10.0f}  {verdict}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"receipt_edge_{until:%Y%m%d}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="仓单因子验证")
    parser.add_argument("--years", type=int, default=4)
    parser.add_argument("--no-backfill", action="store_true")
    parser.add_argument("--dead-zone", type=float, default=0.0)
    args = parser.parse_args()
    path = asyncio.run(run(args.years, not args.no_backfill, args.dead_zone))
    print(f"\nreport: {path}")


if __name__ == "__main__":
    main()
