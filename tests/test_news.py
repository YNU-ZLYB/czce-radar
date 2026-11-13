"""M5 资讯层测试：采集解析、去重、规则/LLM 抽取、情绪聚合、预期差修订。"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import text

from jobs.collectors.base import RawItem
from jobs.collectors.news import (
    cls_sign,
    parse_cls,
    parse_czce_announcements,
    parse_eastmoney,
    parse_jin10,
    parse_sina_news,
)
from jobs.features.build import build_snapshot
from jobs.ingest.news import upsert_news_docs
from jobs.news.aggregate import aggregate_sentiment
from jobs.news.dedup import is_duplicate, jaccard
from jobs.news.extract import extract_rule
from jobs.news.llm import load_recent_docs, parse_llm_extracts, run_llm_extraction
from jobs.news.pipeline import load_aliases
from jobs.news.revision import apply_revisions

NOW = dt.datetime(2026, 11, 10, 3, 0, tzinfo=dt.UTC)


# ---------- 采集解析 ----------


def test_parse_jin10() -> None:
    payload = {
        "data": [
            {
                "id": "1001",
                "time": "2026-11-10 15:04:05",
                "data": {"content": "<b>白糖</b>：广西糖厂开榨推迟，现货报价上涨。"},
            },
            {"id": "", "time": "2026-11-10 15:05:00", "data": {"content": "无 id 应丢弃"}},
        ]
    }
    items = parse_jin10(payload)
    assert len(items) == 1
    item = items[0]
    assert item.source == "jin10_flash"
    assert item.external_id == "1001"
    assert item.ts == dt.datetime(2026, 11, 10, 7, 4, 5, tzinfo=dt.UTC)
    assert item.payload["url"] == "https://flash.jin10.com/detail/1001"
    assert "<b>" not in item.payload["content"]


def test_parse_eastmoney() -> None:
    body = (
        'var ajaxResult={"LivesList":[{"id":"e1","title":"甲醇供应紧张",'
        '"digest":"西北装置检修增多","showtime":"2026-11-10 09:30:00",'
        '"url_unique":"https://finance.eastmoney.com/a/e1.html"}]};'
    )
    items = parse_eastmoney(body)
    assert len(items) == 1
    assert items[0].payload["title"] == "甲醇供应紧张"
    assert items[0].payload["url"].endswith("e1.html")


def test_parse_sina_news() -> None:
    payload = {
        "result": {
            "data": {
                "feed": {
                    "list": [
                        {
                            "id": "s1",
                            "rich_text": "尿素需求疲软，企业降价出货。",
                            "create_time": "2026-11-10 11:00:00",
                            "docurl": "https://finance.sina.com.cn/s1",
                        }
                    ]
                }
            }
        }
    }
    items = parse_sina_news(payload)
    assert len(items) == 1
    assert items[0].ts == dt.datetime(2026, 11, 10, 3, 0, tzinfo=dt.UTC)


def test_cls_sign_is_stable() -> None:
    params = {"app": "CailianpressWeb", "os": "web", "rn": "20", "sv": "7.7.5"}
    assert cls_sign(params) == cls_sign(dict(reversed(list(params.items()))))


def test_parse_cls() -> None:
    payload = {
        "data": {
            "roll_data": [
                {
                    "id": 2533734,
                    "title": "国务院安委办对11个市级政府开展安全生产约谈",
                    "brief": "【国务院安委办…】财联社11月13日电，从应急管理部获悉。",
                    "ctime": "1794552897",
                    "shareurl": "https://api3.cls.cn/share/article/2533734?os=web",
                    "is_ad": False,
                    "ad": {"id": 0, "title": "", "url": ""},
                },
                {"id": 1, "brief": "广告", "is_ad": True},
                {"id": 3, "brief": "广告位", "ad": {"id": 77}},
                {"id": 2, "brief": "", "is_ad": False},
            ]
        }
    }
    items = parse_cls(payload)
    assert len(items) == 1
    item = items[0]
    assert item.source == "cls_telegraph"
    assert item.external_id == "2533734"
    assert item.ts == dt.datetime.fromtimestamp(1794552897, dt.UTC)
    assert item.payload["url"].startswith("https://api3.cls.cn/share/article/2533734")


def test_parse_czce_announcements() -> None:
    body = (
        '<li><a href="/cn/rootfiles/2026/11/10/abc.htm" target="_blank">'
        "关于调整白糖期货合约交易保证金标准的通知</a><span>2026-11-10</span></li>"
        '<li><a href="/cn/index.htm">首页</a></li>'
    )
    items = parse_czce_announcements(body)
    assert len(items) == 1
    assert items[0].external_id.startswith("/cn/rootfiles/")
    assert items[0].payload["url"].startswith("http://www.czce.com.cn/cn/rootfiles/")


# ---------- 去重与入库 ----------


def test_title_dedup_jaccard() -> None:
    a = "广西糖厂开榨推迟 白糖现货报价上涨"
    b = "广西糖厂开榨推迟，白糖现货报价上涨"
    assert jaccard(a, b) >= 0.9
    assert is_duplicate(a, [b])
    assert not is_duplicate("甲醇装置重启", [b])


async def test_upsert_news_docs_dedup_and_future(db_session) -> None:  # type: ignore[no-untyped-def]
    await db_session.execute(text("DELETE FROM news_extract"))
    await db_session.execute(text("DELETE FROM news_doc"))
    now = NOW
    items = [
        RawItem(
            source="jin10_flash",
            external_id="a1",
            ts=now,
            payload={"title": "白糖现货报价上涨", "content": "广西糖厂开榨推迟。"},
        ),
        # content_hash 相同（重复源）
        RawItem(
            source="sina_news",
            external_id="a2",
            ts=now,
            payload={"title": "白糖现货报价上涨", "content": "广西糖厂开榨推迟。"},
        ),
        # 标题近似（Jaccard >= 0.9）但正文不同
        RawItem(
            source="eastmoney_news",
            external_id="a3",
            ts=now,
            payload={"title": "白糖现货报价上涨！", "content": "另一条内容。"},
        ),
        # 来自未来的发布时间应丢弃
        RawItem(
            source="jin10_flash",
            external_id="a4",
            ts=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
            payload={"title": "未来资讯", "content": "不应入库。"},
        ),
    ]
    assert await upsert_news_docs(db_session, items) == 1
    total = await db_session.scalar(text("SELECT count(*) FROM news_doc"))
    assert total == 1
    await db_session.rollback()


# ---------- 规则抽取 ----------


def test_extract_rule_evidence_is_substring() -> None:
    title = "白糖：广西糖厂开榨推迟"
    content = "受降雨影响，广西白糖糖厂开榨推迟，市场预计供应紧张，现货报价上涨。"
    aliases = {"SR": ("白糖", "食糖")}
    extracts = extract_rule(title, content, aliases)
    assert len(extracts) == 1
    item = extracts[0]
    assert item.variety_code == "SR"
    assert item.direction == 1
    assert item.chain_stage == "upstream"
    assert item.evidence in f"{title}\n{content}"


def test_extract_rule_multi_variety_direction() -> None:
    title = "甲醇与尿素同步走弱"
    content = "甲醇现货价格下跌，尿素需求疲软，企业累库。"
    extracts = {e.variety_code: e for e in extract_rule(title, content, {
        "MA": ("甲醇",),
        "UR": ("尿素",),
    })}
    assert set(extracts) == {"MA", "UR"}
    assert extracts["MA"].direction == -1
    assert extracts["UR"].direction == -1
    assert all(e.evidence in f"{title}\n{content}" for e in extracts.values())


def test_extract_rule_ignores_apple_inc_in_stock_news() -> None:
    title = "美股三大股指集体低开 英伟达开盘续跌近3%"
    content = "苹果股价走低，科技股承压。"
    assert extract_rule(title, content, {"AP": ("苹果",)}) == []


def test_extract_rule_market_wide_decline_is_bearish() -> None:
    title = "烧碱、玻璃夜盘跌多涨少"
    content = "主力合约小幅波动。"
    extracts = {e.variety_code: e for e in extract_rule(title, content, {
        "SH": ("烧碱",),
        "FG": ("玻璃",),
    })}
    assert extracts["SH"].direction == -1
    assert extracts["FG"].direction == -1


# ---------- LLM 抽取 ----------


def test_parse_llm_drops_fabricated_evidence() -> None:
    raw = json.dumps(
        {
            "extracts": [
                {
                    "variety_code": "SR",
                    "direction": -1,
                    "strength": 4,
                    "confidence": 0.7,
                    "evidence": "原文中不存在的一句话",
                },
                {
                    "variety_code": "SR",
                    "direction": 1,
                    "strength": 3,
                    "confidence": 0.6,
                    "evidence": "白糖现货报价上涨",
                },
                {
                    "variety_code": "ZZ",
                    "direction": 1,
                    "strength": 3,
                    "evidence": "白糖现货报价上涨",
                },
            ]
        },
        ensure_ascii=False,
    )
    extracts, dropped = parse_llm_extracts(
        raw, "白糖现货报价上涨", None, {"SR", "MA"}
    )
    assert dropped == 2
    assert len(extracts) == 1
    assert extracts[0].variety_code == "SR"
    assert extracts[0].evidence == "白糖现货报价上涨"


class _FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def extract(
        self, title: str, content: str | None, *, system_extra: str = ""
    ) -> tuple[str, int, int]:
        self.calls += 1
        raw = json.dumps(
            {
                "extracts": [
                    {
                        "variety_code": "SR",
                        "direction": -1,
                        "strength": 4,
                        "confidence": 0.7,
                        "evidence": title,
                    }
                ]
            },
            ensure_ascii=False,
        )
        return raw, 100, 50


async def test_llm_cache_hit_and_budget(db_session) -> None:  # type: ignore[no-untyped-def]
    from jobs.news.pipeline import extract_recent

    await db_session.execute(text("DELETE FROM news_extract"))
    await db_session.execute(text("DELETE FROM llm_call_log"))
    await db_session.execute(text("DELETE FROM news_doc"))
    items = [
        RawItem(
            source="jin10_flash",
            external_id=f"llm{i}",
            ts=NOW - dt.timedelta(minutes=i),
            payload={"title": f"白糖现货报价上涨第{i}条", "content": "广西糖厂开榨推迟。"},
        )
        for i in range(3)
    ]
    await upsert_news_docs(db_session, items)
    await extract_recent(db_session, since=NOW - dt.timedelta(hours=1))
    docs = await load_recent_docs(db_session, since=NOW - dt.timedelta(hours=1))
    aliases = await load_aliases(db_session)
    client = _FakeLLM()

    first = await run_llm_extraction(
        db_session, docs, client, model="fake", aliases=aliases, now=NOW
    )
    assert first.calls == 3
    assert first.cache_hits == 0
    assert first.cost_cny <= 1.0

    # 同一批文档重跑 → 全部命中缓存
    second = await run_llm_extraction(
        db_session, docs, client, model="fake", aliases=aliases, now=NOW
    )
    assert second.calls == 0
    assert second.cache_hits == 3
    assert second.cache_hit_rate == 1.0

    # 预算已耗尽 → 不再调用（created_at 显式落在 NOW 当日，避免跨午夜日期漂移）
    await db_session.execute(
        text(
            "INSERT INTO llm_call_log (purpose, model, cost_cny, cache_hit, created_at)"
            " VALUES ('extract_event', 'fake', 5.0, FALSE, :now)"
        ),
        {"now": NOW},
    )
    extra = RawItem(
        source="jin10_flash",
        external_id="llm-extra",
        ts=NOW - dt.timedelta(minutes=10),
        payload={"title": "白糖现货报价上涨额外条", "content": "广西糖厂开榨推迟。"},
    )
    await upsert_news_docs(db_session, [extra])
    docs2 = await load_recent_docs(db_session, since=NOW - dt.timedelta(hours=1))
    before = client.calls
    third = await run_llm_extraction(
        db_session, docs2, client, model="fake", aliases=aliases, now=NOW
    )
    assert third.degraded is True
    assert client.calls == before  # 未发生新调用
    await db_session.rollback()


# ---------- 情绪聚合 + 泄漏 ----------


async def _seed_doc_with_extract(
    session,  # type: ignore[no-untyped-def]
    *,
    title: str,
    published: dt.datetime,
    direction: int,
    strength: int = 4,
    surprise: bool = True,
) -> int:
    doc_id = await session.scalar(
        text(
            "INSERT INTO news_doc (source, external_id, title, content, published_at,"
            " fetched_at, content_hash) VALUES ('test', :eid, :t, :t, :p, :p, :h)"
            " RETURNING id"
        ),
        {
            "eid": f"ext-{title}",
            "t": title,
            "p": published,
            "h": f"{abs(hash(title)):040d}"[:40],
        },
    )
    await session.execute(
        text(
            "INSERT INTO news_extract (doc_id, variety_code, direction, strength,"
            " chain_stage, is_surprise, method, model, confidence, evidence)"
            " VALUES (:doc, 'MA', :d, :s, 'upstream', :sp, 'rule', NULL, 0.8, :ev)"
        ),
        {
            "doc": doc_id,
            "d": direction,
            "s": strength,
            "sp": surprise,
            "ev": title,
        },
    )
    return int(doc_id)


async def test_sentiment_strict_lag1_no_leak(db_session) -> None:  # type: ignore[no-untyped-def]
    await db_session.execute(text("DELETE FROM news_extract"))
    await db_session.execute(text("DELETE FROM news_doc"))
    await db_session.execute(text("DELETE FROM news_sentiment_d WHERE variety_code = 'MA'"))
    # 11-09 CST 10:00（=02:00 UTC）利多
    await _seed_doc_with_extract(
        db_session,
        title="甲醇装置检修供应紧张",
        published=dt.datetime(2026, 11, 9, 2, 0, tzinfo=dt.UTC),
        direction=1,
    )
    # 11-10 CST 00:30（=11-09 16:30 UTC）利空：发布时间早于 asof，但属于 T 日，必须排除
    await _seed_doc_with_extract(
        db_session,
        title="甲醇库存高企需求疲软",
        published=dt.datetime(2026, 11, 9, 16, 30, tzinfo=dt.UTC),
        direction=-1,
    )
    await aggregate_sentiment(db_session, dt.date(2026, 11, 9))
    await aggregate_sentiment(db_session, dt.date(2026, 11, 10))

    snapshot = await build_snapshot(
        db_session, "MA", dt.datetime(2026, 11, 10, 0, 30, tzinfo=dt.UTC)
    )
    news = snapshot["features"]["news"]
    assert news["count_lag1"] == 1
    assert news["sentiment_lag1"] == pytest.approx(1 * 4 * 0.8, abs=0.01)
    await db_session.rollback()


# ---------- 预期差修订 ----------


async def test_revision_created_once_and_alert(db_session) -> None:  # type: ignore[no-untyped-def]
    target = dt.date(2030, 1, 2)
    version = f"rule-{target:%Y%m%d}-01"
    await db_session.execute(text("DELETE FROM prediction_revision"))
    await db_session.execute(
        text("DELETE FROM prediction WHERE target_date = :d"), {"d": target}
    )
    await db_session.execute(text("DELETE FROM model_registry WHERE kind = 'fusion'"))
    await db_session.execute(text("DELETE FROM news_extract"))
    await db_session.execute(text("DELETE FROM news_doc"))
    pid = await db_session.scalar(
        text(
            "INSERT INTO prediction (variety_code, target_date, horizon, asof, direction,"
            " bucket, p_up, p_flat, p_down, exp_change, signal, confidence, contributor,"
            " model_version) VALUES ('MA', :d, '1d', :a, 1, '偏多', 0.5, 0.3, 0.2, 1.0,"
            " 20, 0.5, '[]'::jsonb, :v) RETURNING id"
        ),
        {"d": target, "a": dt.datetime(2030, 1, 2, 0, 30, tzinfo=dt.UTC), "v": version},
    )
    assert pid is not None

    for i in range(4):
        await _seed_doc_with_extract(
            db_session,
            title=f"甲醇超预期累库第{i}条",
            published=dt.datetime(2030, 1, 2, 2 + i, 0, tzinfo=dt.UTC),
            direction=-1,
            strength=5,
        )
    result = await apply_revisions(db_session, target, now=NOW)
    assert result.created == 4
    assert result.per_variety["MA"] == 4
    assert "MA" in result.alerts

    # 幂等：同一批文档重跑不新增
    again = await apply_revisions(db_session, target, now=NOW)
    assert again.created == 0
    assert again.per_variety["MA"] == 4

    row = (
        await db_session.execute(
            text(
                "SELECT prev_signal, new_signal, prev_direction, new_direction, reason,"
                " trigger_doc_id FROM prediction_revision WHERE prediction_id = :p"
                " LIMIT 1"
            ),
            {"p": pid},
        )
    ).first()
    assert row.prev_signal == 20
    assert row.new_signal == -5  # 20 + (-1)*5*5
    assert row.prev_direction == 1
    assert row.new_direction == -1
    assert "预期差资讯" in row.reason
    await db_session.rollback()
