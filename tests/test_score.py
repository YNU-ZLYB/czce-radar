"""M6 事后评分：σ 窗口无泄漏、命中口径、幂等性。"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jobs.predict.score import score_one, score_outcomes

TARGET = dt.date(2030, 3, 4)  # 周二


def _closes(base: float, days: int, end: dt.date) -> list[tuple[dt.date, float]]:
    return [(end - dt.timedelta(days=days - 1 - i), base * (1 + 0.001 * i)) for i in range(days)]


def test_score_one_direction_hit_and_window() -> None:
    # 25 个收盘：前 21 天平缓（σ 小），目标日 +2%（超出震荡带）
    closes = [*_closes(100.0, 24, TARGET - dt.timedelta(days=1)), (TARGET, 100.0 * 1.023 * 1.02)]
    result = score_one(
        direction=1, p_up=0.6, p_flat=0.3, p_down=0.1, closes=closes, target=TARGET
    )
    assert result is not None
    assert result["actual_change"] == pytest.approx(2.0, abs=0.01)
    assert result["actual_dir"] == 1
    assert result["hit_dir"] is True
    assert result["brier"] == pytest.approx((0.1 - 0) ** 2 + (0.3 - 0) ** 2 + (0.6 - 1) ** 2)


def test_score_one_flat_prediction_hit_inside_band() -> None:
    closes = [*_closes(100.0, 24, TARGET - dt.timedelta(days=1)), (TARGET, 100.0 * 1.023 * 1.001)]
    result = score_one(
        direction=0, p_up=0.2, p_flat=0.6, p_down=0.2, closes=closes, target=TARGET
    )
    assert result is not None
    assert result["actual_dir"] == 0
    assert result["hit_dir"] is True


def test_score_one_needs_target_close() -> None:
    closes = _closes(100.0, 23, TARGET - dt.timedelta(days=1))
    assert (
        score_one(direction=1, p_up=0.5, p_flat=0.3, p_down=0.2, closes=closes, target=TARGET)
        is None
    )


def test_score_one_insufficient_history() -> None:
    closes = [*_closes(100.0, 10, TARGET - dt.timedelta(days=1)), (TARGET, 101.0)]
    assert (
        score_one(direction=1, p_up=0.5, p_flat=0.3, p_down=0.2, closes=closes, target=TARGET)
        is None
    )


async def _seed_history(session: AsyncSession) -> None:
    await session.execute(
        text("DELETE FROM outcome WHERE prediction_id IN"
             " (SELECT id FROM prediction WHERE target_date = :d)"),
        {"d": TARGET},
    )
    await session.execute(text("DELETE FROM prediction WHERE target_date = :d"), {"d": TARGET})
    await session.execute(
        text("DELETE FROM daily_bar WHERE variety_code = 'MA' AND trade_date >= :d"),
        {"d": TARGET - dt.timedelta(days=40)},
    )
    # 24 个交易日历史（直线小涨），目标日收盘 +1.5%
    for day, close in _closes(100.0, 24, TARGET - dt.timedelta(days=1)):
        await session.execute(
            text(
                "INSERT INTO daily_bar (variety_code, trade_date, open, high, low, close,"
                " source) VALUES ('MA', :d, :c, :c, :c, :c, 'test')"
            ),
            {"d": day, "c": round(close, 4)},
        )
    await session.execute(
        text(
            "INSERT INTO daily_bar (variety_code, trade_date, open, high, low, close, source)"
            " VALUES ('MA', :d, 100, 103, 99, :c, 'test')"
        ),
        {"d": TARGET, "c": round(100.0 * 1.023 * 1.015, 4)},
    )
    await session.execute(
        text(
            "INSERT INTO prediction (variety_code, target_date, horizon, asof, direction,"
            " bucket, p_up, p_flat, p_down, exp_change, signal, confidence, contributor,"
            " model_version)"
            " VALUES ('MA', :d, '1d', :asof, 1, '偏多', 0.55, 0.30, 0.15, 0.8,"
            " 25, 0.55, CAST(:c AS jsonb), 'rule-20300304-01')"
        ),
        {
            "d": TARGET,
            "asof": dt.datetime(2030, 3, 4, 0, 30, tzinfo=dt.UTC),
            "c": json.dumps([]),
        },
    )
    await session.commit()


def test_score_outcomes_writes_and_idempotent(db_session: AsyncSession) -> None:
    async def _main() -> tuple[dict[str, int], dict[str, int], list[tuple]]:
        await _seed_history(db_session)
        first = await score_outcomes(db_session, today=TARGET + dt.timedelta(days=1))
        await db_session.commit()
        second = await score_outcomes(db_session, today=TARGET + dt.timedelta(days=1))
        await db_session.commit()
        rows = (
            await db_session.execute(
                text(
                    "SELECT o.actual_change, o.actual_dir, o.hit_dir, o.brier"
                    " FROM outcome o JOIN prediction p ON p.id = o.prediction_id"
                    " WHERE p.target_date = :d"
                ),
                {"d": TARGET},
            )
        ).all()
        await _cleanup(db_session)
        return first, second, [tuple(r) for r in rows]

    first, second, rows = asyncio.run(_main())
    assert first["scored"] == 1
    assert second["scored"] == 0  # 已评分行被 LEFT JOIN 过滤，不重复计算
    assert len(rows) == 1  # 幂等：一条预测最多一条 outcome
    change, actual_dir, hit, _brier = rows[0]
    assert float(change) == pytest.approx(1.5, abs=0.01)
    assert actual_dir == 1
    assert hit is True


async def _cleanup(session: AsyncSession) -> None:
    await session.execute(
        text("DELETE FROM outcome WHERE prediction_id IN"
             " (SELECT id FROM prediction WHERE target_date = :d)"),
        {"d": TARGET},
    )
    await session.execute(text("DELETE FROM prediction WHERE target_date = :d"), {"d": TARGET})
    await session.execute(
        text("DELETE FROM daily_bar WHERE variety_code = 'MA' AND trade_date >= :d"),
        {"d": TARGET - dt.timedelta(days=40)},
    )
    await session.commit()
