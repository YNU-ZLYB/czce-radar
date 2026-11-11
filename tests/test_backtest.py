"""回测指标纯函数测试：命中率、Balanced Acc、Brier、ECE、话分与 holdout。"""

from __future__ import annotations

import datetime as dt

import pytest

from jobs.predict.metrics import (
    BacktestRecord,
    brier_direction,
    brier_score,
    expected_calibration_error,
    reliability_curve,
    split_holdout,
    summarize,
    v1_diagnostic,
    walk_forward,
)


def _rec(
    *,
    pred_dir: int,
    actual_dir: int,
    p_up: float = 0.2,
    p_flat: float = 0.3,
    p_down: float = 0.5,
    transmission: int = 0,
    max_abs: float = 0.0,
    day: int = 1,
    code: str = "MA",
    actual_change: float | None = None,
    trade_date: dt.date | None = None,
) -> BacktestRecord:
    bucket = {1: "偏多", 0: "震荡", -1: "偏空"}[pred_dir]
    actual_bucket = {1: "偏多", 0: "震荡", -1: "偏空"}[actual_dir]
    change = float(actual_dir) if actual_change is None else actual_change
    return BacktestRecord(
        variety_code=code,
        trade_date=trade_date
        or (dt.date(2026, 1, day) if day <= 28 else dt.date(2026, 2, day - 28)),
        pred_dir=pred_dir,
        pred_bucket=bucket,
        p_up=p_up,
        p_flat=p_flat,
        p_down=p_down,
        confidence=max(p_up, p_flat, p_down),
        actual_change=change,
        actual_dir=actual_dir,
        actual_bucket=actual_bucket,
        transmission_sign=transmission,
        max_abs_foreign=max_abs,
    )


def test_direction_accuracy_excludes_flat_predictions() -> None:
    records = [
        _rec(pred_dir=1, actual_dir=1),
        _rec(pred_dir=-1, actual_dir=-1),
        _rec(pred_dir=1, actual_dir=-1),
        _rec(pred_dir=0, actual_dir=1),  # 震荡预测不计入方向命中分母
    ]
    summary = summarize(records)
    assert summary["n"] == 4
    assert summary["direction_n"] == 3
    assert summary["direction_accuracy"] == pytest.approx(2 / 3, abs=1e-4)
    assert summary["coverage"] == pytest.approx(0.75)


def test_balanced_accuracy_and_brier_exact() -> None:
    records = [
        _rec(pred_dir=1, actual_dir=1, p_up=0.8, p_flat=0.1, p_down=0.1),
        _rec(pred_dir=-1, actual_dir=-1, p_up=0.1, p_flat=0.1, p_down=0.8),
        _rec(pred_dir=0, actual_dir=0, p_up=0.1, p_flat=0.8, p_down=0.1),
    ]
    summary = summarize(records)
    assert summary["balanced_accuracy"] == pytest.approx(1.0)
    # 每条 Brier = (1-0.8)^2 + (0-0.1)^2 + (0-0.1)^2 = 0.06
    assert summary["brier"] == pytest.approx(0.06, abs=1e-6)
    assert brier_score(records) == pytest.approx(0.06, abs=1e-6)


def test_brier_direction_uses_conditional_probability() -> None:
    record = _rec(
        pred_dir=1,
        actual_dir=1,
        p_up=0.5,
        p_flat=0.0,
        p_down=0.5,
        actual_change=1.0,
    )
    # 条件上涨概率 = 0.5，实际上涨 → (0.5-1)^2
    assert brier_direction([record]) == pytest.approx(0.25)
    flat_actual = _rec(pred_dir=1, actual_dir=0, p_up=0.9, p_down=0.05)
    assert brier_direction([flat_actual]) == 0.0  # 实际震荡日不参与方向 Brier


def test_reliability_curve_and_ece() -> None:
    records = [
        _rec(pred_dir=1, actual_dir=1, p_up=0.8, p_flat=0.1, p_down=0.1),
        _rec(pred_dir=1, actual_dir=1, p_up=0.8, p_flat=0.1, p_down=0.1),
        _rec(pred_dir=1, actual_dir=-1, p_up=0.8, p_flat=0.1, p_down=0.1),
    ]
    curve = reliability_curve(records)
    assert len(curve) == 1
    assert curve[0]["n"] == 3
    assert curve[0]["avg_confidence"] == pytest.approx(0.8)
    assert curve[0]["accuracy"] == pytest.approx(2 / 3, abs=1e-4)
    assert expected_calibration_error(records) == pytest.approx(
        abs(2 / 3 - 0.8), abs=1e-4
    )


def test_v1_diagnostic_overall_and_strong_subset() -> None:
    records = [
        _rec(pred_dir=1, actual_dir=1, transmission=1, actual_change=1.0, max_abs=1.2),
        _rec(pred_dir=1, actual_dir=1, transmission=1, actual_change=-1.0, max_abs=1.0),
        _rec(pred_dir=1, actual_dir=1, transmission=-1, actual_change=-0.5, max_abs=0.3),
        _rec(pred_dir=1, actual_dir=1, transmission=-1, actual_change=0.5, max_abs=0.2),
    ]
    diag = v1_diagnostic(records)
    assert diag["n"] == 4
    assert diag["hit_rate"] == pytest.approx(0.5)
    assert diag["strong_n"] == 2
    assert diag["strong_hit_rate"] == pytest.approx(0.5)
    assert diag["by_variety"]["MA"]["n"] == 4


def test_split_holdout_uses_last_60_trading_days() -> None:
    base = dt.date(2026, 1, 1)
    records = [
        _rec(
            pred_dir=1,
            actual_dir=1,
            trade_date=base + dt.timedelta(days=i),
        )
        for i in range(100)
    ]
    dev, holdout = split_holdout(records, 60)
    assert len(holdout) == 60
    assert len(dev) == 40
    assert min(r.trade_date for r in holdout) > max(r.trade_date for r in dev)


def test_walk_forward_folds_use_only_test_windows() -> None:
    base = dt.date(2026, 1, 1)
    records = [
        _rec(
            pred_dir=1,
            actual_dir=1,
            trade_date=base + dt.timedelta(days=i),
        )
        for i in range(12)
    ]
    result = walk_forward(records, train=4, validation=2, test=2)
    # 从第 6 条开始，每 2 条一折：6-7, 8-9, 10-11
    assert result["folds"] == 3
    assert result["aggregate"]["n"] == 6
    assert result["aggregate"]["direction_accuracy"] == pytest.approx(1.0)
