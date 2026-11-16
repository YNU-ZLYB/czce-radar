"""动量/反转因子验证：价格自身趋势对次日方向有没有预测力。

动机：现有信号只有「外盘传导 + 季节性 + 资讯情绪」三项，价格自身的历史
完全没进模型（只用来算波动率阈值）。而日频最稳的信号之一恰恰是动量/反转。

做法：对每个品种、每个回看窗口 k，计算动量 mom = close/close[k] - 1，
与次日收益做秩相关 IC 和方向命中，并把样本对半切开看 IC 是否前后一致。
前后半段同号且都超过噪声阈值，才算稳定信号。

用法：
    python -m scripts.momentum_edge --years 4
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.core.config import load_settings
from app.core.db import create_engine, create_session_factory
from scripts.receipt_edge import _spearman

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "backtest"
WINDOWS = (1, 2, 3, 5, 10, 20, 60)
MIN_SAMPLES = 200
MIN_IC = 0.05


def evaluate_momentum(closes: list[tuple[dt.date, float]], window: int) -> dict[str, Any]:
    """动量 k 日变化 vs 次日收益。返回全样本/前后半段 IC 与命中率。"""
    xs: list[float] = []
    ys: list[float] = []
    hit = total = 0
    for i in range(window, len(closes) - 1):
        base = closes[i - window][1]
        cur = closes[i][1]
        nxt = closes[i + 1][1]
        if base <= 0 or cur <= 0:
            continue
        mom = cur / base - 1
        ret = (nxt / cur - 1) * 100
        if mom == 0 or ret == 0:
            continue
        xs.append(mom)
        ys.append(ret)
        total += 1
        if mom * ret > 0:
            hit += 1
    if total < MIN_SAMPLES:
        return {"n": total, "ic": None, "ic_first": None, "ic_second": None, "hit": None}
    mid = total // 2
    return {
        "n": total,
        "ic": _spearman(xs, ys),
        "ic_first": _spearman(xs[:mid], ys[:mid]),
        "ic_second": _spearman(xs[mid:], ys[mid:]),
        "hit": hit / total,
    }


def _verdict(r: dict[str, Any]) -> str:
    ic, a, b = r["ic"], r["ic_first"], r["ic_second"]
    if r["n"] < MIN_SAMPLES or ic is None or a is None or b is None:
        return "样本不足"
    if ic >= MIN_IC and a >= MIN_IC / 2 and b >= MIN_IC / 2:
        return "稳定动量"
    if ic <= -MIN_IC and a <= -MIN_IC / 2 and b <= -MIN_IC / 2:
        return "稳定反转"
    if a * b < 0:
        return "前后半段变号，不稳"
    if abs(ic) < MIN_IC:
        return "无 edge"
    return "单侧，待观察"


async def run(years: int) -> Path:
    settings = load_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    today = dt.date.today()
    since = today.replace(year=today.year - years)
    try:
        async with session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT variety_code, trade_date, close FROM daily_bar"
                        " WHERE trade_date >= :a ORDER BY variety_code, trade_date"
                    ),
                    {"a": since},
                )
            ).all()
    finally:
        await engine.dispose()

    series: dict[str, list[tuple[dt.date, float]]] = defaultdict(list)
    for row in rows:
        series[row.variety_code].append((row.trade_date, float(row.close)))

    out: dict[str, dict[int, dict[str, Any]]] = {
        code: {w: evaluate_momentum(bars, w) for w in WINDOWS} for code, bars in series.items()
    }
    return _render(out, since, today)


def _render(out: dict[str, dict[int, dict[str, Any]]], since: dt.date, until: dt.date) -> Path:
    lines = [
        f"# 动量/反转因子验证 · {until.isoformat()}",
        "",
        f"- 区间：{since.isoformat()} ~ {until.isoformat()}",
        f"- 窗口：{', '.join(str(w) for w in WINDOWS)} 日",
        f"- 稳定判定：全样本 |IC| ≥ {MIN_IC} 且前后半段同号、各自 ≥ {MIN_IC / 2}",
        "",
        "| 品种 | 窗口 | 样本 | IC | 前半 | 后半 | 命中% | 判定 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    print(f"{'品种':<6}{'窗口':>5}{'样本':>7}{'IC':>8}{'前半':>8}{'后半':>8}{'命中%':>8}  判定")
    picks: list[str] = []
    for code in sorted(out):
        for w in WINDOWS:
            r = out[code][w]
            ic_s = "—" if r["ic"] is None else f"{r['ic']:+.3f}"
            a_s = "—" if r["ic_first"] is None else f"{r['ic_first']:+.3f}"
            b_s = "—" if r["ic_second"] is None else f"{r['ic_second']:+.3f}"
            hit_s = "—" if r["hit"] is None else f"{r['hit'] * 100:.1f}"
            verdict = _verdict(r)
            if verdict in ("稳定动量", "稳定反转"):
                picks.append(f"{code}({w}日 {verdict})")
            lines.append(
                f"| {code} | {w} | {r['n']} | {ic_s} | {a_s} | {b_s} | {hit_s} | {verdict} |"
            )
            print(f"{code:<6}{w:>5}{r['n']:>7}{ic_s:>8}{a_s:>8}{b_s:>8}{hit_s:>8}  {verdict}")

    lines += ["", "## 可用清单", ""]
    lines += [f"- {p}" for p in picks] or ["- 无：所有品种全窗口均不满足稳定性要求"]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"momentum_edge_{until:%Y%m%d}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="动量/反转因子验证")
    parser.add_argument("--years", type=int, default=4)
    args = parser.parse_args()
    print(f"report: {asyncio.run(run(args.years))}")


if __name__ == "__main__":
    main()
