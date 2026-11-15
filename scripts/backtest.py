"""回测脚本：Layer A 规则基线的 walk-forward + 永久 holdout。

用法：
    python -m scripts.backtest                       # 最近 4 年，输出 docs/backtest/YYYYMMDD.md
    python -m scripts.backtest --since 2022-01-01
    python -m scripts.backtest --until 2026-06-30

与线上共用 ``jobs.predict.rules.score``（同一条计分路径），只用
``daily_bar / foreign_daily / knowledge_edge / seasonality``，绝不使用未来数据：
目标日 T 的输入为 T-1 及更早的国内日线、T 之前的外盘日线、T 月份的季节性。

传导弹性 ``q`` 的口径（2026-11-15 修正）：
YAML 里的 ``linkage_q`` 来自 v1 的**全样本**回测，是「用答案调出来的参数」。
直接拿它回测等于偷看未来，故默认改用 **时点估计**（point-in-time）：

    q_T = 截至 T-1 已兑现的「传导符号命中率」，Laplace 平滑，样本 <60 时置 0（不展示传导行）

同一份报告同时给出 ``static``（旧口径，含泄漏，仅作参照）与 ``pit``（可复现口径）
两组指标，便于看清泄漏把命中率抬了多少。
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import datetime as dt
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import load_settings
from app.core.db import create_engine, create_session_factory
from app.models.prediction import BacktestRun
from jobs.predict.metrics import (
    BacktestRecord,
    per_variety,
    records_to_json,
    split_holdout,
    summarize,
    v1_diagnostic,
    walk_forward,
)
from jobs.predict.rules import classify_bucket, direction_of, score

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "backtest"
TRAIN, VALIDATION, TEST, HOLDOUT = 500, 20, 20, 60
MIN_Q_SAMPLES = 60  # 时点 q 的最少已兑现样本数，不足则置 0（不展示传导行）


@dataclass(frozen=True, slots=True)
class Edge:
    name: str
    ext: str
    prior: float
    q: float


@dataclass(frozen=True, slots=True)
class Season:
    bias: float
    confidence: float
    stage: str


@dataclass(frozen=True, slots=True)
class Series:
    dates: list[dt.date]
    values: list[float]


def _std(values: list[float]) -> float | None:
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def _r(value: float, nd: int = 4) -> float:
    return round(value, nd)


def _foreign_value(series: Series, day: dt.date) -> float | None:
    """返回 trade_date < day 的最近一个外盘 change_pct。"""
    idx = bisect.bisect_left(series.dates, day) - 1
    if idx < 0:
        return None
    value = series.values[idx]
    return value if not math.isnan(value) else None


async def _load_edges(session: AsyncSession) -> dict[str, list[Edge]]:
    rows = (
        await session.execute(
            text(
                "SELECT e.variety_code, e.name, e.proxy_ext, e.prior, v.linkage_q"
                " FROM knowledge_edge e JOIN variety v ON v.code = e.variety_code"
                " WHERE e.proxy_ext IS NOT NULL"
                " ORDER BY e.variety_code, e.prior DESC"
            )
        )
    ).all()
    grouped: dict[str, list[Edge]] = defaultdict(list)
    for row in rows:
        grouped[row.variety_code].append(
            Edge(
                name=row.name,
                ext=row.proxy_ext,
                prior=float(row.prior),
                q=float(row.linkage_q),
            )
        )
    return grouped


async def _load_seasonality(session: AsyncSession) -> dict[tuple[str, int], Season]:
    rows = (
        await session.execute(
            text("SELECT variety_code, month, stage, bias, confidence FROM seasonality")
        )
    ).all()
    return {
        (row.variety_code, int(row.month)): Season(
            bias=float(row.bias),
            confidence=float(row.confidence),
            stage=row.stage or "",
        )
        for row in rows
    }


async def _load_daily(
    session: AsyncSession, since: dt.date, until: dt.date
) -> dict[str, list[tuple[dt.date, float]]]:
    rows = (
        await session.execute(
            text(
                "SELECT variety_code, trade_date, close FROM daily_bar"
                " WHERE trade_date >= :a AND trade_date <= :b"
                " ORDER BY variety_code, trade_date"
            ),
            {"a": since - dt.timedelta(days=400), "b": until},
        )
    ).all()
    grouped: dict[str, list[tuple[dt.date, float]]] = defaultdict(list)
    for row in rows:
        grouped[row.variety_code].append((row.trade_date, float(row.close)))
    return grouped


async def _load_foreign(
    session: AsyncSession, ext_codes: list[str], since: dt.date, until: dt.date
) -> dict[str, Series]:
    rows = (
        await session.execute(
            text(
                "SELECT ext_code, trade_date, change_pct FROM foreign_daily"
                " WHERE ext_code = ANY(:codes) AND trade_date >= :a AND trade_date <= :b"
                " ORDER BY ext_code, trade_date"
            ),
            {
                "codes": ext_codes,
                "a": since - dt.timedelta(days=400),
                "b": until,
            },
        )
    ).all()
    grouped: dict[str, list[tuple[dt.date, float]]] = defaultdict(list)
    for row in rows:
        if row.change_pct is None:
            continue
        grouped[row.ext_code].append((row.trade_date, float(row.change_pct)))
    return {
        ext: Series(
            dates=[d for d, _ in items],
            values=[v for _, v in items],
        )
        for ext, items in grouped.items()
    }


def _pit_q(hits: int, n: int) -> float:
    """截至昨日已兑现的传导符号命中率（Laplace 平滑）；样本不足返回 0（不展示传导行）。"""
    if n < MIN_Q_SAMPLES:
        return 0.0
    return (hits + 1) / (n + 2)


def _build_records(
    code: str,
    bars: list[tuple[dt.date, float]],
    edges: list[Edge],
    foreign: dict[str, Series],
    seasonality: dict[tuple[str, int], Season],
    since: dt.date,
    until: dt.date,
    q_mode: str = "pit",
) -> list[BacktestRecord]:
    """q_mode: pit=时点命中率（不偷看未来），static=YAML 全样本 q（含泄漏，仅参照）。"""
    if len(bars) < 22:
        return []
    closes = [c for _, c in bars]
    returns: list[float | None] = [None]
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        returns.append((closes[i] / prev - 1) if prev else None)

    records: list[BacktestRecord] = []
    hits = 0
    n_seen = 0
    for i in range(21, len(bars)):
        day = bars[i][0]

        linkage: list[dict[str, Any]] = []
        transmission = 0.0
        max_abs_foreign = 0.0
        missing = 0
        for edge in edges:
            series = foreign.get(edge.ext)
            chg = _foreign_value(series, day) if series is not None else None
            if chg is None:
                missing += 1
                continue
            max_abs_foreign = max(max_abs_foreign, abs(chg))
            transmission += chg * edge.prior
            q = _pit_q(hits, n_seen) if q_mode == "pit" else edge.q
            linkage.append(
                {
                    "name": edge.name,
                    "ext": edge.ext,
                    "prior": edge.prior,
                    "q": q,
                    "chg_1d": chg,
                }
            )

        actual_change = (closes[i] / closes[i - 1] - 1) * 100
        sigma_window = returns[i - 20 : i]
        sigma = None
        if not any(v is None for v in sigma_window):
            sigma = _std([float(v) for v in sigma_window if v is not None]) * 100

        if since <= day <= until and sigma is not None:
            season = seasonality.get((code, day.month))
            features = {
                "price": {"vol_20d": _r(sigma)},
                "linkage": linkage,
                "seasonality": {
                    "month": day.month,
                    "bias": season.bias if season else None,
                    "confidence": season.confidence if season else None,
                    "stage": season.stage if season else None,
                },
                "news": {"sentiment_lag1": None},
                "event": {"is_event_day": False},
            }
            out = score(features, missing_foreign=missing)
            actual_bucket = classify_bucket(actual_change, sigma)
            records.append(
                BacktestRecord(
                    variety_code=code,
                    trade_date=day,
                    pred_dir=out.direction,
                    pred_bucket=out.bucket,
                    p_up=out.p_up,
                    p_flat=out.p_flat,
                    p_down=out.p_down,
                    confidence=out.confidence,
                    actual_change=_r(actual_change),
                    actual_dir=direction_of(actual_bucket),
                    actual_bucket=actual_bucket,
                    transmission_sign=1
                    if transmission > 1e-12
                    else -1
                    if transmission < -1e-12
                    else 0,
                    max_abs_foreign=_r(max_abs_foreign),
                )
            )

        # 只在 T 收盘后把 (传导, 已兑现涨跌) 计入历史，供 T+1 之后估计 q —— 无未来信息
        if transmission > 1e-12 or transmission < -1e-12:
            n_seen += 1
            if actual_change > 0:
                hits += int(transmission > 0)
            elif actual_change < 0:
                hits += int(transmission < 0)
    return records


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _num(value: float | None, nd: int = 3) -> str:
    return "—" if value is None else f"{value:.{nd}f}"


def _section_metrics(title: str, data: dict[str, Any]) -> list[str]:
    return [
        f"### {title}",
        "",
        f"- 样本：{data['n']}（非震荡预测 {data['direction_n']}，覆盖率 {_pct(data['coverage'])}）",
        f"- 方向命中率：{_pct(data['direction_accuracy'])}（门槛 ≥55%）",
        f"- Balanced Accuracy：{_num(data['balanced_accuracy'])}（M4 门槛 ≥0.45）",
        f"- Brier（三分类）：{_num(data['brier'], 4)}",
        f"- Brier（方向）：{_num(data['brier_direction'], 4)}（M4 门槛 ≤0.23）",
        f"- ECE：{_num(data['ece'])}（M3 如实报告，M4 门槛 ≤0.05）",
        "",
    ]


def _render_report(
    *,
    generated: dt.date,
    start: dt.date,
    end: dt.date,
    all_records: list[BacktestRecord],
    dev: dict[str, Any],
    holdout: dict[str, Any],
    wf: dict[str, Any],
    v1: dict[str, Any],
    by_code: dict[str, dict[str, Any]],
    failures: list[BacktestRecord],
    comparison: dict[str, Any],
) -> str:
    v1_dir_ok = v1["hit_rate"] is not None and abs(float(v1["hit_rate"]) - 0.57) <= 0.02
    v1_strong_ok = (
        v1["strong_hit_rate"] is not None and abs(float(v1["strong_hit_rate"]) - 0.62) <= 0.05
    )
    strong_delta = (
        (float(v1["strong_hit_rate"]) - 0.62) * 100 if v1["strong_hit_rate"] is not None else None
    )
    lines: list[str] = [
        f"# 回测报告 · {generated.isoformat()}",
        "",
        f"- 数据范围：{start.isoformat()} ~ {end.isoformat()}，21 品种，"
        f"共 {len(all_records)} 条预测样本",
        "- 规则：Layer A（外盘传导 × 先验 × q + 季节性 + 资讯情绪×0.2），与线上同一函数",
        f"- 切分：walk-forward 训练 {TRAIN} / 验证 {VALIDATION} / 测试 {TEST}，"
        f"永久 holdout 最近 {HOLDOUT} 日",
        "",
        "## 一、v1 结论复现（纯传导符号，不用 q / 季节性）",
        "",
        "| 指标 | 结果 | 目标 | 判定 |",
        "|---|---|---|---|",
        f"| 全样本方向命中 | {_pct(v1['hit_rate'])}（n={v1['n']}） | 57%±2% |"
        f" {'✅' if v1_dir_ok else '❌'} |",
        f"| \\|外盘\\|≥0.8% 命中 | {_pct(v1['strong_hit_rate'])}（n={v1['strong_n']}） |"
        f" ≈62%（差 {strong_delta:+.1f}pp） | {'✅' if v1_strong_ok else '❌'} |",
        "",
        "## 二、生产规则指标（含季节性与概率）",
        "",
        *_section_metrics("开发集（剔除永久 holdout）", dev),
        *_section_metrics("永久 holdout（最近 60 个交易日）", holdout),
        "## 三、Walk-forward",
        "",
        f"- 折数：{wf['folds']}，单折方向命中均值：{_pct(wf['fold_accuracy_mean'])}",
        f"- 测试窗口汇总：n={wf['aggregate']['n']}，"
        f"方向命中 {_pct(wf['aggregate']['direction_accuracy'])}，"
        f"Balanced Acc {_num(wf['aggregate']['balanced_accuracy'])}，"
        f"Brier {_num(wf['aggregate']['brier'], 4)}，"
        f"ECE {_num(wf['aggregate']['ece'])}",
        "",
        "## 四、分品种命中率",
        "",
        "| 品种 | 样本 | 方向命中 | 覆盖率 | Brier(方向) | v1 传导命中 |",
        "|---|---|---|---|---|---|",
    ]
    for code, item in by_code.items():
        v1_item = v1["by_variety"].get(code, {})
        lines.append(
            f"| {code} | {item['n']} | {_pct(item['direction_accuracy'])} |"
            f" {_pct(item['coverage'])} | {_num(item['brier_direction'], 4)} |"
            f" {_pct(v1_item.get('hit_rate'))} |"
        )

    lines += [
        "",
        "## 五、泄漏对照：静态 q vs 时点 q",
        "",
        "`static` = YAML 里的 linkage_q（来自 v1 全样本回测，含未来信息）；"
        "`pit` = 截至 T-1 已兑现的传导符号命中率。正文与 JSON 的 `dev/holdout` 均取 `pit`。",
        "",
        "| 口径 | 组 | 样本 | 覆盖率 | 方向命中 | Brier(方向) |",
        "|---|---|---|---|---|---|",
    ]
    for mode in ("pit", "static"):
        for group in ("dev", "holdout"):
            item = comparison[mode][group]
            lines.append(
                f"| {mode} | {group} | {item['n']} | {_pct(item['coverage'])} |"
                f" {_pct(item['direction_accuracy'])} | {_num(item['brier_direction'], 4)} |"
            )
    pit_h, static_h = comparison["pit"]["holdout"], comparison["static"]["holdout"]
    if pit_h["direction_accuracy"] is not None and static_h["direction_accuracy"] is not None:
        delta = (static_h["direction_accuracy"] - pit_h["direction_accuracy"]) * 100
        lines += [
            "",
            f"- holdout 上静态 q 比时点 q 高 **{delta:+.1f}pp**，"
            "这部分就是泄漏带来的虚高，不应计入真实能力。",
        ]
    lines += [""]

    lines += [
        "",
        "## 六、可靠性曲线（开发集）",
        "",
        "| 置信度区间 | 样本 | 平均置信度 | 实际命中率 |",
        "|---|---|---|---|",
    ]
    for bin_item in dev["reliability"]:
        lines.append(
            f"| {bin_item['lo']:.1f}~{bin_item['hi']:.1f} | {bin_item['n']} |"
            f" {bin_item['avg_confidence']:.3f} | {bin_item['accuracy']:.3f} |"
        )

    lines += ["", "## 七、失败案例（方向做反且实际波动最大的 10 条）", ""]
    if failures:
        lines += [
            "| 日期 | 品种 | 预测 | 实际 | 实际涨跌 | 置信度 |",
            "|---|---|---|---|---|---|",
        ]
        for r in failures:
            lines.append(
                f"| {r.trade_date.isoformat()} | {r.variety_code} | {r.pred_bucket} |"
                f" {r.actual_bucket} | {r.actual_change:+.2f}% | {r.confidence:.3f} |"
            )
    else:
        lines.append("（无）")

    lines += [
        "",
        "## 八、口径说明",
        "",
        "- `exp_change` = Σ(外盘涨跌 × prior × q) + 季节 bias + 资讯情绪×0.2；"
        "`signal` 按 ×22/Σprior×q 缩放。",
        f"- `q` 用**时点估计**：截至 T-1 已兑现的传导符号命中率，Laplace 平滑，"
        f"兑现样本 <{MIN_Q_SAMPLES} 时置 0（该日前不展示传导行）。",
        "- 季节 bias 来自 `data/knowledge/*.yaml` 的人工先验，不含由历史数据拟合的参数，"
        "因此不构成泄漏。",
        "- 三分类与幅度分档按品种 σ 自适应：震荡带 max(0.5%, 0.35σ)，强档带 max(2.0%, 1.00σ)。",
        "- 目标日 T 只使用 T-1 及更早数据；外盘只取 trade_date < T 的最近收盘。",
        "- v1 复现部分只检验「外盘传导符号」，与 v1 手册的 57%/62% 结论同口径。",
        "",
    ]
    return "\n".join(lines)


async def run(since: dt.date, until: dt.date) -> Path:
    settings = load_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    generated = dt.date.today()
    model_version = f"rule-{generated:%Y%m%d}-01"
    try:
        async with session_factory() as session:
            edges = await _load_edges(session)
            seasonality = await _load_seasonality(session)
            daily = await _load_daily(session, since, until)
            ext_codes = sorted({edge.ext for items in edges.values() for edge in items})
            foreign = await _load_foreign(session, ext_codes, since, until)

        def build(q_mode: str) -> list[BacktestRecord]:
            out: list[BacktestRecord] = []
            for code in sorted(daily):
                out.extend(
                    _build_records(
                        code,
                        daily[code],
                        edges.get(code, []),
                        foreign,
                        seasonality,
                        since,
                        until,
                        q_mode,
                    )
                )
            return out

        all_records = build("pit")
        if not all_records:
            raise SystemExit("回测无样本：请先回补 daily_bar 与 foreign_daily")
        static_records = build("static")

        dev_records, holdout_records = split_holdout(all_records, HOLDOUT)
        dev = summarize(dev_records)
        holdout = summarize(holdout_records)
        wf = walk_forward(dev_records, TRAIN, VALIDATION, TEST)
        v1 = v1_diagnostic(all_records)
        by_code = per_variety(all_records)

        s_dev_records, s_holdout_records = split_holdout(static_records, HOLDOUT)
        comparison = {
            "pit": {"dev": dev, "holdout": holdout},
            "static": {
                "dev": summarize(s_dev_records),
                "holdout": summarize(s_holdout_records),
            },
        }

        failures = sorted(
            (r for r in dev_records if r.pred_dir != 0 and r.pred_dir != r.actual_dir),
            key=lambda r: abs(r.actual_change),
            reverse=True,
        )[:10]

        start = min(r.trade_date for r in all_records)
        end = max(r.trade_date for r in all_records)
        report = _render_report(
            generated=generated,
            start=start,
            end=end,
            all_records=all_records,
            dev=dev,
            holdout=holdout,
            wf=wf,
            v1=v1,
            by_code=by_code,
            failures=failures,
            comparison=comparison,
        )

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        md_path = OUTPUT_DIR / f"{generated:%Y%m%d}.md"
        md_path.write_text(report, encoding="utf-8")
        payload = {
            "generated_at": dt.datetime.now(dt.UTC).isoformat(),
            "model_version": model_version,
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "config": {
                "train": TRAIN,
                "validation": VALIDATION,
                "test": TEST,
                "holdout": HOLDOUT,
            },
            "dev": dev,
            "holdout": holdout,
            "leakage_comparison": comparison,
            "walk_forward": wf,
            "v1": v1,
            "per_variety": by_code,
            "records": len(all_records),
            "failures": records_to_json(failures),
        }
        json_path = md_path.with_suffix(".json")
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        async with session_factory() as session, session.begin():
            session.add(
                BacktestRun(
                    model_version=model_version,
                    start_date=start,
                    end_date=end,
                    config=payload["config"],
                    metrics={
                        "dev": dev,
                        "holdout": holdout,
                        "walk_forward": wf,
                        "v1": v1,
                        "per_variety": by_code,
                    },
                    report_md=report,
                )
            )

        print(f"report: {md_path}")
        print(f"json:   {json_path}")
        print(
            f"v1 全样本命中 {_pct(v1['hit_rate'])}，强外盘 {_pct(v1['strong_hit_rate'])}；"
            f"生产规则方向命中 {_pct(dev['direction_accuracy'])}，ECE {_num(dev['ece'])}"
        )
        return md_path
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Layer A 规则基线回测")
    today = dt.date.today()
    parser.add_argument(
        "--since", type=dt.date.fromisoformat, default=today.replace(year=today.year - 4)
    )
    parser.add_argument("--until", type=dt.date.fromisoformat, default=today)
    args = parser.parse_args()
    asyncio.run(run(args.since, args.until))


if __name__ == "__main__":
    main()
