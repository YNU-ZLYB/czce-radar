"""仓单因子检验逻辑的单元测试（不连库）。"""

from __future__ import annotations

import datetime as dt

import pytest

from scripts.receipt_edge import _spearman, evaluate


def _d(offset: int) -> dt.date:
    return dt.date(2024, 1, 1) + dt.timedelta(days=offset)


def test_evaluate_counts_direction_and_skips_flat() -> None:
    # 仓单加速上升，价格线性下跌：仓单增→跌 应全部命中，且 IC 为正
    days = [_d(i) for i in range(14)]
    receipts = {d: 100.0 + 10.0 * i * i for i, d in enumerate(days)}
    closes = {d: 200.0 - 3.0 * i for i, d in enumerate(days)}
    res = evaluate(receipts, closes)
    # 前 5 天不够回看，最后 1 天没有次日收盘：14 天里 8 天可用
    assert res["n"] == 8
    assert res["hit"] == 1.0


def test_evaluate_respects_dead_zone() -> None:
    days = [_d(i) for i in range(8)]
    receipts = {d: 100.0 * (1.1**i) for i, d in enumerate(days)}
    closes = {d: 100.0 for d in days}
    res = evaluate(receipts, closes, dead_zone=0.3)
    assert res["n"] == 0
    assert res["hit"] is None


def test_evaluate_skips_zero_base_receipt() -> None:
    days = [_d(i) for i in range(8)]
    receipts = {d: 0.0 for d in days}
    closes = {d: 100.0 * (1.01**i) for i, d in enumerate(days)}
    res = evaluate(receipts, closes)
    assert res["n"] == 0


def test_spearman_perfect_monotonic() -> None:
    assert _spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert _spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
