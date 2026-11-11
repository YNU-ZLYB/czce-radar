"""M7：scorecard 快照持久化与周报对比测试。"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jobs.predict.scorecard import compute_scorecard, save_scorecard_snapshot
from jobs.report.weekly import generate_weekly_report

NOW = dt.datetime.now(dt.UTC)
TARGET = NOW.date()


async def _seed_outcome(session: AsyncSession, pred_id: int, actual_change: float) -> None:
    await session.execute(
        text(
            "INSERT INTO prediction (id, variety_code, target_date, horizon, asof, direction,"
            " bucket, p_up, p_flat, p_down, signal, confidence, contributor, model_version)"
            " VALUES (:id, 'MA', :d, '1d', :asof, :dir, '偏多', 0.6, 0.3, 0.1, 30, 0.6,"
            " '[]'::jsonb, 'rule-20310506-01')"
        ),
        {
            "id": pred_id,
            "d": TARGET,
            "asof": NOW,
            "dir": 1 if actual_change > 0 else -1,
        },
    )
    await session.execute(
        text(
            "INSERT INTO outcome (prediction_id, actual_change, actual_dir, actual_bucket,"
            " hit_dir, brier) VALUES (:id, :ch, :dir, '偏多', :hit, 0.2)"
        ),
        {
            "id": pred_id,
            "ch": actual_change,
            "dir": 1 if actual_change > 0 else -1,
            "hit": (actual_change > 0),
        },
    )


async def _cleanup(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM scorecard_snapshot"))
    await session.execute(
        text("DELETE FROM outcome WHERE prediction_id IN (900911, 900912, 900913)")
    )
    await session.execute(
        text("DELETE FROM prediction WHERE id IN (900911, 900912, 900913)")
    )
    await session.commit()


async def test_save_snapshot_and_idempotent(db_session: AsyncSession) -> None:
    await _cleanup(db_session)
    await _seed_outcome(db_session, 900911, 1.0)
    await db_session.commit()

    card = await compute_scorecard(db_session, days=400)
    result = await save_scorecard_snapshot(db_session, card, run_date=TARGET)
    await db_session.commit()
    assert result["run_date"] == TARGET.isoformat()
    assert result["n"] >= 1

    # 幂等：同 run_date + model_version 再写一次，行数不变
    rows_before = (
        await db_session.execute(
            text("SELECT count(*) FROM scorecard_snapshot")
        )
    ).scalar()
    await save_scorecard_snapshot(db_session, card, run_date=TARGET)
    await db_session.commit()
    rows_after = (
        await db_session.execute(
            text("SELECT count(*) FROM scorecard_snapshot")
        )
    ).scalar()
    assert rows_before == rows_after

    # top_misses 落库
    row = (
        await db_session.execute(
            text("SELECT top_misses FROM scorecard_snapshot WHERE run_date = :d"),
            {"d": TARGET},
        )
    ).first()
    assert row is not None
    misses = json.loads(row.top_misses) if isinstance(row.top_misses, str) else row.top_misses
    assert isinstance(misses, list)
    await _cleanup(db_session)


async def test_weekly_report_no_data_hold(db_session: AsyncSession) -> None:
    await _cleanup(db_session)
    report = await generate_weekly_report(db_session)
    assert report["status"] == "no_data"
    assert report["recommendation"] == "hold"


async def test_weekly_report_compares_two_weeks(db_session: AsyncSession) -> None:
    await _cleanup(db_session)
    # 本周一条命中（direction=1, actual_change>0 → hit）
    await _seed_outcome(db_session, 900911, 2.0)
    await db_session.commit()
    card = await compute_scorecard(db_session, days=400)
    await save_scorecard_snapshot(db_session, card, run_date=TARGET)
    await db_session.commit()

    # 上周一条失误（direction=1, actual_change<0 → miss）
    last_week = TARGET - dt.timedelta(days=8)
    await db_session.execute(
        text(
            "INSERT INTO prediction (id, variety_code, target_date, horizon, asof, direction,"
            " bucket, p_up, p_flat, p_down, signal, confidence, contributor, model_version)"
            " VALUES (900913, 'MA', :d, '1d', :asof, 1, '偏多', 0.6, 0.3, 0.1, 30, 0.6,"
            " '[]'::jsonb, 'rule-20310506-01')"
        ),
        {"d": last_week, "asof": NOW},
    )
    await db_session.execute(
        text(
            "INSERT INTO outcome (prediction_id, actual_change, actual_dir, actual_bucket,"
            " hit_dir, brier) VALUES (900913, -1.0, -1, '偏空', false, 0.3)"
        )
    )
    await db_session.commit()
    card_last = await compute_scorecard(db_session, days=400)
    await save_scorecard_snapshot(db_session, card_last, run_date=last_week)
    await db_session.commit()

    report = await generate_weekly_report(db_session)
    assert report["this_week"] is not None
    assert report["last_week"] is not None
    assert report["recommendation"] in ("hold", "retrain")
    assert "delta" in report
    await _cleanup(db_session)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
