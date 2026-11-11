"""知识库导入器：YAML → knowledge_node / knowledge_edge / seasonality / variety.linkage_q。

幂等：全部走 upsert；重复导入只更新内容，不产生重复行。
``reviewed`` 语义：YAML 里 `reviewed: true` 的知识才允许参与模型权重（M3 引擎侧校验）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import delete, insert, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app.models.knowledge import KnowledgeEdge, KnowledgeNode, Seasonality
from app.models.variety import Variety
from jobs.knowledge.models import KnowledgeDoc

_CHAIN_KINDS: Final = ("upstream", "midstream", "downstream")


@dataclass(slots=True)
class ImportSummary:
    varieties: int
    nodes: int
    edges: int
    seasonality_rows: int
    reviewed: int

    def __str__(self) -> str:  # pragma: no cover - 仅供人读
        return (
            f"varieties={self.varieties} nodes={self.nodes} edges={self.edges}"
            f" seasonality={self.seasonality_rows} reviewed={self.reviewed}"
        )


def _node_rows(docs: Sequence[KnowledgeDoc]) -> list[dict[str, str]]:
    """跨品种去重后的产业链节点。"""
    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, str]] = []
    for doc in docs:
        for kind in _CHAIN_KINDS:
            for name in doc.chain.get(kind, []):
                key = (kind, name)
                if key not in seen:
                    seen.add(key)
                    rows.append({"kind": kind, "name": name})
    return rows


def _edge_rows(doc: KnowledgeDoc) -> list[dict[str, object]]:
    return [
        {
            "variety_code": doc.code,
            "kind": d.kind.value,
            "name": d.name,
            "proxy_ext": d.proxy_ext,
            "proxy_variety": d.proxy_variety,
            "metric": d.metric,
            "lag_days": d.lag_days,
            "prior": d.prior,
            "mechanism": d.mechanism,
            "reviewed": doc.reviewed,
        }
        for d in doc.drivers
    ]


def _seasonality_rows(doc: KnowledgeDoc) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for stage in doc.seasonality:
        for month in stage.months:
            rows.append(
                {
                    "variety_code": doc.code,
                    "month": month,
                    "stage": stage.stage,
                    "bias": stage.bias,
                    "confidence": stage.confidence,
                    "note": stage.note,
                    "reviewed_by": (doc.reviewed_by or "user") if doc.reviewed else None,
                }
            )
    return rows


async def import_knowledge(conn: AsyncConnection, docs: Sequence[KnowledgeDoc]) -> ImportSummary:
    """在一个事务连接里导入全部知识。调用方负责 commit/rollback。"""
    # 1) 产业链节点（kind+name 唯一，冲突忽略）
    nodes = _node_rows(docs)
    if nodes:
        await conn.execute(
            pg_insert(KnowledgeNode)
            .values(nodes)
            .on_conflict_do_nothing(index_elements=["kind", "name"])
        )

    # 2) 传导边（variety+name 唯一，冲突整行更新）
    edges = [row for doc in docs for row in _edge_rows(doc)]
    if edges:
        await conn.execute(
            pg_insert(KnowledgeEdge)
            .values(edges)
            .on_conflict_do_update(
                constraint="uq_knowledge_edge_variety_name",
                set_={
                    "kind": pg_insert(KnowledgeEdge).excluded.kind,
                    "proxy_ext": pg_insert(KnowledgeEdge).excluded.proxy_ext,
                    "proxy_variety": pg_insert(KnowledgeEdge).excluded.proxy_variety,
                    "metric": pg_insert(KnowledgeEdge).excluded.metric,
                    "lag_days": pg_insert(KnowledgeEdge).excluded.lag_days,
                    "prior": pg_insert(KnowledgeEdge).excluded.prior,
                    "mechanism": pg_insert(KnowledgeEdge).excluded.mechanism,
                    "reviewed": pg_insert(KnowledgeEdge).excluded.reviewed,
                },
            )
        )

    # 3) 季节性：整品种替换（删旧插新，避免残留已从 YAML 删除的月份）
    season_rows = [row for doc in docs for row in _seasonality_rows(doc)]
    for doc in docs:
        await conn.execute(delete(Seasonality).where(Seasonality.variety_code == doc.code))
    if season_rows:
        await conn.execute(insert(Seasonality), season_rows)

    # 4) 品种级校准系数
    for doc in docs:
        await conn.execute(
            update(Variety).where(Variety.code == doc.code).values(linkage_q=doc.linkage_q)
        )

    return ImportSummary(
        varieties=len(docs),
        nodes=len(nodes),
        edges=len(edges),
        seasonality_rows=len(season_rows),
        reviewed=sum(1 for d in docs if d.reviewed),
    )
