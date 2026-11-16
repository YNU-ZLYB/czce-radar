"""置信度分层逻辑的单元测试（不连库）。"""

from __future__ import annotations

import datetime as dt

from scripts.confidence_tiers import tier_stats, top_share_stats

from jobs.predict.metrics import BacktestRecord


def _rec(conf: float, pred: int, actual: float, day: int = 0) -> BacktestRecord:
    return BacktestRecord(
        variety_code="SR",
        trade_date=dt.date(2024, 1, 1) + dt.timedelta(days=day),
        pred_dir=pred,
        pred_bucket="up" if pred > 0 else "down",
        p_up=0.5,
        p_flat=0.2,
        p_down=0.3,
        confidence=conf,
        actual_change=actual,
        actual_dir=1 if actual > 0 else -1,
        actual_bucket="up" if actual > 0 else "down",
        transmission_sign=1,
        max_abs_foreign=1.0,
    )


def test_tier_stats_orders_high_confidence_first() -> None:
    records = [
        _rec(0.9, 1, 1.0, 0),
        _rec(0.8, 1, 1.0, 1),
        _rec(0.2, 1, -1.0, 2),
        _rec(0.1, 1, -1.0, 3),
    ]
    tiers = tier_stats(records, tiers=2)
    assert [t["tier"] for t in tiers] == ["T1", "T2"]
    assert tiers[0]["hit"] == 1.0  # 高置信度两笔全中
    assert tiers[1]["hit"] == 0.0  # 低置信度两笔全错


def test_tier_stats_ignores_flat_predictions() -> None:
    records = [_rec(0.9, 0, 1.0, 0), _rec(0.8, 1, 1.0, 1), _rec(0.2, 1, -1.0, 2)]
    tiers = tier_stats(records, tiers=2)
    assert sum(t["n"] for t in tiers) == 2


def test_tier_stats_empty_without_directional() -> None:
    assert tier_stats([_rec(0.9, 0, 1.0)]) == []


def test_top_share_keeps_highest_confidence() -> None:
    records = [_rec(0.9, 1, 1.0, i) for i in range(20)]
    records += [_rec(0.8, 1, 1.0, 20 + i) for i in range(20)]
    records += [_rec(0.2, 1, -1.0, 40 + i) for i in range(360)]
    rows = top_share_stats(records, tops=(10,))
    assert rows and rows[0]["keep_pct"] == 10
    assert rows[0]["n"] == 40
    assert rows[0]["hit"] == 1.0  # 只留最准的 10%
    assert rows[0]["conf_min"] == 0.8


def test_top_share_drops_tiers_below_sample_floor() -> None:
    records = [_rec(0.9, 1, 1.0, 0), _rec(0.2, 1, -1.0, 1)]
    assert top_share_stats(records, tops=(10,)) == []
