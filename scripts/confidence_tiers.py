"""置信度分层分析：当前规则给方向时，信心高低是否对应真实命中率差异。

问题背景：现在「覆盖 18.5% + 命中 65%」是所有开口日的平均值。如果高置信度日
确实更准，就可以只输出 top 档，用覆盖率换命中率——这是唯一不需要新数据、
不需要新模型的提质手段。

口径与 backtest.summarize 完全一致：命中 = 排除横盘预测后的二分类命中。
开发集 / holdout 分开看，防止在样本内挑阈值。

用法：
    python -m scripts.confidence_tiers --years 4
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

from jobs.predict.metrics import BacktestRecord, split_holdout, summarize

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "backtest"
TIERS = 5
TOPS = (10, 20, 30, 50)
MIN_TIER_SAMPLES = 30
HOLDOUT = 60


def tier_stats(records: list[BacktestRecord], tiers: int = TIERS) -> list[dict[str, Any]]:
    """按置信度降序等分，返回每档指标（最高置信度在前）。"""
    directional = [r for r in records if r.pred_dir != 0]
    if not directional:
        return []
    directional.sort(key=lambda r: r.confidence, reverse=True)
    size = max(1, len(directional) // tiers)
    out: list[dict[str, Any]] = []
    for i in range(tiers):
        start = i * size
        end = len(directional) if i == tiers - 1 else (i + 1) * size
        chunk = directional[start:end]
        if not chunk:
            continue
        stats = summarize(chunk)
        out.append(
            {
                "tier": f"T{i + 1}",
                "n": len(chunk),
                "conf_low": min(r.confidence for r in chunk),
                "conf_high": max(r.confidence for r in chunk),
                "hit": stats["direction_accuracy"],
                "hidden": stats["direction_n"],
            }
        )
    return out


def top_share_stats(
    records: list[BacktestRecord], tops: tuple[int, ...] = TOPS
) -> list[dict[str, Any]]:
    """若只保留置信度最高的 x% 方向日，命中率与样本量如何。"""
    directional = [r for r in records if r.pred_dir != 0]
    if not directional:
        return []
    directional.sort(key=lambda r: r.confidence, reverse=True)
    out: list[dict[str, Any]] = []
    for pct in tops:
        keep = max(1, round(len(directional) * pct / 100))
        stats = summarize(directional[:keep])
        if stats["direction_n"] < MIN_TIER_SAMPLES:
            continue
        out.append(
            {
                "keep_pct": pct,
                "n": keep,
                "hit": stats["direction_accuracy"],
                "hidden": stats["direction_n"],
                "conf_min": directional[keep - 1].confidence,
            }
        )
    return out


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}"


async def run(years: int) -> Path:
    # 延迟导入：让 tier_stats / top_share_stats 可在无 DB 依赖的环境单测
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

    settings = load_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    generated = dt.date.today()
    since = generated.replace(year=generated.year - years)
    try:
        async with session_factory() as session:
            edges = await _load_edges(session)
            seasonality = await _load_seasonality(session)
            daily = await _load_daily(session, since, generated)
            ext_codes = sorted({edge.ext for items in edges.values() for edge in items})
            foreign = await _load_foreign(session, ext_codes, since, generated)
    finally:
        await engine.dispose()

    records: list[BacktestRecord] = []
    for code in sorted(daily):
        records.extend(
            _build_records(
                code,
                daily[code],
                edges.get(code, []),
                foreign,
                seasonality,
                since,
                generated,
                "pit",
                band_scale=band_scale_for(code),
            )
        )
    if not records:
        raise SystemExit("无样本：请先回补 daily_bar 与 foreign_daily")

    dev, hold = split_holdout(records, HOLDOUT)
    lines = [
        f"# 置信度分层分析 · {generated.isoformat()}",
        "",
        f"- 样本：{len(records)} 行，其中开口（给出方向）"
        f"{sum(1 for r in records if r.pred_dir != 0)} 行",
        "- 口径：与 backtest 一致，命中 = 排除横盘预测后的二分类命中",
        "",
    ]
    print(f"{'集合':<8}{'档':<4}{'样本':>6}{'置信区间':>16}{'命中%':>8}")
    for name, group in (("开发集", dev), ("holdout", hold)):
        lines += [
            f"## {name}",
            "",
            "| 档 | 样本 | 置信度区间 | 方向命中% |",
            "|---|---|---|---|",
        ]
        for row in tier_stats(group):
            lines.append(
                f"| {row['tier']} | {row['n']} |"
                f" {row['conf_low']:.2f}~{row['conf_high']:.2f} | {_fmt(row['hit'])} |"
            )
            print(
                f"{name:<8}{row['tier']:<4}{row['n']:>6}"
                f"{row['conf_low']:>8.2f}~{row['conf_high']:>5.2f}{_fmt(row['hit']):>9}"
            )
        lines += [
            "",
            "### 只保留最高置信度的 x%",
            "",
            "| 保留% | 样本 | 命中% | 阈值 |",
            "|---|---|---|---|",
        ]
        for row in top_share_stats(group):
            lines.append(
                f"| {row['keep_pct']} | {row['n']} | {_fmt(row['hit'])} | {row['conf_min']:.2f} |"
            )
            print(
                f"{name:<8}top{row['keep_pct']:<4}{row['n']:>5}"
                f"{'':>14}{_fmt(row['hit']):>9}  阈值 {row['conf_min']:.2f}"
            )
        lines.append("")

    lines += [
        "## 判读",
        "",
        "- 最高档命中显著高于最低档 → 置信度有分层能力，可只输出高置信度",
        "- 各档命中接近持平 → 置信度只是阈值余量，不能用于筛选",
        "- 开发集分层、holdout 不分层 → 过拟合，不可采纳",
        "",
    ]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"confidence_tiers_{generated:%Y%m%d}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="置信度分层分析")
    parser.add_argument("--years", type=int, default=4)
    args = parser.parse_args()
    print(f"report: {asyncio.run(run(args.years))}")


if __name__ == "__main__":
    main()
