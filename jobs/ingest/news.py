"""资讯原始数据入库：归一化、内容哈希去重、标题近似去重。

BaseCollector 只负责拿；本模块负责清洗与幂等写入 news_doc。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from jobs.collectors.base import RawItem
from jobs.news.dedup import is_duplicate

logger = logging.getLogger(__name__)

_MAX_FUTURE_SKEW = dt.timedelta(minutes=5)
_RECENT_WINDOW = dt.timedelta(hours=72)
_WS_RE = re.compile(r"\s+")


def _clean(value: str | None) -> str:
    return _WS_RE.sub(" ", value or "").strip()


def content_hash(title: str, content: str | None) -> str:
    """归一化文本的 SHA-1：标题+正文，忽略空白差异。"""
    payload = f"{_clean(title)}\n{_clean(content)}".encode()
    return hashlib.sha1(payload).hexdigest()


def normalize_news(items: list[RawItem], *, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """RawItem → news_doc 行；丢弃空标题与来自未来的发布时间。"""
    now = now or dt.datetime.now(dt.UTC)
    limit = now + _MAX_FUTURE_SKEW
    rows: list[dict[str, Any]] = []
    for item in items:
        title = _clean(item.payload.get("title"))
        content = _clean(item.payload.get("content")) or None
        if not title:
            continue
        published = item.ts or now
        if published.tzinfo is None:
            published = published.replace(tzinfo=dt.UTC)
        published = published.astimezone(dt.UTC)
        if published > limit:
            continue
        rows.append(
            {
                "source": item.source,
                "external_id": item.external_id,
                "title": title,
                "content": content,
                "url": item.payload.get("url"),
                "published_at": published,
                "fetched_at": now,
                "content_hash": content_hash(title, content),
                "raw": item.payload,
            }
        )
    return rows


async def _recent_titles(conn: AsyncConnection, now: dt.datetime) -> list[str]:
    result = await conn.execute(
        text(
            "SELECT title FROM news_doc WHERE published_at >= :since"
            " ORDER BY published_at DESC LIMIT 500"
        ),
        {"since": now - _RECENT_WINDOW},
    )
    return [row.title for row in result]


async def upsert_news_docs(conn: AsyncConnection, items: list[RawItem]) -> int:
    """幂等写入 news_doc，返回真正新增条数。"""
    rows = normalize_news(items)
    if not rows:
        return 0
    now = dt.datetime.now(dt.UTC)
    existing_titles = await _recent_titles(conn, now)
    inserted = 0
    for row in rows:
        if is_duplicate(row["title"], existing_titles):
            logger.info("news_title_duplicate source=%s title=%s", row["source"], row["title"][:40])
            continue
        result = await conn.execute(
            text(
                "INSERT INTO news_doc (source, external_id, title, content, url,"
                " published_at, fetched_at, content_hash, raw)"
                " VALUES (:source, :external_id, :title, :content, :url,"
                " :published_at, :fetched_at, :content_hash, CAST(:raw AS jsonb))"
                " ON CONFLICT DO NOTHING"
            ),
            {**row, "raw": _json(row["raw"])},
        )
        if result.rowcount:
            inserted += 1
            existing_titles.append(row["title"])
    return inserted


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)
