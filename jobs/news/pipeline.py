"""资讯抽取流水线：读取未抽取的原文 → 规则抽取 → 幂等写入 news_extract。"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from jobs.news.dictionary import aliases_for
from jobs.news.extract import RuleExtract, extract_rule

logger = logging.getLogger(__name__)


async def load_aliases(conn: AsyncConnection) -> dict[str, tuple[str, ...]]:
    rows = (
        await conn.execute(
            text("SELECT code, name FROM variety WHERE is_tradable IS TRUE ORDER BY sort_order")
        )
    ).all()
    return {row.code: aliases_for(row.code, row.name) for row in rows}


async def insert_extracts(
    conn: AsyncConnection,
    doc_id: int,
    extracts: Sequence[RuleExtract],
    *,
    method: str,
    model: str | None = None,
) -> int:
    """写入抽取结果；evidence 必须出现在原文中（防御性二次校验）。"""
    if not extracts:
        return 0
    source_text = (
        await conn.execute(
            text("SELECT title || chr(10) || COALESCE(content, '') AS body FROM news_doc"
                 " WHERE id = :id"),
            {"id": doc_id},
        )
    ).scalar()
    body = source_text or ""
    inserted = 0
    for item in extracts:
        if not item.evidence or item.evidence not in body:
            logger.warning("news_evidence_invalid doc_id=%s code=%s", doc_id, item.variety_code)
            continue
        result = await conn.execute(
            text(
                "INSERT INTO news_extract (doc_id, variety_code, direction, strength,"
                " chain_stage, is_surprise, method, model, confidence, evidence)"
                " VALUES (:doc_id, :code, :direction, :strength, :stage, :surprise,"
                " :method, :model, :confidence, :evidence)"
                " ON CONFLICT ON CONSTRAINT uq_news_extract_doc_variety_method DO NOTHING"
            ),
            {
                "doc_id": doc_id,
                "code": item.variety_code,
                "direction": item.direction,
                "strength": item.strength,
                "stage": item.chain_stage,
                "surprise": item.is_surprise,
                "method": method,
                "model": model,
                "confidence": item.confidence,
                "evidence": item.evidence,
            },
        )
        inserted += result.rowcount or 0
    return inserted


async def load_pending_docs(
    conn: AsyncConnection, *, since: dt.datetime, limit: int = 500
) -> list[Any]:
    """取窗口内尚未规则抽取的原文（新→旧）。"""
    rows = (
        await conn.execute(
            text(
                "SELECT d.id, d.title, d.content FROM news_doc d"
                " WHERE d.published_at >= :since"
                "   AND NOT EXISTS (SELECT 1 FROM news_extract e"
                "                   WHERE e.doc_id = d.id AND e.method = 'rule')"
                " ORDER BY d.published_at DESC LIMIT :limit"
            ),
            {"since": since, "limit": limit},
        )
    ).all()
    return list(rows)


async def extract_recent(
    conn: AsyncConnection, *, since: dt.datetime, limit: int = 500
) -> dict[str, int]:
    aliases = await load_aliases(conn)
    docs = await load_pending_docs(conn, since=since, limit=limit)
    inserted = 0
    for doc in docs:
        extracts = extract_rule(doc.title, doc.content, aliases)
        inserted += await insert_extracts(conn, doc.id, extracts, method="rule")
    return {"docs": len(docs), "extracts": inserted}
