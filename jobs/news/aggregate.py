"""资讯情绪日聚合：news_extract → news_sentiment_d（按 CST 自然日归属）。

同一文档同一品种若同时存在 rule 与 llm 抽取，优先 llm（DISTINCT ON），避免重复计数。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_UPSERT = text(
    """
    WITH picked AS (
        SELECT DISTINCT ON (e.doc_id, e.variety_code)
               e.doc_id, e.variety_code, e.direction, e.strength, e.confidence
        FROM news_extract e
        JOIN news_doc d ON d.id = e.doc_id
        WHERE (d.published_at AT TIME ZONE 'Asia/Shanghai')::date = :d
        ORDER BY e.doc_id, e.variety_code, (e.method = 'llm') DESC
    )
    INSERT INTO news_sentiment_d
        (variety_code, trade_date, score, doc_count, pos_count, neg_count)
    SELECT variety_code,
           :d,
           round(sum(direction * strength * COALESCE(confidence, 0.5))::numeric, 3),
           count(DISTINCT doc_id),
           count(DISTINCT doc_id) FILTER (WHERE direction > 0),
           count(DISTINCT doc_id) FILTER (WHERE direction < 0)
    FROM picked
    GROUP BY variety_code
    ON CONFLICT ON CONSTRAINT pk_news_sentiment_d DO UPDATE SET
        score = EXCLUDED.score,
        doc_count = EXCLUDED.doc_count,
        pos_count = EXCLUDED.pos_count,
        neg_count = EXCLUDED.neg_count
    RETURNING variety_code
    """
)


async def aggregate_sentiment(conn: AsyncConnection, trade_date: dt.date) -> int:
    """重算某日情绪（幂等），返回品种数。"""
    rows = (await conn.execute(_UPSERT, {"d": trade_date})).all()
    return len(rows)


async def aggregate_sentiment_range(
    conn: AsyncConnection, start: dt.date, end: dt.date
) -> int:
    """闭区间逐日重算，返回总品种-日行数。"""
    total = 0
    day = start
    while day <= end:
        total += await aggregate_sentiment(conn, day)
        day += dt.timedelta(days=1)
    return total
