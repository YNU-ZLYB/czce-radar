"""覆盖率归因的纯函数测试（不连库）。"""

from __future__ import annotations

import datetime as dt

from scripts.coverage_audit import _aggregate, _classify_day, _fmt, _render, _verdict


def _row(**kw):
    base = {
        "code": "MA",
        "day": dt.date(2025, 1, 2),
        "exp_change": 0.0,
        "flat_band": 0.5,
        "eff_weight": 0.0,
        "n_edges": 0,
        "n_eff_edges": 0,
        "direction": 0,
        "actual_change": 0.3,
        "actual_dir": 1,
    }
    base.update(kw)
    return base


def test_classify_four_buckets() -> None:
    assert _classify_day(_row(n_eff_edges=0)) == "A"
    assert _classify_day(_row(n_eff_edges=1, exp_change=0.4)) == "B"
    assert _classify_day(_row(n_eff_edges=1, exp_change=1.2, direction=1, actual_dir=1)) == "C"
    assert _classify_day(_row(n_eff_edges=1, exp_change=-1.2, direction=-1, actual_dir=1)) == "D"


def test_verdict_priority() -> None:
    # 一半以上天数无输入 → 缺输入，优先于其他判断
    miss = {"n": 100, "A": 60, "B": 30, "C": 10, "D": 0, "abs_sum": 20.0, "band_sum": 50.0}
    assert _verdict(miss) == "缺输入"

    # 有输入的日子里 80% 被切 → 阈值偏严
    cut = {"n": 100, "A": 0, "B": 85, "C": 15, "D": 0, "abs_sum": 20.0, "band_sum": 50.0}
    assert _verdict(cut) == "阈值偏严"

    # 给方向但命中率 < 52% → 不可预测
    bad = {"n": 100, "A": 0, "B": 40, "C": 28, "D": 32, "abs_sum": 60.0, "band_sum": 50.0}
    assert _verdict(bad) == "不可预测"

    good = {"n": 100, "A": 0, "B": 40, "C": 40, "D": 20, "abs_sum": 60.0, "band_sum": 50.0}
    assert _verdict(good) == "正常"


def test_fmt_zero_division_safe() -> None:
    stats = {"n": 0, "A": 0, "B": 0, "C": 0, "D": 0, "abs_sum": 0.0, "band_sum": 0.0}
    out = _fmt(stats)
    assert out["acc"] is None
    assert out["verdict"] == "无样本"


def test_aggregate_and_render() -> None:
    rows = [
        _row(code="MA", n_eff_edges=1, exp_change=1.2, direction=1, actual_dir=1),
        _row(code="MA", n_eff_edges=1, exp_change=0.1),
        _row(code="AP", n_eff_edges=0),
        _row(code="AP", n_eff_edges=0),
    ]
    by_code = _aggregate(rows)
    assert by_code["MA"]["C"] == 1 and by_code["MA"]["B"] == 1
    assert by_code["AP"]["A"] == 2
    md = _render(rows, dt.date(2025, 1, 1), dt.date(2025, 1, 10))
    assert "覆盖率归因" in md
    assert "缺输入" in md and "AP" in md
