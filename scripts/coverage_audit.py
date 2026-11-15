"""覆盖率归因：把「不预测」的日子拆开，看是缺输入还是被阈值切掉。

用法：
    python -m scripts.coverage_audit                    # 最近 4 年
    python -m scripts.coverage_audit --since 2022-01-01

对每个品种的每个交易日打一个标签（口径见 ``_classify_day``）：

    A 无输入   当天没有一条传导链可用（q 未校准 / 外盘缺数据），exp 必然为 0
    B 被切掉   有输入，但 |exp_change| 落在震荡带内
    C 命中     给出方向且与次日实际方向一致
    D 反向     给出方向但与次日实际方向相反

输出 ``docs/backtest/coverage_audit_YYYYMMDD.md`` + 终端表格，只读数据库。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.core.config import load_settings
from app.core.db import create_engine, create_session_factory
from scripts.backtest import (
    _build_records,
    _load_daily,
    _load_edges,
    _load_foreign,
    _load_seasonality,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "backtest"


def _classify_day(row: dict[str, Any]) -> str:
    """A/B/C/D 四分类；阈值与线上 ``bands()`` 完全同一函数。"""
    if row["n_eff_edges"] == 0:
        return "A"
    if abs(row["exp_change"]) <= row["flat_band"]:
        return "B"
    if row["direction"] == row["actual_dir"]:
        return "C"
    return "D"


def _verdict(stats: dict[str, Any]) -> str:
    """归因结论：优先级 A(缺输入) > B(阈值) > 不可预测 > 正常。"""
    n = stats["n"]
    if n == 0:
        return "无样本"
    a_ratio = stats["A"] / n
    with_input = n - stats["A"]
    b_ratio = stats["B"] / with_input if with_input else 0.0
    directional = stats["C"] + stats["D"]
    acc = stats["C"] / directional if directional else None

    if a_ratio >= 0.5:
        return "缺输入"
    if b_ratio >= 0.8:
        return "阈值偏严"
    if acc is not None and acc < 0.52:
        return "不可预测"
    return "正常"


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按品种聚合；同时算「震荡带 ×0.5」的反事实覆盖率/命中率，用于判断调阈值会不会塌。"""
    by_code: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "n": 0,
            "A": 0,
            "B": 0,
            "C": 0,
            "D": 0,
            "abs_sum": 0.0,
            "band_sum": 0.0,
            "alt_n": 0,
            "alt_hit": 0,
            "alt_cov": 0,
        }
    )
    for row in rows:
        tag = _classify_day(row)
        s = by_code[row["code"]]
        s["n"] += 1
        s[tag] += 1
        s["abs_sum"] += abs(row["exp_change"]) / 100  # % → 小数，便于均值对比阈值
        s["band_sum"] += row["flat_band"]

        if row["n_eff_edges"] > 0:
            s["alt_n"] += 1
            if abs(row["exp_change"]) > row["flat_band"] * 0.5:
                s["alt_cov"] += 1
                pred = 1 if row["exp_change"] > 0 else -1
                if pred == row["actual_dir"]:
                    s["alt_hit"] += 1
    return by_code


def _fmt(stats: dict[str, Any]) -> dict[str, Any]:
    n = stats["n"] or 1
    alt_cov_n = stats.get("alt_cov", 0)
    return {
        "n": stats["n"],
        "a_pct": stats["A"] / n * 100,
        "b_pct": (stats["B"] / (n - stats["A"]) * 100) if n > stats["A"] else 0.0,
        "cov_pct": (stats["C"] + stats["D"]) / n * 100,
        "acc": (stats["C"] / (stats["C"] + stats["D"]) * 100)
        if (stats["C"] + stats["D"])
        else None,
        "abs_exp": stats["abs_sum"] / n * 100,  # 回到 %
        "band": stats["band_sum"] / n,
        "alt_cov_pct": alt_cov_n / n * 100,
        "alt_acc": (stats["alt_hit"] / alt_cov_n * 100) if alt_cov_n else None,
        "verdict": _verdict(stats),
    }


def _render(rows: list[dict[str, Any]], since: dt.date, until: dt.date) -> str:
    by_code = _aggregate(rows)
    total = {"n": 0, "A": 0, "B": 0, "C": 0, "D": 0, "abs_sum": 0.0, "band_sum": 0.0}
    for stats in by_code.values():
        for key in total:
            total[key] += stats[key]

    lines = [
        f"# 覆盖率归因 · {since.isoformat()} ~ {until.isoformat()}",
        "",
        "- A 无输入：当天没有可用传导链（q 未校准或外盘缺数据）",
        "- B 被切掉：有输入但 |exp_change| ≤ 震荡带",
        "- C 命中 / D 反向：越过震荡带、给过方向的日子",
        "",
        "## 全样本",
        "",
        f"- 样本 {total['n']}，A {total['A'] / total['n'] * 100:.1f}%，"
        f"B {total['B'] / total['n'] * 100:.1f}%，"
        f"C {total['C'] / total['n'] * 100:.1f}%，D {total['D'] / total['n'] * 100:.1f}%",
        "",
        "## 分品种",
        "",
        "| 品种 | 样本 | 无输入% | 有输入中被切% | 覆盖率% | 方向命中% | 平均\\|exp\\|% "
        "| 震荡带% | 半阈值覆盖% | 半阈值命中% | 归因 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    order = sorted(by_code.items(), key=lambda kv: _fmt(kv[1])["cov_pct"])
    for code, stats in order:
        f = _fmt(stats)
        acc = "—" if f["acc"] is None else f"{f['acc']:.1f}"
        alt_acc = "—" if f["alt_acc"] is None else f"{f['alt_acc']:.1f}"
        lines.append(
            f"| {code} | {f['n']} | {f['a_pct']:.1f} | {f['b_pct']:.1f} | "
            f"{f['cov_pct']:.1f} | {acc} | {f['abs_exp']:.3f} | {f['band']:.3f} | "
            f"{f['alt_cov_pct']:.1f} | {alt_acc} | {f['verdict']} |"
        )
    buckets: dict[str, list[str]] = defaultdict(list)
    for code, stats in by_code.items():
        buckets[_fmt(stats)["verdict"]].append(code)
    lines += ["", "## 归因汇总", ""]
    for verdict, codes in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"- **{verdict}**（{len(codes)}）：{' '.join(sorted(codes))}")

    # 是否值得下调阈值：半阈值后覆盖率上升且命中率仍 ≥55%（样本 ≥30）
    cuttable = [
        code
        for code, stats in by_code.items()
        if _fmt(stats)["alt_acc"] is not None
        and _fmt(stats)["alt_acc"] >= 55.0
        and stats["alt_cov"] >= 30
    ]
    lines += [
        "",
        "## 下调阈值的影响（震荡带 ×0.5）",
        "",
        f"- 半阈值后命中率仍 ≥55%、样本 ≥30 的品种（**可考虑下调**）："
        f"{' '.join(sorted(cuttable)) if cuttable else '无'}",
        "- 未列入的品种：要么半阈值后命中率跌破 55%（调了就是拿覆盖率换准确率），"
        "要么样本不足。",
    ]
    return "\n".join(lines) + "\n"


async def run(since: dt.date, until: dt.date) -> Path:
    settings = load_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            edges = await _load_edges(session)
            seasonality = await _load_seasonality(session)
            bars_by_code = await _load_daily(session, since, until)
            ext_codes = sorted({e.ext for items in edges.values() for e in items})
            foreign = await _load_foreign(session, ext_codes, since, until)

        rows: list[dict[str, Any]] = []
        for code, bars in sorted(bars_by_code.items()):
            detail: list[dict[str, Any]] = []
            _build_records(
                code,
                bars,
                edges.get(code, []),
                foreign,
                seasonality,
                since,
                until,
                q_mode="pit",
                detail=detail,
            )
            rows.extend(detail)

        md = _render(rows, since, until)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUTPUT_DIR / f"coverage_audit_{dt.date.today():%Y%m%d}.md"
        path.write_text(md, encoding="utf-8")
        print(md)
        print(f"\nreport: {path}")
        return path
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="覆盖率归因（A 无输入 / B 被切掉 / C 命中 / D 反向）")
    today = dt.date.today()
    parser.add_argument(
        "--since", type=dt.date.fromisoformat, default=today.replace(year=today.year - 4)
    )
    parser.add_argument("--until", type=dt.date.fromisoformat, default=today)
    parser.add_argument("--years", type=int, default=None, help="等价于 --since 今天往前 N 年")
    args = parser.parse_args()
    since = args.since if args.years is None else today.replace(year=today.year - args.years)
    asyncio.run(run(since, args.until))


if __name__ == "__main__":
    main()
