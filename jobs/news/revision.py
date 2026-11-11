"""预期差判定 → prediction_revision（只追加，不覆盖原预测）。

触发条件（《02》§9）：is_surprise 且 strength ≥ 4，且资讯方向与当前预测方向不一致。
单品种单日修订 > 3 次 → 告警（日志 + 接口返回 alert 标记）。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

ALERT_THRESHOLD = 3
SIGNAL_STEP = 5


@dataclass(slots=True)
class RevisionResult:
    created: int = 0
    alerts: list[str] = field(default_factory=list)
    per_variety: dict[str, int] = field(default_factory=dict)


def _clamp(value: int) -> int:
    return max(-100, min(100, value))


async def _active_version(conn: AsyncConnection, target_date: dt.date) -> str:
    active = await conn.scalar(
        text(
            "SELECT version FROM model_registry"
            " WHERE kind = 'fusion' AND status = 'active'"
            " ORDER BY created_at DESC LIMIT 1"
        )
    )
    return str(active) if active else f"rule-{target_date:%Y%m%d}-01"


async def apply_revisions(
    conn: AsyncConnection, target_date: dt.date, *, now: dt.datetime | None = None
) -> RevisionResult:
    """扫描当日预期差资讯，为方向冲突的预测追加修订记录。"""
    now = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    version = await _active_version(conn, target_date)
    predictions = (
        await conn.execute(
            text(
                "SELECT id, variety_code, direction, signal FROM prediction"
                " WHERE target_date = :d AND model_version = :v"
            ),
            {"d": target_date, "v": version},
        )
    ).all()
    by_code = {r.variety_code: r for r in predictions}
    if not by_code:
        return RevisionResult()

    extras = (
        await conn.execute(
            text(
                "SELECT e.id AS extract_id, e.variety_code, e.direction, e.strength, e.evidence,"
                "       d.id AS doc_id, d.title, d.source"
                " FROM news_extract e JOIN news_doc d ON d.id = e.doc_id"
                " WHERE e.is_surprise IS TRUE AND e.strength >= 4 AND e.direction <> 0"
                "   AND (d.published_at AT TIME ZONE 'Asia/Shanghai')::date = :d"
                " ORDER BY d.published_at"
            ),
            {"d": target_date},
        )
    ).all()

    result = RevisionResult()
    for extra in extras:
        pred = by_code.get(extra.variety_code)
        if pred is None or pred.direction == extra.direction:
            continue
        new_signal = _clamp(
            int(pred.signal) + int(extra.direction) * int(extra.strength) * SIGNAL_STEP
        )
        reason = (
            f"预期差资讯（{extra.source}）：{extra.title}｜依据：{(extra.evidence or '')[:120]}"
        )
        inserted = await conn.scalar(
            text(
                "INSERT INTO prediction_revision (prediction_id, at, prev_signal, new_signal,"
                " prev_direction, new_direction, reason, trigger_doc_id, is_surprise)"
                " VALUES (:pid, :at, :ps, :ns, :pd, :nd, :reason, :doc, TRUE)"
                " ON CONFLICT ON CONSTRAINT uq_revision_prediction_doc DO NOTHING"
                " RETURNING id"
            ),
            {
                "pid": pred.id,
                "at": now,
                "ps": pred.signal,
                "ns": new_signal,
                "pd": pred.direction,
                "nd": extra.direction,
                "reason": reason,
                "doc": extra.doc_id,
            },
        )
        if inserted is not None:
            result.created += 1

    counts = (
        await conn.execute(
            text(
                "SELECT p.variety_code, count(*) AS cnt FROM prediction_revision r"
                " JOIN prediction p ON p.id = r.prediction_id"
                " WHERE p.target_date = :d GROUP BY p.variety_code"
            ),
            {"d": target_date},
        )
    ).all()
    result.per_variety = {row.variety_code: int(row.cnt) for row in counts}
    for code, count in result.per_variety.items():
        if count > ALERT_THRESHOLD:
            result.alerts.append(code)
            logger.error(
                "news_revision_alert variety=%s target_date=%s count=%s threshold=%s",
                code,
                target_date,
                count,
                ALERT_THRESHOLD,
            )
    return result
