"""动量因子验证逻辑的单元测试（不连库）。"""

from __future__ import annotations

import datetime as dt

from scripts.momentum_edge import _verdict, evaluate_momentum


def _series(closes: list[float]) -> list[tuple[dt.date, float]]:
    return [(dt.date(2024, 1, 1) + dt.timedelta(days=i), c) for i, c in enumerate(closes)]


def test_accelerating_uptrend_has_positive_ic() -> None:
    closes = [100.0 * (1.01 ** (i * i / 50)) for i in range(400)]
    r = evaluate_momentum(_series(closes), 5)
    assert r["n"] > 200
    assert r["ic"] is not None and r["ic"] > 0.3
    assert r["hit"] is not None and r["hit"] > 0.9


def test_flat_series_yields_no_samples() -> None:
    r = evaluate_momentum(_series([100.0] * 300), 5)
    assert r["n"] == 0
    assert r["ic"] is None
    assert _verdict(r) == "样本不足"


def test_verdict_requires_both_halves_consistent() -> None:
    assert _verdict({"n": 500, "ic": 0.08, "ic_first": 0.05, "ic_second": 0.09}) == "稳定动量"
    assert _verdict({"n": 500, "ic": -0.08, "ic_first": -0.05, "ic_second": -0.09}) == "稳定反转"
    assert (
        _verdict({"n": 500, "ic": 0.02, "ic_first": 0.06, "ic_second": -0.03})
        == "前后半段变号，不稳"
    )
    assert _verdict({"n": 500, "ic": 0.01, "ic_first": 0.01, "ic_second": 0.01}) == "无 edge"
