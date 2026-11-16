"""换月诊断逻辑的单元测试（不连库）。"""

from __future__ import annotations

import datetime as dt

from scripts.roll_check import detect_rolls, expand_around


def _d(offset: int) -> dt.date:
    return dt.date(2024, 1, 1) + dt.timedelta(days=offset)


def test_detects_jump_with_open_interest_collapse() -> None:
    bars = [
        (_d(0), 100.0, 100_000),
        (_d(1), 106.0, 30_000),  # 涨 6% 且持仓掉 70% → 换月
        (_d(2), 106.0, 32_000),
    ]
    assert detect_rolls(bars) == {_d(1)}


def test_ignores_jump_without_open_interest_collapse() -> None:
    # 真涨跌：持仓稳定，不该被当成换月
    bars = [
        (_d(0), 100.0, 100_000),
        (_d(1), 108.0, 101_000),
    ]
    assert detect_rolls(bars) == set()


def test_ignores_small_move_even_if_open_interest_drops() -> None:
    bars = [
        (_d(0), 100.0, 100_000),
        (_d(1), 101.0, 20_000),  # 只涨 1%，不到阈值
    ]
    assert detect_rolls(bars) == set()


def test_missing_open_interest_is_not_flagged() -> None:
    bars = [
        (_d(0), 100.0, None),
        (_d(1), 110.0, None),
    ]
    assert detect_rolls(bars) == set()


def test_expand_around_includes_neighbours_and_clips_edges() -> None:
    days = [_d(i) for i in range(5)]
    expanded = expand_around(days, {_d(0), _d(2)}, span=1)
    assert expanded == {_d(0), _d(1), _d(2), _d(3)}
