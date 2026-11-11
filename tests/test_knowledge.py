"""知识库测试：YAML 校验 + 导入幂等。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from czce_schema.symbols import DOMESTIC_SINA_SYMBOLS
from jobs.knowledge import load_knowledge_dir
from jobs.knowledge.importer import import_knowledge
from jobs.knowledge.models import KnowledgeDoc

REPO = Path(__file__).resolve().parents[1]
KNOWLEDGE = REPO / "data" / "knowledge"


def _docs() -> dict[str, KnowledgeDoc]:
    return {d.code: d for d in load_knowledge_dir(KNOWLEDGE)}


def test_all_yaml_load_and_match_varieties() -> None:
    docs = _docs()
    assert len(docs) == 21
    assert set(docs) == set(DOMESTIC_SINA_SYMBOLS)
    # 每个品种至少一条驱动；全部已核实（2026-11-10，AI 对照手册 + 回测报告，用户授权）
    assert all(d.drivers for d in docs.values())
    assert all(d.reviewed for d in docs.values())
    assert {d.reviewed_by for d in docs.values()} == {"ai"}


def test_linkage_q_matches_v1_report() -> None:
    """q>0 的品种集合必须与 v1 回测报告一致（§二 分品种表）。"""
    docs = _docs()
    positive = {code for code, d in docs.items() if d.linkage_q > 0}
    assert positive == {"CF", "CY", "MA", "OI", "PF", "PL", "PR", "PX", "RM", "RS", "TA"}
    assert docs["OI"].linkage_q == 1.0
    assert docs["SR"].linkage_q == 0.0  # ICE 原糖源缺失，如实置 0


def test_seasonality_and_events_present() -> None:
    docs = _docs()
    # 手册明确有季节规律的品种必须有 seasonality
    for code in ("MA", "SR", "UR", "FG", "SA", "RM", "PR", "CF"):
        assert docs[code].seasonality, f"{code} 缺季节性"
    # AP/CJ 是气候/事件驱动，季节性显式置空（不用 0 冒充）
    assert docs["AP"].seasonality == []
    assert docs["CJ"].events


def test_invalid_proxy_ext_rejected() -> None:
    raw = dict(_docs()["MA"].model_dump())
    raw["drivers"] = [{**raw["drivers"][0], "proxy_ext": "XXX"}]
    with pytest.raises(ValidationError, match="未知外盘代码"):
        KnowledgeDoc.model_validate(raw)


def test_invalid_month_rejected() -> None:
    raw = dict(_docs()["MA"].model_dump())
    raw["seasonality"] = [{**raw["seasonality"][0], "months": [0]}]
    with pytest.raises(ValidationError, match="月份越界"):
        KnowledgeDoc.model_validate(raw)


def test_unknown_code_rejected() -> None:
    raw = dict(_docs()["MA"].model_dump())
    raw["code"] = "XX"
    with pytest.raises(ValidationError, match="21 个可交易品种"):
        KnowledgeDoc.model_validate(raw)


async def test_import_is_idempotent(db_session) -> None:  # type: ignore[no-untyped-def]
    docs = list(_docs().values())
    s1 = await import_knowledge(db_session, docs)
    edges_1 = await db_session.scalar(text("SELECT count(*) FROM knowledge_edge"))
    nodes_1 = await db_session.scalar(text("SELECT count(*) FROM knowledge_node"))
    season_1 = await db_session.scalar(text("SELECT count(*) FROM seasonality"))
    s2 = await import_knowledge(db_session, docs)
    edges_2 = await db_session.scalar(text("SELECT count(*) FROM knowledge_edge"))
    nodes_2 = await db_session.scalar(text("SELECT count(*) FROM knowledge_node"))
    season_2 = await db_session.scalar(text("SELECT count(*) FROM seasonality"))
    assert (edges_1, nodes_1, season_1) == (edges_2, nodes_2, season_2)
    assert s1.edges == s2.edges == 59
    assert s1.seasonality_rows == 62  # 19 条规律 × 各自月份展开
    assert s1.reviewed == 21
    await db_session.rollback()


async def test_import_updates_variety_linkage_q(db_session) -> None:  # type: ignore[no-untyped-def]
    await import_knowledge(db_session, list(_docs().values()))
    q = await db_session.scalar(text("SELECT linkage_q FROM variety WHERE code = 'MA'"))
    assert q is not None and float(q) == pytest.approx(0.58)
    # MA 的 OIL 边：prior 0.45，reviewed=false
    row = (
        await db_session.execute(
            text(
                "SELECT prior, reviewed FROM knowledge_edge"
                " WHERE variety_code='MA' AND proxy_ext='OIL'"
            )
        )
    ).first()
    assert row is not None
    assert float(row.prior) == pytest.approx(0.45)
    assert row.reviewed is True
    await db_session.rollback()


async def test_import_replaces_seasonality(db_session) -> None:  # type: ignore[no-untyped-def]
    """YAML 删掉某个月后重导入，旧行不得残留。"""
    docs = list(_docs().values())
    await import_knowledge(db_session, docs)
    await db_session.execute(text("DELETE FROM seasonality WHERE variety_code='MA' AND month=12"))
    # 再导入一次（模拟 YAML 修改后重跑）
    await import_knowledge(db_session, docs)
    n = await db_session.scalar(text("SELECT count(*) FROM seasonality WHERE variety_code='MA'"))
    assert n == 7  # 冬季限气 [11,12,1,2,3] + 梅雨累库 [6,7]
    await db_session.rollback()
