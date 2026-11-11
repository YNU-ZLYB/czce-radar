"""可选 LLM 抽取（DeepSeek/OpenAI 兼容）+ 成本治理。

铁律：
- evidence 必须能在原文中找到，否则丢弃（防幻觉，M5 DoD ①）
- 单日预算硬约束：已花费 + 预留 > 预算 → 不再调用，当日降级为规则抽取
- 每次调用（含缓存命中）都写 llm_call_log，便于统计成本与命中率
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from jobs.news.extract import RuleExtract
from jobs.news.pipeline import insert_extracts

logger = logging.getLogger(__name__)

BEIJING = dt.timezone(dt.timedelta(hours=8))
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
MAX_COMPLETION_TOKENS = 800

SYSTEM_PROMPT = (
    "你是期货资讯结构化抽取器。只从给定原文抽取事实，不得编造。"
    '输出 JSON 对象：{"extracts":[{"variety_code":"MA","direction":-1,"strength":3,'
    '"chain_stage":"upstream","is_surprise":false,"confidence":0.6,'
    '"evidence":"原文中的一句话"}]}。'
    "direction 只能是 -1/0/1；strength 1-5；chain_stage 只能是"
    " upstream/midstream/downstream/demand 或 null；"
    "evidence 必须逐字出现在原文中，否则该条作废。只输出 JSON。"
)


@dataclass(slots=True)
class DocRef:
    id: int
    title: str
    content: str | None


@dataclass(slots=True)
class LLMStats:
    calls: int = 0
    cache_hits: int = 0
    dropped: int = 0
    cost_cny: float = 0.0
    degraded: bool = False

    @property
    def cache_hit_rate(self) -> float:
        total = self.calls + self.cache_hits
        return round(self.cache_hits / total, 3) if total else 0.0


class LLMClient(Protocol):
    async def extract(self, title: str, content: str | None) -> tuple[str, int, int]:
        """返回 (模型原始输出, prompt_tokens, completion_tokens)。"""


class DeepSeekClient:
    """OpenAI 兼容 Chat Completions 客户端（默认 DeepSeek）。"""

    def __init__(
        self, http: httpx.AsyncClient, *, api_key: str, base_url: str, model: str
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model

    async def extract(self, title: str, content: str | None) -> tuple[str, int, int]:
        user = f"标题：{title}\n正文：{content or ''}"
        response = await self._http.post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "temperature": 0,
                "max_tokens": MAX_COMPLETION_TOKENS,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ],
            },
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        content_out = str(data["choices"][0]["message"]["content"])
        usage: dict[str, Any] = data.get("usage") or {}
        return content_out, int(usage.get("prompt_tokens") or 0), int(
            usage.get("completion_tokens") or 0
        )


def _json_block(raw: str) -> Any:
    cleaned = _FENCE_RE.sub("", raw).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None


def parse_llm_extracts(
    raw: str, title: str, content: str | None, valid_codes: set[str]
) -> tuple[list[RuleExtract], int]:
    """解析并校验 LLM 输出；evidence 不在原文 → 丢弃并计数。"""
    payload = _json_block(raw)
    items = payload.get("extracts") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return [], 0
    body = f"{title}\n{content or ''}"
    extracts: list[RuleExtract] = []
    dropped = 0
    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        code = str(item.get("variety_code") or "")
        evidence = str(item.get("evidence") or "").strip()
        raw_direction = item.get("direction")
        raw_strength = item.get("strength")
        try:
            direction = int(raw_direction) if raw_direction is not None else 0
            strength = int(raw_strength) if raw_strength is not None else 0
            confidence = float(item.get("confidence") or 0.5)
        except (TypeError, ValueError):
            dropped += 1
            continue
        if (
            code not in valid_codes
            or direction not in (-1, 0, 1)
            or not 1 <= strength <= 5
            or not evidence
            or evidence not in body
        ):
            dropped += 1
            continue
        stage = item.get("chain_stage")
        extracts.append(
            RuleExtract(
                variety_code=code,
                direction=direction,
                strength=strength,
                chain_stage=str(stage) if stage in {
                    "upstream", "midstream", "downstream", "demand"
                } else None,
                is_surprise=bool(item.get("is_surprise")),
                confidence=round(min(0.95, max(0.1, confidence)), 2),
                evidence=evidence,
            )
        )
    return extracts, dropped


async def load_recent_docs(
    conn: AsyncConnection, *, since: dt.datetime, limit: int = 200
) -> list[DocRef]:
    rows = (
        await conn.execute(
            text(
                "SELECT id, title, content FROM news_doc WHERE published_at >= :since"
                " ORDER BY published_at DESC LIMIT :limit"
            ),
            {"since": since, "limit": limit},
        )
    ).all()
    return [DocRef(id=row.id, title=row.title, content=row.content) for row in rows]


def _day_bounds(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    local = now.astimezone(BEIJING)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(dt.UTC), (start + dt.timedelta(days=1)).astimezone(dt.UTC)


async def _today_spend(conn: AsyncConnection, now: dt.datetime) -> tuple[float, int]:
    start, end = _day_bounds(now)
    row = (
        await conn.execute(
            text(
                "SELECT COALESCE(sum(cost_cny), 0) AS cost, count(*) AS calls"
                " FROM llm_call_log WHERE created_at >= :start AND created_at < :end"
            ),
            {"start": start, "end": end},
        )
    ).first()
    if row is None:
        return 0.0, 0
    return float(row.cost or 0), int(row.calls or 0)


async def _log_call(
    conn: AsyncConnection,
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost: float,
    cache_hit: bool,
    latency_ms: int,
) -> None:
    await conn.execute(
        text(
            "INSERT INTO llm_call_log (purpose, model, prompt_tokens, completion_tokens,"
            " cost_cny, cache_hit, latency_ms) VALUES ('extract_event', :model, :pt, :ct,"
            " :cost, :hit, :latency)"
        ),
        {
            "model": model,
            "pt": prompt_tokens,
            "ct": completion_tokens,
            "cost": round(cost, 4),
            "hit": cache_hit,
            "latency": latency_ms,
        },
    )


async def run_llm_extraction(
    conn: AsyncConnection,
    docs: list[DocRef],
    client: LLMClient,
    *,
    model: str,
    valid_codes: set[str],
    budget_cny: float = 5.0,
    price_in_per_1k: float = 0.001,
    price_out_per_1k: float = 0.002,
    now: dt.datetime | None = None,
) -> LLMStats:
    """对给定原文做 LLM 抽取；预算耗尽后立即停止并标记 degraded。"""
    now = now or dt.datetime.now(dt.UTC)
    stats = LLMStats()
    spent, prior_calls = await _today_spend(conn, now)
    stats.cost_cny = spent
    for doc in docs:
        existing = await conn.scalar(
            text(
                "SELECT 1 FROM news_extract WHERE doc_id = :id AND method = 'llm' LIMIT 1"
            ),
            {"id": doc.id},
        )
        if existing:
            stats.cache_hits += 1
            await _log_call(
                conn,
                model=model,
                prompt_tokens=0,
                completion_tokens=0,
                cost=0.0,
                cache_hit=True,
                latency_ms=0,
            )
            continue
        prompt_estimate = (len(doc.title) + len(doc.content or "")) / 1000 * price_in_per_1k
        estimate = prompt_estimate + MAX_COMPLETION_TOKENS / 1000 * price_out_per_1k
        avg_cost = spent / prior_calls if prior_calls else 0.0
        reserve = max(estimate, avg_cost)
        if spent + reserve > budget_cny:
            stats.degraded = True
            logger.warning(
                "llm_budget_exceeded spent=%.4f reserve=%.4f budget=%.2f",
                spent,
                reserve,
                budget_cny,
            )
            break
        started = time.perf_counter()
        try:
            raw, prompt_tokens, completion_tokens = await client.extract(doc.title, doc.content)
        except Exception:
            logger.warning("llm_extract_failed doc_id=%s", doc.id, exc_info=True)
            stats.degraded = True
            continue
        latency = int((time.perf_counter() - started) * 1000)
        cost = prompt_tokens / 1000 * price_in_per_1k + completion_tokens / 1000 * price_out_per_1k
        spent += cost
        prior_calls += 1
        stats.calls += 1
        stats.cost_cny = round(spent, 4)
        extracts, dropped = parse_llm_extracts(raw, doc.title, doc.content, valid_codes)
        stats.dropped += dropped
        await insert_extracts(conn, doc.id, extracts, method="llm", model=model)
        await _log_call(
            conn,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            cache_hit=False,
            latency_ms=latency,
        )
    return stats
