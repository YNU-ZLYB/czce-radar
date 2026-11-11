"""FeatureSnapshot 构建测试：数值正确性、防泄漏、确定性、幂等落库。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import text

from jobs.features.build import build_snapshot, persist_snapshot
from jobs.ingest.foreign import aggregate_foreign_day
from jobs.ingest.quotes import ensure_partitions
from jobs.knowledge.importer import import_knowledge
from jobs.knowledge.models import load_knowledge_dir

REPO = Path(__file__).resolve().parents[1]
ASOF = dt.datetime(2026, 11, 10, 0, 30, tzinfo=dt.UTC)  # 08:30 CST
KNOWLEDGE = REPO / "data" / "knowledge"


async def _seed(db_session) -> None:  # type: ignore[no-untyped-def]
    """MA 最小数据集：日线 22 天（单调 +1%）、OIL 外盘 4 天、盘面 spark 两点。"""
    s = db_session
    for table in ("daily_bar", "foreign_daily", "quote_1m", "feature_snapshot"):
        await s.execute(text(f"DELETE FROM {table} WHERE 1=1"))
    await ensure_partitions(s, [dt.date(2026, 11, 9)])
    # 日线：时间升序单调 +1%（最新 2026-11-09 close = 100 * 1.01**21）
    for i in range(22):
        day = dt.date(2026, 11, 9) - dt.timedelta(days=i)
        close = 100 * (1.01 ** (21 - i))
        volume = 240000 if i == 0 else 120000
        await s.execute(
            text(
                "INSERT INTO daily_bar (variety_code, trade_date, open, high, low, close,"
                " volume, source) VALUES ('MA', :d, :c, :c, :c, :c, :v, 'test')"
                " ON CONFLICT DO NOTHING"
            ),
            {"d": day, "c": close, "v": volume},
        )
    # 外盘 OIL：11-06 ~ 11-09；stored change_pct 直接给 -1.5
    for j, (day, close) in enumerate(
        [(dt.date(2026, 11, d), c) for d, c in [(6, 78.0), (7, 78.9), (8, 79.0), (9, 80.0)]]
    ):
        await s.execute(
            text(
                "INSERT INTO foreign_daily (ext_code, trade_date, close, change_pct, source)"
                " VALUES ('OIL', :d, :c, :p, 'test') ON CONFLICT DO NOTHING"
            ),
            {"d": day, "c": close, "p": -1.5 if j == 3 else 0.5},
        )
    # quote_1m：MA0 两个 10 分钟桶（spark）
    for minute, price in [(1, 3001.0), (11, 3002.0)]:
        await s.execute(
            text(
                "INSERT INTO quote_1m (market, symbol, price, ts, source)"
                " VALUES ('D', 'MA0', :p, :ts, 'test') ON CONFLICT DO NOTHING"
            ),
            {"p": price, "ts": dt.datetime(2026, 11, 9, 20, minute, tzinfo=dt.UTC)},
        )
    # 知识库（真实 MA.yaml：OIL prior 0.45、q 0.58、冬季限气 11 月 bias 3.0）
    await import_knowledge(s, [d for d in load_knowledge_dir(KNOWLEDGE) if d.code == "MA"])


async def test_snapshot_values(db_session) -> None:  # type: ignore[no-untyped-def]
    await _seed(db_session)
    bundle = await build_snapshot(db_session, "MA", ASOF)
    f = bundle["features"]

    # price：单调 +1% 序列
    assert f["price"]["close"] == pytest.approx(100 * 1.01**21, abs=0.01)
    assert f["price"]["ret_1d"] == pytest.approx(1.0, abs=0.001)
    assert f["price"]["ret_5d"] == pytest.approx((1.01**5 - 1) * 100, abs=0.01)
    assert f["price"]["vol_20d"] == pytest.approx(0.0, abs=1e-6)
    assert f["price"]["pos_20d"] == pytest.approx(1.0)

    # foreign：最新 11-09 的 chg_1d = -1.5（存储值），chg_3d = 80/78.9
    assert f["foreign"]["OIL"]["chg_1d"] == pytest.approx(-1.5)
    assert f["foreign"]["OIL"]["chg_3d"] == pytest.approx((80 / 78.9 - 1) * 100, abs=0.001)
    assert f["foreign"]["OIL"]["session"] == "prior"

    # linkage：chg × prior × q = -1.5 × 0.45 × 0.58
    link = next(x for x in f["linkage"] if x["ext"] == "OIL")
    assert link["prior"] == pytest.approx(0.45)
    assert link["q"] == pytest.approx(0.58)
    assert link["contrib"] == pytest.approx(-0.39, abs=0.005)

    # seasonality：11 月 → 冬季限气
    assert f["seasonality"]["stage"] == "冬季限气"
    assert f["seasonality"]["bias"] == pytest.approx(3.0)
    assert f["seasonality"]["month"] == 11

    # news 无表（M5 才建）→ 显式 null；industry 无数据 → 显式 null
    assert f["news"] == {"sentiment_lag1": None, "count_lag1": None}
    assert f["industry"]["receipt_chg_5d"] is None
    assert f["industry"]["inventory_pct"] is None
    assert f["industry"]["basis_pct"] is None

    # display_only：spark 两点 + 日线量比 240000/120000
    assert bundle["display"]["spark_6h"] == [3001.0, 3002.0]
    assert bundle["display"]["volume_ratio"] == pytest.approx(2.0)
    assert "volume_ratio" not in f["price"]


async def test_snapshot_no_future_leak(db_session) -> None:  # type: ignore[no-untyped-def]
    """手工写入 asof 当日（11-10）的日线/外盘行，不得出现在快照里。"""
    await _seed(db_session)
    await db_session.execute(
        text(
            "INSERT INTO foreign_daily (ext_code, trade_date, close, change_pct, source)"
            " VALUES ('OIL', '2026-11-10', 99.0, 23.7, 'test') ON CONFLICT DO NOTHING"
        )
    )
    await db_session.execute(
        text(
            "INSERT INTO daily_bar (variety_code, trade_date, open, high, low, close, source)"
            " VALUES ('MA', '2026-11-10', 999, 999, 999, 999, 'test')"
            " ON CONFLICT DO NOTHING"
        )
    )
    f = (await build_snapshot(db_session, "MA", ASOF))["features"]
    assert f["foreign"]["OIL"]["chg_1d"] == pytest.approx(-1.5)  # 未被 23.7 污染
    assert f["price"]["close"] < 1000  # 未被 999 污染


async def test_snapshot_deterministic_and_persist_idempotent(db_session) -> None:  # type: ignore[no-untyped-def]
    await _seed(db_session)
    b1 = await build_snapshot(db_session, "MA", ASOF)
    b2 = await build_snapshot(db_session, "MA", ASOF)
    assert b1 == b2  # 同一 DB 状态两次构建完全一致

    await persist_snapshot(db_session, "MA", ASOF, b1)
    await persist_snapshot(db_session, "MA", ASOF, b2)
    n = await db_session.scalar(
        text("SELECT count(*) FROM feature_snapshot WHERE variety_code='MA'")
    )
    assert n == 1  # uq(variety,asof,version) 幂等
    await db_session.rollback()


async def test_empty_variety_all_nulls(db_session) -> None:  # type: ignore[no-untyped-def]
    """无任何数据的品种：缺失字段显式 null/空，绝不用 0 冒充。"""
    bundle = await build_snapshot(db_session, "AP", ASOF)
    f = bundle["features"]
    assert f["price"]["close"] is None
    assert f["price"]["ret_1d"] is None
    assert f["foreign"] == {}
    assert f["linkage"] == []
    assert f["industry"]["basis_pct"] is None
    assert bundle["display"]["spark_6h"] == []


async def test_foreign_aggregate_from_quotes(db_session) -> None:  # type: ignore[no-untyped-def]
    """quote_1m → foreign_daily：收盘=当日最后一价，chg 对此前最近交易日（跨周末回看）。"""
    await db_session.execute(text("DELETE FROM foreign_daily WHERE ext_code='OIL'"))
    await ensure_partitions(db_session, [dt.date(2026, 11, 8), dt.date(2026, 11, 9)])
    quotes = [
        (dt.date(2026, 11, 8), 20, 69.5),  # 周日（前一收盘基准）
        (dt.date(2026, 11, 9), 1, 70.0),
        (dt.date(2026, 11, 9), 22, 71.0),
    ]
    for day, hour, price in quotes:
        await db_session.execute(
            text(
                "INSERT INTO quote_1m (market, symbol, price, ts, source)"
                " VALUES ('F', 'OIL', :p, :ts, 'test') ON CONFLICT DO NOTHING"
            ),
            {"p": price, "ts": dt.datetime(day.year, day.month, day.day, hour, tzinfo=dt.UTC)},
        )

    n = await aggregate_foreign_day(db_session, dt.date(2026, 11, 9))
    assert n == 1
    row = (
        await db_session.execute(
            text(
                "SELECT close, change_pct FROM foreign_daily"
                " WHERE ext_code='OIL' AND trade_date='2026-11-09'"
            )
        )
    ).first()
    assert float(row.close) == pytest.approx(71.0)
    assert float(row.change_pct) == pytest.approx((71 / 69.5 - 1) * 100, abs=0.001)

    # 幂等：重跑行数不变
    n2 = await aggregate_foreign_day(db_session, dt.date(2026, 11, 9))
    assert n2 == 1
    total = await db_session.scalar(
        text("SELECT count(*) FROM foreign_daily WHERE ext_code='OIL' AND trade_date='2026-11-09'")
    )
    assert total == 1
    await db_session.rollback()
