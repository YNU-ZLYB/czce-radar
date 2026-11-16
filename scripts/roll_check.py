"""换月污染诊断：动量/反转信号里有多少来自主力连续合约的换月跳空。

背景：daily_bar 用的是新浪 `SR0`（主力连续，未复权）。主力换月那天，收盘价
从旧合约跳到新合约，产生一段假涨跌，统计上会伪装成"反转"信号，同时抬高
ATR 与波动率阈值。在拿动量做任何决策之前，必须先量出这块污染有多大。

识别换月日：单日 |涨跌| ≥ 3% 且 持仓量环比下降 ≥ 20%（换月的典型形态：
旧合约持仓清空、新合约接棒）。
对照：剔除嫌疑日及其前后各 1 个交易日后重算 IC，看信号是否随之消失。

用法：
    python -m scripts.roll_check --years 4
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
from scripts.momentum_edge import evaluate_momentum

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "backtest"
WINDOWS = (1, 3, 5, 10)
JUMP = 0.03
OI_DROP = -0.20
MIN_MOVE = 0.05


def detect_rolls(
    bars: list[tuple[dt.date, float, int | None]],
    jump: float = JUMP,
    oi_drop: float = OI_DROP,
) -> set[dt.date]:
    """返回疑似换月日：大涨跌 + 持仓骤降。缺持仓数据的日子不判疑。"""
    flagged: set[dt.date] = set()
    for i in range(1, len(bars)):
        _day, close, oi = bars[i]
        prev_close = bars[i - 1][1]
        prev_oi = bars[i - 1][2]
        if prev_close <= 0 or close <= 0 or oi is None or prev_oi is None or prev_oi <= 0:
            continue
        ret = close / prev_close - 1
        oi_chg = oi / prev_oi - 1
        if abs(ret) >= jump and oi_chg <= oi_drop:
            flagged.add(bars[i][0])
    return flagged


def expand_around(days: list[dt.date], flagged: set[dt.date], span: int = 1) -> set[dt.date]:
    """把嫌疑日扩成「前后各 span 个交易日」，避开跳空的余波。"""
    index = {d: i for i, d in enumerate(days)}
    out: set[dt.date] = set()
    for day in flagged:
        i = index.get(day)
        if i is None:
            continue
        for k in range(max(0, i - span), min(len(days), i + span + 1)):
            out.add(days[k])
    return out


def _jump_stats(
    bars: list[tuple[dt.date, float, int | None]], flagged: set[dt.date]
) -> tuple[float, float]:
    """嫌疑日与非嫌疑日的平均 |涨跌|（%）。"""

    def _mean(days: set[dt.date] | None) -> float:
        vals: list[float] = []
        for i in range(1, len(bars)):
            if days is None:
                if bars[i][0] in flagged:
                    continue
            elif bars[i][0] not in days:
                continue
            prev = bars[i - 1][1]
            if prev > 0:
                vals.append(abs(bars[i][1] / prev - 1) * 100)
        return sum(vals) / len(vals) if vals else 0.0

    return _mean(flagged), _mean(None)


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
                        "SELECT variety_code, trade_date, close, oi FROM daily_bar"
                        " WHERE trade_date >= :a ORDER BY variety_code, trade_date"
                    ),
                    {"a": since},
                )
            ).all()
    finally:
        await engine.dispose()

    bars: dict[str, list[tuple[dt.date, float, int | None]]] = defaultdict(list)
    for row in rows:
        bars[row.variety_code].append(
            (row.trade_date, float(row.close), int(row.oi) if row.oi is not None else None)
        )

    results: dict[str, dict[str, Any]] = {}
    for code, series in bars.items():
        days = [b[0] for b in series]
        flagged = detect_rolls(series)
        cleaned = expand_around(days, flagged)
        jump_avg, normal_avg = _jump_stats(series, flagged)
        per_window = {
            w: {
                "full": evaluate_momentum(series, w),
                "clean": evaluate_momentum(series, w, exclude=cleaned),
            }
            for w in WINDOWS
        }
        results[code] = {
            "days": len(series),
            "rolls": len(flagged),
            "share": len(flagged) / max(1, len(series)),
            "jump_avg": jump_avg,
            "normal_avg": normal_avg,
            "windows": per_window,
        }

    return _render(results, since, today)


def _render(results: dict[str, dict[str, Any]], since: dt.date, until: dt.date) -> Path:
    lines = [
        f"# 换月污染诊断 · {until.isoformat()}",
        "",
        f"- 区间：{since.isoformat()} ~ {until.isoformat()}",
        f"- 换月判定：单日 |涨跌| ≥ {JUMP:.0%} 且 持仓环比 ≤ {OI_DROP:.0%}",
        "- 清洗口径：剔除嫌疑日 + 前后各 1 个交易日",
        "",
        "## 换月嫌疑日分布",
        "",
        "| 品种 | 交易日 | 嫌疑日 | 占比% | 嫌疑日均\\|涨跌\\|% | 其余日均\\|涨跌\\|% |",
        "|---|---|---|---|---|---|",
    ]
    for code in sorted(results):
        r = results[code]
        lines.append(
            f"| {code} | {r['days']} | {r['rolls']} | {r['share'] * 100:.2f} |"
            f" {r['jump_avg']:.2f} | {r['normal_avg']:.2f} |"
        )

    lines += [
        "",
        "## 剔除换月日后 IC 变化",
        "",
        "| 品种 | 窗口 | IC 全样本 | IC 清洗后 | 变化 | 判定 |",
        "|---|---|---|---|---|---|",
    ]
    print(f"{'品种':<6}{'换月日':>8}{'占比%':>8}{'窗口':>5}{'IC全':>9}{'IC清洗':>9}  判定")
    for code in sorted(results):
        r = results[code]
        for w in WINDOWS:
            full = r["windows"][w]["full"]["ic"]
            clean = r["windows"][w]["clean"]["ic"]
            if full is None or clean is None:
                continue
            delta = clean - full
            if abs(full) >= MIN_MOVE and abs(clean) < MIN_MOVE:
                verdict = "信号来自换月，不可用"
            elif abs(full) >= MIN_MOVE and abs(clean) >= MIN_MOVE:
                verdict = "清洗后仍成立"
            elif abs(full) < MIN_MOVE:
                verdict = "本就无信号"
            else:
                verdict = "样本不足"
            lines.append(
                f"| {code} | {w} | {full:+.3f} | {clean:+.3f} | {delta:+.3f} | {verdict} |"
            )
            print(
                f"{code:<6}{r['rolls']:>8}{r['share'] * 100:>8.1f}{w:>5}"
                f"{full:>+9.3f}{clean:>+9.3f}  {verdict}"
            )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"roll_check_{until:%Y%m%d}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="换月污染诊断")
    parser.add_argument("--years", type=int, default=4)
    args = parser.parse_args()
    print(f"report: {asyncio.run(run(args.years))}")


if __name__ == "__main__":
    main()
