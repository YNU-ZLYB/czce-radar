"""收益回测：把方向信号换算成钱，而不是命中率。

用法（worker 容器内）::

    python -m scripts.pnl_backtest                # 最近 4 年，全品种
    python -m scripts.pnl_backtest --codes TA,MA  # 只看部分品种
    python -m scripts.pnl_backtest --years 2

口径
----
- 信号：生产规则 ``score()``（PIT q），与命中率回测同源、同数据。
- 交易：信号日收盘建仓，下一交易日收盘平仓，方向 = 多/空，不加杠杆、不止损。
- 单笔毛收益：``dir * (close[t+1]/close[t] - 1)``（%），成本按双边合计 bps 扣减。
- 组合：每天资金等权分配到当天所有信号，无信号则空仓（当日收益 0），逐日复利。

诚实声明（必须随结果一起读）
--------------------------
1. 行情是未复权主力连续价，换月跳空被当成真实盈亏，可能虚增也可能虚减。
2. 未处理涨跌停无法成交、夜盘流动性、保证金占用与强平。
3. 成本是统一假设值而非逐品种真实费率，因此输出 0/4/8/12bps 敏感性。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.core.config import load_settings
from app.core.db import create_engine, create_session_factory
from jobs.predict.rules import band_scale_for
from scripts.backtest import (
    _build_records,
    _load_daily,
    _load_edges,
    _load_foreign,
    _load_seasonality,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "backtest"
COST_BPS = (0.0, 4.0, 8.0, 12.0)
GATE = 0.66


@dataclass
class Trade:
    code: str
    day: dt.date
    direction: int
    confidence: float
    gross: float  # %，未扣成本


def trades_from_records(records, gate: float | None) -> list[Trade]:
    out: list[Trade] = []
    for r in records:
        d = int(r.pred_dir)
        if d == 0:
            continue
        if gate is not None and float(r.confidence) < gate:
            continue
        out.append(
            Trade(
                code=r.variety_code,
                day=r.trade_date,
                direction=d,
                confidence=float(r.confidence),
                gross=d * float(r.actual_change),
            )
        )
    return out


def evaluate(trades: list[Trade], days: list[dt.date], cost_bps: float) -> dict:
    """等权日频组合指标 + 逐笔统计。复杂度 O(交易数 + 天数)。"""
    cost = cost_bps / 100.0  # bps -> %
    pos = {d: i for i, d in enumerate(days)}
    sums = np.zeros(len(days))
    counts = np.zeros(len(days), dtype=int)
    net = np.empty(len(trades))
    for i, t in enumerate(trades):
        nv = t.gross - cost
        net[i] = nv
        j = pos.get(t.day)
        if j is not None:
            sums[j] += nv
            counts[j] += 1
    daily = np.where(counts > 0, sums / np.maximum(counts, 1), 0.0)

    eq = np.cumprod(1.0 + daily / 100.0) if len(days) else np.array([1.0])
    peak = np.maximum.accumulate(eq)
    mdd = float((eq / peak - 1.0).min())
    n = len(days)
    ann = float(eq[-1] ** (252.0 / n) - 1.0) if n and eq[-1] > 0 else float("nan")
    sd = float(daily.std(ddof=1)) if n > 1 else 0.0
    sharpe = float(daily.mean() / sd * math.sqrt(252)) if sd > 0 else 0.0
    losses = net[net < 0]
    pf = float(net[net > 0].sum() / abs(losses.sum())) if losses.sum() != 0 else float("inf")
    return {
        "trades": len(trades),
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_net_bp": float(net.mean() * 100) if len(net) else 0.0,
        "profit_factor": pf,
        "total_return": float(eq[-1] - 1.0),
        "annual_return": ann,
        "sharpe": sharpe,
        "max_drawdown": mdd,
        "active_days": int((counts > 0).sum()),
        "avg_positions": float(counts[counts > 0].mean()) if (counts > 0).any() else 0.0,
        "daily": daily,
        "equity": eq,
        "net": net,
    }


def yearly(trades: list[Trade], cost_bps: float) -> dict[int, dict]:
    cost = cost_bps / 100.0
    out: dict[int, dict] = {}
    for y in sorted({t.day.year for t in trades}):
        net = np.array([t.gross - cost for t in trades if t.day.year == y])
        out[y] = {
            "trades": len(net),
            "win_rate": float((net > 0).mean()),
            "avg_net_bp": float(net.mean() * 100),
            "sum_net": float(net.sum()),
        }
    return out


def by_code(trades: list[Trade], cost_bps: float) -> list[tuple[str, int, float, float]]:
    cost = cost_bps / 100.0
    rows = []
    for code in sorted({t.code for t in trades}):
        net = np.array([t.gross - cost for t in trades if t.code == code])
        rows.append((code, len(net), float((net > 0).mean()), float(net.mean() * 100)))
    rows.sort(key=lambda r: r[3])
    return rows


def _row(name: str, m: dict) -> str:
    return (
        f"| {name} | {m['trades']} | {m['win_rate']:.1%} | {m['avg_net_bp']:+.1f} | "
        f"{m['profit_factor']:.2f} | {m['total_return']:+.1%} | {m['annual_return']:+.1%} | "
        f"{m['sharpe']:.2f} | {m['max_drawdown']:.1%} | {m['avg_positions']:.1f} |"
    )


def run_report(configs: dict[str, list[Trade]], days: list[dt.date], since: dt.date, until: dt.date) -> str:
    lines = [
        f"# 收益回测 · {dt.date.today():%Y-%m-%d}",
        f"- 区间 {since} ~ {until}，{len(days)} 个交易日",
        "- 信号日收盘建仓、次日收盘平仓；每日等权分配到有信号的品种，无信号则空仓",
        "- 未复权连续价（换月跳空计入盈亏）、未处理涨跌停与保证金；成本为统一假设",
        "",
        "## 成本 8bps（双边合计）下的组合表现",
        "| 配置 | 交易数 | 净胜率 | 每笔净收益bp | 盈亏比 | 累计收益 | 年化 | 夏普 | 最大回撤 | 平均持仓数 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, trades in configs.items():
        lines.append(_row(name, evaluate(trades, days, 8.0)))

    lines += [
        "",
        "## 成本敏感性（年化收益 / 夏普）",
        "| 配置 | 0bps | 4bps | 8bps | 12bps |",
        "|---|---|---|---|---|",
    ]
    for name, trades in configs.items():
        cells = []
        for c in COST_BPS:
            m = evaluate(trades, days, c)
            cells.append(f"{m['annual_return']:+.1%} / {m['sharpe']:.2f}")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")

    for name, trades in configs.items():
        lines += [
            "",
            f"## 分年度（{name}，8bps）",
            "| 年份 | 交易数 | 净胜率 | 每笔净收益bp | 净收益合计% |",
            "|---|---|---|---|---|",
        ]
        for y, r in yearly(trades, 8.0).items():
            lines.append(
                f"| {y} | {r['trades']} | {r['win_rate']:.1%} | {r['avg_net_bp']:+.1f} | {r['sum_net']:+.1f} |"
            )

    key = f"live+gate{GATE}" if f"live+gate{GATE}" in configs else next(iter(configs))
    lines += [
        "",
        f"## 分品种（{key}，8bps，按每笔净收益升序）",
        "| 品种 | 交易数 | 净胜率 | 每笔净收益bp |",
        "|---|---|---|---|",
    ]
    for code, n, wr, avg in by_code(configs[key], 8.0):
        lines.append(f"| {code} | {n} | {wr:.1%} | {avg:+.1f} |")
    return "\n".join(lines)


async def run(since: dt.date, until: dt.date, codes_filter: list[str] | None) -> None:
    settings = load_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            edges = await _load_edges(session)
            season = await _load_seasonality(session)
            daily = await _load_daily(session, since, until)
            ext_codes = sorted({e.ext for items in edges.values() for e in items})
            foreign = await _load_foreign(session, ext_codes, since, until)

        days = sorted({b.day for bars in daily.values() for b in bars if since <= b.day <= until})
        configs: dict[str, list[Trade]] = {
            "live(线上带宽无门槛)": [],
            f"live+gate{GATE}": [],
            "scale1.0(无门槛)": [],
            f"scale1.0+gate{GATE}": [],
        }
        for code in sorted(daily):
            if codes_filter and code not in codes_filter:
                continue
            bars = daily[code]
            if len(bars) < 80:
                continue
            for prefix, scale in (("live", band_scale_for(code)), ("scale1.0", 1.0)):
                recs = _build_records(
                    code, bars, edges.get(code, []), foreign, season, since, until, "pit",
                    band_scale=scale,
                )
                if prefix == "live":
                    configs["live(线上带宽无门槛)"].extend(trades_from_records(recs, None))
                    configs[f"live+gate{GATE}"].extend(trades_from_records(recs, GATE))
                else:
                    configs["scale1.0(无门槛)"].extend(trades_from_records(recs, None))
                    configs[f"scale1.0+gate{GATE}"].extend(trades_from_records(recs, GATE))

        report = run_report(configs, days, since, until)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUTPUT_DIR / f"pnl_{dt.date.today():%Y%m%d}.md"
        path.write_text(report, encoding="utf-8")
        print(report)
        print(f"\nreport: {path}")
    finally:
        await engine.dispose()


def main() -> None:
    ap = argparse.ArgumentParser(description="方向信号收益回测")
    ap.add_argument("--since", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--until", type=dt.date.fromisoformat, default=dt.date.today())
    ap.add_argument("--years", type=int, default=4)
    ap.add_argument("--codes", default=None)
    args = ap.parse_args()
    since = args.since or (args.until - dt.timedelta(days=365 * args.years))
    codes = [c.strip().upper() for c in args.codes.split(",")] if args.codes else None
    asyncio.run(run(since, args.until, codes))


if __name__ == "__main__":
    main()
