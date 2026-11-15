"""回测基线一键运行：检查数据 → 按需回补 → 跑 Layer A 回测 → 打印分品种命中率。

用法：
    python -m scripts.baseline                # 默认 3 年窗口
    python -m scripts.baseline --years 4
    python -m scripts.baseline --check-only   # 只打印数据检查结果，不动数据库

设计目标：一次命令给出「哪些品种现在基本是瞎猜」，作为后续加特征（价差、
基差、外盘联动、资讯情绪）的对照基线。回测本身与线上共用
``jobs.predict.rules.score``，不使用任何未来数据。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.core.config import load_settings
from app.core.db import create_engine, create_session_factory

ROOT = Path(__file__).resolve().parents[1]

# 数据充足性门槛
MIN_DAILY_DAYS = 260
MIN_FOREIGN_ROWS = 1800
# 判定门槛（方向命中率）
GOOD_ACC = 0.55
BAD_ACC = 0.50
MIN_SAMPLE = 40

MODULES = {
    "daily": "scripts.backfill_daily",
    "foreign": "scripts.backfill_foreign",
    "knowledge": "scripts.import_knowledge",
}

REASONS = {
    "daily": "历史日线不足",
    "foreign": "外盘日线不足",
    "knowledge": "知识库未导入",
}


async def _probe(since: dt.date) -> dict[str, Any]:
    """读取数据现状，不做任何写入。"""
    settings = load_settings()
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            daily = (
                await session.execute(
                    text(
                        "SELECT count(DISTINCT trade_date) AS days,"
                        " min(trade_date) AS lo, max(trade_date) AS hi FROM daily_bar"
                    )
                )
            ).one()
            foreign = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM foreign_daily WHERE trade_date >= :a"
                    ),
                    {"a": since},
                )
            ).scalar_one()
            edges = (
                await session.execute(text("SELECT count(*) FROM knowledge_edge"))
            ).scalar_one()
            seasons = (
                await session.execute(text("SELECT count(*) FROM seasonality"))
            ).scalar_one()
    finally:
        await engine.dispose()

    return {
        "daily_days": int(daily.days or 0),
        "daily_lo": daily.lo,
        "daily_hi": daily.hi,
        "foreign_rows": int(foreign or 0),
        "edges": int(edges or 0),
        "seasons": int(seasons or 0),
    }


def _planned_steps(checks: dict[str, Any], since: dt.date) -> list[str]:
    """根据检查结果决定要跑哪些回补。纯函数，便于单测。"""
    steps: list[str] = []
    lo = checks.get("daily_lo")
    if checks["daily_days"] < MIN_DAILY_DAYS or lo is None or lo > since + dt.timedelta(days=45):
        steps.append("daily")
    if checks["foreign_rows"] < MIN_FOREIGN_ROWS:
        steps.append("foreign")
    if checks["edges"] == 0 or checks["seasons"] == 0:
        steps.append("knowledge")
    return steps


def _mark(ok: bool) -> str:
    return "OK" if ok else "缺"


def _print_checks(checks: dict[str, Any], since: dt.date, steps: list[str]) -> None:
    lo = checks["daily_lo"].isoformat() if checks["daily_lo"] else "-"
    hi = checks["daily_hi"].isoformat() if checks["daily_hi"] else "-"
    enough_days = "daily" not in steps
    enough_foreign = "foreign" not in steps
    enough_kb = "knowledge" not in steps
    print("== 数据检查 ==")
    print(f"  窗口起点        {since.isoformat()}")
    print(f"  日线交易日      {checks['daily_days']:>6} 天 ({lo} ~ {hi})  [{_mark(enough_days)}]")
    print(f"  外盘日线        {checks['foreign_rows']:>6} 行              [{_mark(enough_foreign)}]")
    print(
        f"  知识库          edge {checks['edges']} / seasonality {checks['seasons']}"
        f"      [{_mark(enough_kb)}]"
    )
    if steps:
        print("  待回补：" + "、".join(f"{s}({REASONS[s]})" for s in steps))
    else:
        print("  待回补：无")


def _run_module(name: str) -> None:
    module = MODULES[name]
    print(f"\n== 回补 {name} ==\n$ python -m {module}", flush=True)
    result = subprocess.run([sys.executable, "-m", module], cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(f"{module} 失败（exit {result.returncode}），已中止回测")


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:5.1f}%"


def _fmt_num(value: float | None, nd: int = 3) -> str:
    return "—" if value is None else f"{value:.{nd}f}"


def _verdict(n: int, accuracy: float | None) -> str:
    if n < MIN_SAMPLE or accuracy is None:
        return "样本不足"
    if accuracy >= GOOD_ACC:
        return "可用"
    if accuracy >= BAD_ACC:
        return "偏弱"
    return "接近随机"


def _print_report(json_path: Path) -> None:
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    dev = payload["dev"]
    holdout = payload["holdout"]
    print("\n== 整体指标 ==")
    print(
        f"  开发集   n={dev['n']:<5} 方向命中 {_fmt_pct(dev['direction_accuracy'])}"
        f"  覆盖率 {_fmt_pct(dev['coverage'])}  Brier {_fmt_num(dev['brier_direction'], 4)}"
    )
    print(
        f"  holdout  n={holdout['n']:<5} 方向命中 {_fmt_pct(holdout['direction_accuracy'])}"
        f"  覆盖率 {_fmt_pct(holdout['coverage'])}  Brier {_fmt_num(holdout['brier_direction'], 4)}"
    )

    items = payload["per_variety"]
    rows = sorted(
        ((code, m) for code, m in items.items()),
        key=lambda kv: (
            kv[1]["n"] < MIN_SAMPLE,
            kv[1]["direction_accuracy"] if kv[1]["direction_accuracy"] is not None else 1.0,
        ),
    )
    print("\n== 分品种命中率（按命中率升序，最差的在最上面）==")
    print(f"  {'品种':<6}{'样本':>6}{'方向命中':>10}{'覆盖率':>9}{'Brier':>8}  判定")
    weak: list[str] = []
    for code, m in rows:
        acc = m["direction_accuracy"]
        verdict = _verdict(m["n"], acc)
        print(
            f"  {code:<6}{m['n']:>6}{_fmt_pct(acc):>10}{_fmt_pct(m['coverage']):>9}"
            f"{_fmt_num(m['brier_direction'], 4):>8}  {verdict}"
        )
        if verdict in {"接近随机", "偏弱"}:
            weak.append(code)

    print(f"\n  报告：{json_path.with_suffix('.md')}")
    print(f"  明细：{json_path}")
    if weak:
        print(f"\n  优先补特征的品种（{len(weak)} 个）：{' '.join(weak)}")
    else:
        print("\n  所有品种均达到 50% 以上，暂无需要定向补特征的品种")


async def _main(years: int, check_only: bool) -> int:
    today = dt.date.today()
    try:
        since = today.replace(year=today.year - years)
    except ValueError:  # 2/29
        since = today.replace(year=today.year - years, day=28)

    checks = await _probe(since)
    steps = _planned_steps(checks, since)
    _print_checks(checks, since, steps)

    if check_only:
        print("\n--check-only：未执行回补与回测")
        return 0

    for step in steps:
        _run_module(step)

    print("\n== 回测 ==", flush=True)
    from scripts.backtest import run as run_backtest

    try:
        report = await run_backtest(since, today)
    except SystemExit as exc:
        print(f"\n回测中止：{exc}")
        return 1

    _print_report(report.with_suffix(".json"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="回测基线一键运行（检查→回补→回测→分品种命中率）")
    parser.add_argument("--years", type=int, default=3, help="回测窗口年数，默认 3")
    parser.add_argument("--check-only", action="store_true", help="只检查数据，不回补不回测")
    args = parser.parse_args()
    import asyncio

    return asyncio.run(_main(args.years, args.check_only))


if __name__ == "__main__":
    raise SystemExit(main())
