"""M4 数据集构建测试：特征名表、向量化一致性、防泄漏、display_only 硬断言。"""

from __future__ import annotations

import datetime as dt
import math

import pytest

from jobs.train.dataset import (
    DISPLAY_ONLY_KEYS,
    LABEL_DOWN,
    LABEL_FLAT,
    LABEL_UP,
    Edge,
    ForeignSeries,
    base_feature_names,
    build_samples_for_variety,
    feature_dict,
    pooled_feature_names,
    to_matrix,
)

EXTS = ["NG", "OIL"]


def _bars(n: int, start: dt.date = dt.date(2026, 1, 1)) -> list[tuple[dt.date, float]]:
    """合成日线：缓慢上行 + 小幅摆动（工作日近似，连续自然日即可）。"""
    out = []
    for i in range(n):
        close = 100.0 + i * 0.1 + (1.5 if i % 7 in (3, 4) else -1.0 if i % 7 == 5 else 0.0)
        out.append((start + dt.timedelta(days=i), round(close, 4)))
    return out


def _foreign(n: int, start: dt.date = dt.date(2026, 1, 1)) -> ForeignSeries:
    dates = [start + dt.timedelta(days=i) for i in range(n)]
    closes = [100.0 + (0.5 if i % 5 == 0 else -0.2) for i in range(n)]
    chg = [None] + [round((closes[i] / closes[i - 1] - 1) * 100, 4) for i in range(1, n)]
    return ForeignSeries(dates=dates, change_pct=chg, closes=closes)


def test_display_only_never_in_feature_names() -> None:
    names = base_feature_names(EXTS) + pooled_feature_names(EXTS, ["MA", "TA"])
    for key in DISPLAY_ONLY_KEYS:
        assert all(key not in n for n in names)


def test_feature_dict_mapping_and_transmission() -> None:
    feats = feature_dict(
        price={"ret_1d": 0.5, "ret_5d": 1.2, "vol_20d": 2.0, "pos_20d": 0.3},
        foreign={"OIL": {"chg_1d": -1.0, "chg_3d": -2.0}, "NG": {"chg_1d": 0.4}},
        edges=[Edge(ext="OIL", prior=0.45, q=0.58), Edge(ext="NG", prior=0.3, q=0.58)],
        industry={"receipt_chg_5d": None, "inventory_pct": None, "basis_pct": None},
        season_bias=3.0,
        season_conf=0.5,
        month=11,
        news_sentiment_lag1=None,
        news_count_lag1=None,
        is_event_day=False,
        ext_codes=EXTS,
    )
    assert feats["f_OIL_chg_1d"] == -1.0
    assert feats["f_NG_chg_3d"] is None
    assert feats["trans_prior"] == pytest.approx(-1.0 * 0.45 + 0.4 * 0.3)
    assert feats["trans_q"] == pytest.approx((-1.0 * 0.45 + 0.4 * 0.3) * 0.58)
    assert feats["month"] == 11.0
    assert feats["n_foreign_missing"] == 0.0


def test_to_matrix_nan_and_onehot() -> None:
    s = build_samples_for_variety(
        "MA", _bars(40), [Edge(ext="OIL", prior=0.45, q=0.58)], {"OIL": _foreign(40)},
        {}, {}, None, EXTS, dt.date(2026, 1, 1), dt.date(2026, 3, 1),
    )
    assert s, "合成数据应产出样本"
    names = pooled_feature_names(["OIL"], ["MA", "TA"])
    x, y = to_matrix(s, names)
    assert len(x) == len(y) == len(s)
    ma_idx = names.index("var_MA")
    ta_idx = names.index("var_TA")
    assert all(row[ma_idx] == 1.0 and row[ta_idx] == 0.0 for row in x)
    news_idx = names.index("news_sentiment_lag1")
    assert all(math.isnan(row[news_idx]) for row in x)


def test_no_lookahead_future_bars_do_not_change_sample() -> None:
    """改动 T 及之后的收盘，T 的特征与标签（用 T-1 收盘）绝不变。"""
    bars = _bars(60)
    foreign = {"OIL": _foreign(60)}
    edges = [Edge(ext="OIL", prior=0.45, q=0.58)]
    target = bars[40][0]
    before = build_samples_for_variety(
        "MA", bars, edges, foreign, {}, {}, None, EXTS, target, target
    )
    mutated = bars[:40] + [(d, c * 1.5) for d, c in bars[40:]]
    after = build_samples_for_variety(
        "MA", mutated, edges, foreign, {}, {}, None, EXTS, target, target
    )
    assert len(before) == len(after) == 1
    assert before[0].features == after[0].features
    assert before[0].rule_p == after[0].rule_p


def test_labels_respect_sigma_bands() -> None:
    """恒定σ下：大涨→UP、大跌→DOWN、小动→FLAT。"""
    start = dt.date(2026, 1, 1)
    base = [(start + dt.timedelta(days=i), 100.0) for i in range(30)]
    edges: list[Edge] = []
    foreign: dict[str, ForeignSeries] = {}

    def label_of(last_close: float) -> int:
        bars = [*base[:-1], (base[-1][0], 100.0), (base[-1][0] + dt.timedelta(days=1), last_close)]
        s = build_samples_for_variety(
            "SR", bars, edges, foreign, {}, {}, None, [], bars[-1][0], bars[-1][0]
        )
        assert len(s) == 1
        return s[0].label

    # σ≈0 → 震荡带 0.5%
    assert label_of(100.2) == LABEL_FLAT
    assert label_of(101.0) == LABEL_UP
    assert label_of(99.0) == LABEL_DOWN
