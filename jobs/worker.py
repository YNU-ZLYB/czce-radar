"""Arq Worker 入口：所有重计算都在这里，API 进程不参与。

启动：arq jobs.worker.WorkerSettings
队列任务（cron 均为 UTC）：
- collect_quotes        每分钟    新浪实时行情（国内 21 + 外盘 10）
- ensure_partitions     00:05 CST 预建未来两天的行情分区
- collect_daily_bars    15:10 CST 国内 21 品种日线（幂等，可重复执行）
- collect_receipts      16:40 CST 郑商所仓单日报 → industry_metric
- aggregate_foreign     08:10 CST 昨日外盘分钟行情聚合为 foreign_daily 收盘
- collect_fred_dx       08:10 CST FRED 美元指数日线（替代新浪停供 hf_DX）
- build_snapshots       08:25 CST 21 品种 FeatureSnapshot（f1，幂等）
- predict_daily         08:30 CST Layer A 规则预测 → prediction（幂等）
- score_daily           15:20 CST 昨日及之前到期预测的事后评分（幂等）
- scorecard_daily       15:30 CST 滚动 60d 看板落库 scorecard_snapshot（幂等）
- weekly_report         周一 09:00 CST 本周 vs 上周模型表现对比与重训建议
- train_weekly          周一 03:00 CST LightGBM 重训 + 校准 + 融合（达标才 active）
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any, ClassVar

import httpx
from arq import cron
from arq.connections import RedisSettings
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import load_settings
from app.core.db import create_engine, create_session_factory
from czce_schema.constants import WORKER_HEARTBEAT_KEY
from czce_schema.symbols import DOMESTIC_SINA_SYMBOLS
from jobs.collectors.base import BaseCollector
from jobs.collectors.czce_receipt import CzceReceiptCollector
from jobs.collectors.fred import FredDollarIndexCollector
from jobs.collectors.news import (
    CzceAnnouncementCollector,
    EastmoneyNewsCollector,
    Jin10FlashCollector,
    SinaNewsCollector,
)
from jobs.collectors.sina_daily import SinaDailyKlineCollector
from jobs.collectors.sina_quote import SinaQuoteCollector
from jobs.features.build import build_varieties
from jobs.heartbeat import write_heartbeat
from jobs.ingest.daily import normalize_daily_bar, upsert_daily_bars
from jobs.ingest.foreign import aggregate_foreign_day, upsert_fred_series
from jobs.ingest.industry import upsert_receipts
from jobs.ingest.news import upsert_news_docs
from jobs.ingest.quotes import ensure_partitions, ingest_quotes
from jobs.news.aggregate import aggregate_sentiment
from jobs.news.llm import DeepSeekClient, load_recent_docs, run_llm_extraction
from jobs.news.pipeline import extract_recent, load_aliases
from jobs.news.revision import apply_revisions
from jobs.predict.engine import predict_varieties
from jobs.predict.score import score_outcomes
from jobs.predict.scorecard import compute_scorecard, save_scorecard_snapshot
from jobs.report.weekly import generate_weekly_report

_settings = load_settings()
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
BEIJING = dt.timezone(dt.timedelta(hours=8))


async def startup(ctx: dict[str, Any]) -> None:
    ctx["heartbeat_ttl_sec"] = _settings.worker_heartbeat_ttl_sec
    ctx["engine"] = create_engine(_settings)
    ctx["session_factory"] = create_session_factory(ctx["engine"])
    ctx["http"] = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
    redis: Redis = ctx["redis"]
    await redis.set(WORKER_HEARTBEAT_KEY, "0", ex=_settings.worker_heartbeat_ttl_sec)
    await write_heartbeat(ctx)


async def shutdown(ctx: dict[str, Any]) -> None:
    redis: Redis | None = ctx.get("redis")
    if redis is not None:
        await redis.delete(WORKER_HEARTBEAT_KEY)
    http: httpx.AsyncClient | None = ctx.get("http")
    if http is not None:
        await http.aclose()
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()


async def _last_run_at(session: AsyncSession, code: str) -> dt.datetime | None:
    raw = await session.scalar(
        text("SELECT last_run_at FROM source WHERE code = :code"), {"code": code}
    )
    return raw


async def collect_quotes(ctx: dict[str, Any]) -> dict[str, Any]:
    """每分钟一轮：新浪实时行情 → quote_1m。"""
    session_factory = ctx["session_factory"]
    collector = SinaQuoteCollector(ctx["http"])
    async with session_factory() as session, session.begin():
        last_run = await _last_run_at(session, collector.code)
        result = await collector.run(session, ingest_quotes, last_run_at=last_run)
    return {"status": result.status, "fetched": result.fetched, "inserted": result.inserted}


async def ensure_quote_partitions(ctx: dict[str, Any]) -> dict[str, Any]:
    today = dt.datetime.now(dt.UTC).date()
    days = [today + dt.timedelta(days=offset) for offset in (0, 1, 2)]
    async with ctx["session_factory"]() as session, session.begin():
        await ensure_partitions(session, days)
    return {"ensured": str(len(days))}


async def collect_daily_bars(ctx: dict[str, Any]) -> dict[str, Any]:
    """15:10 CST：21 个品种日线回补/更新，幂等。"""
    session_factory = ctx["session_factory"]
    results: dict[str, int] = {}
    errors: list[str] = []
    for variety_code, symbol in DOMESTIC_SINA_SYMBOLS.items():
        collector: BaseCollector = SinaDailyKlineCollector(ctx["http"], symbol)
        try:
            async with session_factory() as session, session.begin():
                last_run = await _last_run_at(session, collector.code)
                ingest = _daily_ingest_for(variety_code)
                result = await collector.run(session, ingest, last_run_at=last_run)
            if result.status == "error":
                errors.append(f"{variety_code}: {result.error}")
            else:
                results[variety_code] = result.inserted
        except Exception as exc:
            errors.append(f"{variety_code}: {type(exc).__name__}: {exc}")
            logger.exception("daily_bar_failed variety=%s", variety_code)
    return {"updated": results, "errors": errors}


def _daily_ingest_for(variety_code: str) -> Any:
    async def ingest(conn: Any, items: list[Any]) -> int:
        rows = [r for r in (normalize_daily_bar(variety_code, item) for item in items) if r]
        return await upsert_daily_bars(conn, rows)

    return ingest


async def collect_fred_dx(ctx: dict[str, Any]) -> dict[str, Any]:
    """08:10 CST：FRED 美元指数日线（整段幂等 upsert，替代新浪停供的 hf_DX）。"""
    session_factory = ctx["session_factory"]
    collector = FredDollarIndexCollector(ctx["http"])
    async with session_factory() as session, session.begin():
        last_run = await _last_run_at(session, collector.code)
        result = await collector.run(session, upsert_fred_series, last_run_at=last_run)
    return {"status": result.status, "fetched": result.fetched, "inserted": result.inserted}


async def collect_receipts(ctx: dict[str, Any]) -> dict[str, Any]:
    """16:40 CST：郑商所仓单日报（21 品种一次拉全，幂等）。"""
    session_factory = ctx["session_factory"]
    collector = CzceReceiptCollector(ctx["http"])
    async with session_factory() as session, session.begin():
        last_run = await _last_run_at(session, collector.code)
        result = await collector.run(session, upsert_receipts, last_run_at=last_run)
    return {
        "status": result.status,
        "day": collector.day.isoformat() if collector.day else None,
        "inserted": result.inserted,
    }


async def aggregate_foreign_daily(ctx: dict[str, Any]) -> dict[str, Any]:
    """08:10 CST：把昨日 UTC 的外盘分钟行情聚合成 foreign_daily 收盘（幂等）。"""
    yesterday = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    session_factory = ctx["session_factory"]
    async with session_factory() as session, session.begin():
        upserted = await aggregate_foreign_day(session, yesterday)
    return {"day": yesterday.isoformat(), "upserted": upserted}


async def build_snapshots(ctx: dict[str, Any]) -> dict[str, Any]:
    """08:25 CST：21 品种特征快照（预测任务的输入，幂等）。"""
    codes = list(DOMESTIC_SINA_SYMBOLS)
    failed = await build_varieties(ctx["session_factory"], codes)
    return {"failed": failed, "total": len(codes)}


async def predict_daily(ctx: dict[str, Any]) -> dict[str, Any]:
    """08:30 CST：Layer A 规则预测 → prediction（同版本幂等 upsert）。"""
    codes = list(DOMESTIC_SINA_SYMBOLS)
    result = await predict_varieties(ctx["session_factory"], codes)
    return {**result, "total": len(codes)}


async def score_daily(ctx: dict[str, Any]) -> dict[str, Any]:
    """15:20 CST：昨日及之前到期预测的事后评分（幂等，ON CONFLICT DO NOTHING）。"""
    async with ctx["session_factory"]() as session, session.begin():
        return await score_outcomes(session)


async def scorecard_daily(ctx: dict[str, Any]) -> dict[str, Any]:
    """15:30 CST：计算滚动 60d 看板并落库 scorecard_snapshot（幂等 upsert）。"""
    today = dt.datetime.now(dt.UTC).astimezone(BEIJING).date()
    async with ctx["session_factory"]() as session, session.begin():
        card = await compute_scorecard(session, days=60)
        result = await save_scorecard_snapshot(session, card, run_date=today)
    logger.info("scorecard_daily result=%s", result)
    return result


async def weekly_report(ctx: dict[str, Any]) -> dict[str, Any]:
    """周一 09:00 CST：本周 vs 上周模型表现对比，给出重训/持有建议。"""
    async with ctx["session_factory"]() as session:
        report = await generate_weekly_report(session)
    logger.info("weekly_report recommendation=%s", report.get("recommendation"))
    return report


async def train_weekly(ctx: dict[str, Any]) -> dict[str, Any]:
    """周一 03:00 CST：LightGBM 重训 + 校准 + 融合权重（达标自动 active，否则 shadow）。"""
    from jobs.train.pipeline import train

    today = dt.datetime.now(dt.UTC).date()
    model_dir = Path(_settings.model_dir)
    if not model_dir.is_absolute():
        model_dir = REPO_ROOT / model_dir
    result = await train(
        ctx["session_factory"],
        model_dir,
        since=today.replace(year=today.year - 4),
        until=today,
        today=today,
    )
    return {
        "version": result["version"],
        "status": result["status"],
        "passed": result["passed"],
        "gate": result["metrics"]["gate"],
    }


async def _run_news_collector(ctx: dict[str, Any], collector: BaseCollector) -> dict[str, Any]:
    async with ctx["session_factory"]() as session, session.begin():
        last_run = await _last_run_at(session, collector.code)
        result = await collector.run(session, upsert_news_docs, last_run_at=last_run)
    return {"status": result.status, "fetched": result.fetched, "inserted": result.inserted}


async def collect_news_jin10(ctx: dict[str, Any]) -> dict[str, Any]:
    return await _run_news_collector(ctx, Jin10FlashCollector(ctx["http"]))


async def collect_news_eastmoney(ctx: dict[str, Any]) -> dict[str, Any]:
    return await _run_news_collector(ctx, EastmoneyNewsCollector(ctx["http"]))


async def collect_news_sina(ctx: dict[str, Any]) -> dict[str, Any]:
    return await _run_news_collector(ctx, SinaNewsCollector(ctx["http"]))


async def collect_news_czce(ctx: dict[str, Any]) -> dict[str, Any]:
    return await _run_news_collector(ctx, CzceAnnouncementCollector(ctx["http"]))


async def process_news(ctx: dict[str, Any]) -> dict[str, Any]:
    """每 10 分钟：规则抽取 →（有 key）LLM 增强 → 日情绪 → 预期差修订。"""
    now = dt.datetime.now(dt.UTC)
    today = now.astimezone(BEIJING).date()
    since = now - dt.timedelta(hours=24)
    result: dict[str, Any] = {}
    async with ctx["session_factory"]() as session, session.begin():
        result.update(await extract_recent(session, since=since))
        if _settings.llm_api_key:
            docs = await load_recent_docs(session, since=since, limit=100)
            aliases = await load_aliases(session)
            client = DeepSeekClient(
                ctx["http"],
                api_key=_settings.llm_api_key,
                base_url=_settings.llm_base_url,
                model=_settings.llm_model,
            )
            stats = await run_llm_extraction(
                session,
                docs,
                client,
                model=_settings.llm_model,
                valid_codes=set(aliases),
                budget_cny=_settings.llm_daily_budget_cny,
                price_in_per_1k=_settings.llm_price_in_cny_per_1k,
                price_out_per_1k=_settings.llm_price_out_cny_per_1k,
                now=now,
            )
            result["llm"] = {
                "calls": stats.calls,
                "cache_hits": stats.cache_hits,
                "cache_hit_rate": stats.cache_hit_rate,
                "cost_cny": stats.cost_cny,
                "degraded": stats.degraded,
            }
        for day in (today - dt.timedelta(days=1), today):
            await aggregate_sentiment(session, day)
        revision = await apply_revisions(session, today, now=now)
        result["revisions"] = revision.created
        result["alerts"] = revision.alerts
    return result


class WorkerSettings:
    functions: ClassVar[list[Any]] = [
        write_heartbeat,
        collect_quotes,
        ensure_quote_partitions,
        collect_daily_bars,
        aggregate_foreign_daily,
        collect_fred_dx,
        collect_receipts,
        build_snapshots,
        predict_daily,
        score_daily,
        scorecard_daily,
        weekly_report,
        train_weekly,
        collect_news_jin10,
        collect_news_eastmoney,
        collect_news_sina,
        collect_news_czce,
        process_news,
    ]
    cron_jobs: ClassVar[list[Any]] = [
        # 心跳，每 30 秒；run_at_startup 保证启动即有心跳
        cron(write_heartbeat, second={0, 30}, run_at_startup=True),
        # 实时行情：每分钟
        cron(collect_quotes, second={0}, run_at_startup=True),
        # 分区预建：00:05 CST
        cron(ensure_quote_partitions, hour={16}, minute={5}, run_at_startup=True),
        # 日线：15:10 CST（rate-guard 保证一天只真正跑一轮）
        cron(collect_daily_bars, hour={7}, minute={10}, run_at_startup=True),
        # 外盘日线聚合：08:10 CST（聚合昨日 UTC）
        cron(aggregate_foreign_daily, hour={0}, minute={10}, run_at_startup=True),
        # FRED 美元指数：08:10 CST（整段回补，幂等）
        cron(collect_fred_dx, hour={0}, minute={10}, run_at_startup=True),
        # 郑商所仓单：16:40 CST（收盘后文件发布）
        cron(collect_receipts, hour={8}, minute={40}, run_at_startup=True),
        # 特征快照：08:25 CST（08:30 预测前完成）
        cron(build_snapshots, hour={0}, minute={25}, run_at_startup=False),
        # 规则预测：08:30 CST（快照之后；同版本幂等）
        cron(predict_daily, hour={0}, minute={30}, run_at_startup=False),
        # 事后评分：15:20 CST（日线入库后；对昨日及更早到期预测评分，幂等）
        cron(score_daily, hour={7}, minute={20}, run_at_startup=False),
        # Scorecard 快照：15:30 CST（评分后；滚动 60d 看板落库，幂等 upsert）
        cron(scorecard_daily, hour={7}, minute={30}, run_at_startup=False),
        # 周报：周一 09:00 CST（本周 vs 上周，给出重训/持有建议）
        cron(weekly_report, weekday={0}, hour={1}, minute={0}, run_at_startup=False),
        # GBM 周度重训：周一 03:00 CST = 周日 19:00 UTC（数据到上周五收盘）
        cron(train_weekly, weekday={6}, hour={19}, minute={0}, run_at_startup=False),
        # 资讯采集：金十 60s；东财/新浪 120s；郑商所公告 30min（尽力，失败不阻塞）
        cron(collect_news_jin10, second={0}, run_at_startup=False),
        cron(collect_news_eastmoney, minute=set(range(0, 60, 2)), run_at_startup=False),
        cron(collect_news_sina, minute=set(range(0, 60, 2)), run_at_startup=False),
        cron(collect_news_czce, minute={0, 30}, run_at_startup=False),
        # 资讯处理：每 10 分钟（抽取 → 情绪 → 预期差修订）
        cron(process_news, minute=set(range(0, 60, 10)), run_at_startup=False),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    max_jobs = 5
    job_timeout = 600
    keep_result = 3600


if __name__ == "__main__":
    from typing import cast

    from arq import run_worker

    run_worker(cast(Any, WorkerSettings))
