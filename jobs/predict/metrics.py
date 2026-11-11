"""回测指标（纯函数）：方向命中、Balanced Acc、Brier、ECE、可靠性曲线。

回测记录由 ``scripts/backtest.py`` 按同一份 ``jobs.predict.rules.score`` 生成，
指标函数不做 I/O，保证可测试、可复现。
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

_UP = 2
_FLAT = 1
_DOWN = 0


@dataclass(frozen=True, slots=True)
class BacktestRecord:
    variety_code: str
    trade_date: dt.date
    pred_dir: int
    pred_bucket: str
    p_up: float
    p_flat: float
    p_down: float
    confidence: float
    actual_change: float
    actual_dir: int
    actual_bucket: str
    transmission_sign: int
    max_abs_foreign: float


def _pred_class(pred_dir: int) -> int:
    return _UP if pred_dir > 0 else _DOWN if pred_dir < 0 else _FLAT


def _actual_class(actual_dir: int) -> int:
    return _UP if actual_dir > 0 else _DOWN if actual_dir < 0 else _FLAT


def _probs(record: BacktestRecord) -> tuple[float, float, float]:
    """按 (down, flat, up) 顺序返回概率。"""
    return record.p_down, record.p_flat, record.p_up


def _r(value: float, nd: int = 4) -> float:
    return round(value, nd)


def reliability_curve(
    records: Sequence[BacktestRecord], bins: int = 10
) -> list[dict[str, float | int]]:
    """按预测类别置信度分桶，返回非空桶的 (平均置信度, 实际命中率)。"""
    counts = [0] * bins
    conf_sum = [0.0] * bins
    hit_sum = [0] * bins
    for r in records:
        idx = min(bins - 1, max(0, int(r.confidence * bins)))
        counts[idx] += 1
        conf_sum[idx] += r.confidence
        hit_sum[idx] += int(_pred_class(r.pred_dir) == _actual_class(r.actual_dir))
    curve: list[dict[str, float | int]] = []
    for i in range(bins):
        if counts[i] == 0:
            continue
        curve.append(
            {
                "lo": round(i / bins, 2),
                "hi": round((i + 1) / bins, 2),
                "n": counts[i],
                "avg_confidence": _r(conf_sum[i] / counts[i]),
                "accuracy": _r(hit_sum[i] / counts[i]),
            }
        )
    return curve


def expected_calibration_error(
    records: Sequence[BacktestRecord], bins: int = 10
) -> float:
    if not records:
        return 0.0
    curve = reliability_curve(records, bins)
    total = len(records)
    return _r(
        sum(
            (int(item["n"]) / total)
            * abs(float(item["accuracy"]) - float(item["avg_confidence"]))
            for item in curve
        )
    )


def balanced_accuracy(records: Sequence[BacktestRecord]) -> float:
    support: defaultdict[int, int] = defaultdict(int)
    correct: defaultdict[int, int] = defaultdict(int)
    for r in records:
        actual = _actual_class(r.actual_dir)
        support[actual] += 1
        if _pred_class(r.pred_dir) == actual:
            correct[actual] += 1
    recalls = [correct[c] / support[c] for c in support if support[c] > 0]
    if not recalls:
        return 0.0
    return _r(sum(recalls) / len(recalls))


def brier_score(records: Sequence[BacktestRecord]) -> float:
    """三分类 Brier：Σ(p - onehot)²，范围 0~2（如实报告，不作方向门槛）。"""
    if not records:
        return 0.0
    total = 0.0
    for r in records:
        target = _actual_class(r.actual_dir)
        for cls, prob in enumerate(_probs(r)):
            total += (prob - (1.0 if cls == target else 0.0)) ** 2
    return _r(total / len(records), 6)


def brier_direction(records: Sequence[BacktestRecord]) -> float:
    """方向 Brier：只在实际非震荡日，用条件上涨概率 p_up/(p_up+p_down) 评分。"""
    usable = [r for r in records if r.actual_dir != 0]
    if not usable:
        return 0.0
    total = 0.0
    for r in usable:
        denom = r.p_up + r.p_down
        p = r.p_up / denom if denom > 0 else 0.5
        y = 1.0 if r.actual_dir > 0 else 0.0
        total += (p - y) ** 2
    return _r(total / len(usable), 6)


def summarize(records: Sequence[BacktestRecord]) -> dict[str, Any]:
    n = len(records)
    if n == 0:
        return {
            "n": 0,
            "coverage": 0.0,
            "direction_accuracy": None,
            "direction_n": 0,
            "balanced_accuracy": 0.0,
            "brier": 0.0,
            "brier_direction": 0.0,
            "ece": 0.0,
            "reliability": [],
        }
    # 方向命中 = 排除震荡预测后的二分类命中（与 v1 手册同口径）
    non_flat = [r for r in records if r.pred_dir != 0 and r.actual_change != 0]
    hits = sum(
        1
        for r in non_flat
        if r.pred_dir == (1 if r.actual_change > 0 else -1)
    )
    return {
        "n": n,
        "coverage": _r(sum(1 for r in records if r.pred_dir != 0) / n),
        "direction_accuracy": _r(hits / len(non_flat)) if non_flat else None,
        "direction_n": len(non_flat),
        "balanced_accuracy": balanced_accuracy(records),
        "brier": brier_score(records),
        "brier_direction": brier_direction(records),
        "ece": expected_calibration_error(records),
        "reliability": reliability_curve(records),
    }


def per_variety(records: Sequence[BacktestRecord]) -> dict[str, dict[str, Any]]:
    by_code: dict[str, list[BacktestRecord]] = defaultdict(list)
    for r in records:
        by_code[r.variety_code].append(r)
    return {code: summarize(items) for code, items in sorted(by_code.items())}


def v1_diagnostic(records: Sequence[BacktestRecord]) -> dict[str, Any]:
    """v1 结论复现：纯传导符号方向命中（不使用 q / 季节性 / 概率）。

    - 全样本：sign(Σ change×prior) vs 实际涨跌符号
    - 强外盘子集：当日关联外盘 |change| 最大值 ≥ 0.8%
    """
    usable = [r for r in records if r.transmission_sign != 0 and r.actual_change != 0]
    hits = [r for r in usable if r.transmission_sign == (1 if r.actual_change > 0 else -1)]
    strong = [
        r for r in usable if r.max_abs_foreign >= 0.8
    ]
    strong_hits = [
        r for r in strong if r.transmission_sign == (1 if r.actual_change > 0 else -1)
    ]

    by_code: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[BacktestRecord]] = defaultdict(list)
    for r in usable:
        grouped[r.variety_code].append(r)
    for code, items in sorted(grouped.items()):
        code_hits = sum(
            1 for r in items if r.transmission_sign == (1 if r.actual_change > 0 else -1)
        )
        by_code[code] = {"n": len(items), "hit_rate": _r(code_hits / len(items))}

    return {
        "n": len(usable),
        "hit_rate": _r(len(hits) / len(usable)) if usable else None,
        "strong_n": len(strong),
        "strong_hit_rate": _r(len(strong_hits) / len(strong)) if strong else None,
        "by_variety": by_code,
    }


def walk_forward(
    records: Sequence[BacktestRecord],
    train: int = 500,
    validation: int = 20,
    test: int = 20,
) -> dict[str, Any]:
    """Walk-forward 切分（规则基线零训练，仅按协议评估样本外窗口）。

    每个品种独立按时间排序，跳过 train+validation 后每 test 条为一折；
    汇总所有品种的测试窗口记录得到 aggregate。
    """
    by_code: dict[str, list[BacktestRecord]] = defaultdict(list)
    for r in records:
        by_code[r.variety_code].append(r)

    folds: list[dict[str, Any]] = []
    test_records: list[BacktestRecord] = []
    start = train + validation
    for code, items in sorted(by_code.items()):
        ordered = sorted(items, key=lambda r: r.trade_date)
        for offset in range(start, len(ordered), test):
            chunk = ordered[offset : offset + test]
            if not chunk:
                continue
            folds.append(
                {
                    "variety_code": code,
                    "start": chunk[0].trade_date.isoformat(),
                    "end": chunk[-1].trade_date.isoformat(),
                    "n": len(chunk),
                    "direction_accuracy": summarize(chunk)["direction_accuracy"],
                }
            )
            test_records.extend(chunk)

    fold_hits = [
        float(f["direction_accuracy"])
        for f in folds
        if f["direction_accuracy"] is not None
    ]
    return {
        "train": train,
        "validation": validation,
        "test": test,
        "folds": len(folds),
        "fold_accuracy_mean": _r(sum(fold_hits) / len(fold_hits)) if fold_hits else None,
        "aggregate": summarize(test_records),
    }


def split_holdout(
    records: Sequence[BacktestRecord], holdout_days: int = 60
) -> tuple[list[BacktestRecord], list[BacktestRecord]]:
    """按全局交易日切出永久 holdout（最近 N 个交易日），返回 (开发集, holdout)。"""
    days = sorted({r.trade_date for r in records})
    if len(days) <= holdout_days:
        return [], list(records)
    boundary = days[-holdout_days]
    dev = [r for r in records if r.trade_date < boundary]
    holdout = [r for r in records if r.trade_date >= boundary]
    return dev, holdout


def records_to_json(records: Sequence[BacktestRecord]) -> list[dict[str, Any]]:
    return [asdict(r) | {"trade_date": r.trade_date.isoformat()} for r in records]
