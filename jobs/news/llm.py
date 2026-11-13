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
    "只关注郑商所 21 个期货品种；其它品种一律忽略。"
    '输出 JSON：{"extracts":[{"variety_code":"MA","direction":-1,"strength":3,'
    '"chain_stage":"upstream","is_surprise":false,"confidence":0.6,'
    '"evidence":"原文中逐字出现的一句话"}]}。'
    "字段约束：direction 只能 -1/0/1；strength 1-5；chain_stage 只能"
    " upstream/midstream/downstream/demand 或 null；evidence 必须逐字出现在原文中。"
    "若原文与 21 个品种的供需/成本/库存/装置/政策无明显关联，输出空列表。"
    "只输出 JSON，不要解释。"
)

# 每个品种的产业链提示，用于帮助模型做间接推理（如"煤价→甲醇成本"）
VARIETY_HINTS: dict[str, str] = {
    "MA": "甲醇；上游：动力煤/天然气/焦炉气；下游：甲醛/醋酸/MTO制烯烃",
    "TA": "PTA 精对苯二甲酸；上游：PX/原油；下游：聚酯/涤纶长丝/瓶片",
    "PX": "对二甲苯；上游：原油/石脑油；下游：PTA",
    "PR": "瓶片 PET；上游：PTA/乙二醇；下游：饮料包装/再生聚酯",
    "PF": "涤纶短纤；上游：PTA/乙二醇；下游：纺纱/无纺布",
    "SH": "烧碱；上游：原盐/电力；下游：氧化铝/化工/造纸",
    "UR": "尿素；上游：煤炭/天然气/合成氨；下游：农业施肥/复合肥/人造板",
    "SA": "纯碱；上游：原盐/石灰石/合成氨；下游：玻璃/日用玻璃/光伏玻璃",
    "FG": "玻璃；上游：纯碱/天然气/石英砂；下游：房地产竣工/光伏",
    "PL": "丙烯；上游：原油/炼厂/MTO；下游：聚丙烯/丙烯酸/环氧丙烷",
    "OI": "菜油；上游：菜籽；下游：食用油消费/生物柴油",
    "RM": "菜粕；上游：菜籽；下游：水产饲料/畜禽饲料",
    "RS": "菜籽；上游：种植/进口；下游：菜油压榨/菜粕",
    "PK": "花生；上游：种植/天气；下游：花生油/花生粕",
    "SR": "白糖；上游：甘蔗/甜菜种植；下游：食品/饮料",
    "CF": "棉花；上游：种植/天气/进口；下游：纺纱/坯布",
    "CY": "棉纱；上游：棉花；下游：棉布/针织",
    "SF": "硅铁；上游：硅石/焦炭/电力；下游：钢铁（脱氧剂）",
    "SM": "锰硅；上游：锰矿/焦炭/电力；下游：钢铁",
    "AP": "苹果；上游：种植/天气；下游：鲜果消费",
    "CJ": "红枣；上游：种植/天气；下游：干果消费",
}

FEW_SHOT = (
    "\n示例1（成本传导，间接推理）：\n"
    "原文：动力煤主力合约单日上涨 3%，内蒙古主产区外运受限。\n"
    '输出：{"extracts":[{"variety_code":"MA","direction":1,"strength":2,'
    '"chain_stage":"upstream","is_surprise":false,"confidence":0.55,'
    '"evidence":"动力煤主力合约单日上涨 3%"}]}\n'
    "理由：动力煤是甲醇（MA）的上游原料，煤价上涨推高甲醇生产成本。\n\n"
    "示例2（供应收缩，直接命中）：\n"
    "原文：某 PTA 大厂年产 200 万吨装置宣布检修 15 天。\n"
    '输出：{"extracts":[{"variety_code":"TA","direction":1,"strength":3,'
    '"chain_stage":"midstream","is_surprise":true,"confidence":0.70,'
    '"evidence":"年产 200 万吨装置宣布检修 15 天"}]}\n'
    "理由：PTA 装置检修减少供应，利多 PTA（TA）。\n"
)

# 行业事件关键词：即便没出现品种名，出现这些词也可能影响品种供需
INDUSTRY_KEYWORDS: tuple[str, ...] = (
    "装置", "检修", "开工率", "复产", "停产", "产能", "产量",
    "库存", "仓单", "基差", "现货", "现货价",
    "进口", "出口", "关税", "到港", "外运", "船期",
    "天气", "干旱", "洪涝", "霜冻", "台风",
    "政策", "补贴", "收储", "抛储", "限产", "去产能",
    "涨价", "跌价", "上调", "下调", "供应", "需求", "消费",
    "原油", "天然气", "动力煤", "焦煤", "甲醇", "乙烯", "丙烯",
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
    skipped: int = 0  # 关键词预筛后跳过（非相关）
    dropped: int = 0
    cost_cny: float = 0.0
    degraded: bool = False

    @property
    def cache_hit_rate(self) -> float:
        total = self.calls + self.cache_hits
        return round(self.cache_hits / total, 3) if total else 0.0


class LLMClient(Protocol):
    async def extract(
        self, title: str, content: str | None, *, system_extra: str = ""
    ) -> tuple[str, int, int]:
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

    async def extract(
        self, title: str, content: str | None, *, system_extra: str = ""
    ) -> tuple[str, int, int]:
        user = f"标题：{title}\n正文：{content or ''}"
        system = SYSTEM_PROMPT
        if system_extra:
            system = f"{SYSTEM_PROMPT}\n\n{system_extra}{FEW_SHOT}"
        response = await self._http.post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "temperature": 0,
                "max_tokens": MAX_COMPLETION_TOKENS,
                "messages": [
                    {"role": "system", "content": system},
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


def _build_variety_block(aliases: dict[str, tuple[str, ...]]) -> str:
    """组装给 LLM 的品种清单：代码 + 名称 + 产业链提示。"""
    lines: list[str] = []
    for code, names in aliases.items():
        hint = VARIETY_HINTS.get(code, "")
        name = names[0] if names else code
        suffix = f"；{hint}" if hint else ""
        lines.append(f"- {code}（{name}）{suffix}")
    return "可抽取的 21 个品种（variety_code 必须从中选取）：\n" + "\n".join(lines) + "\n"


def _doc_relevant(title: str, content: str | None, aliases: dict[str, tuple[str, ...]]) -> bool:
    """关键词预筛：命中任一品种别名或行业事件词才送 LLM。"""
    body = f"{title}\n{content or ''}"
    if any(kw in body for kw in INDUSTRY_KEYWORDS):
        return True
    for names in aliases.values():
        if any(name in body for name in names if name):
            return True
    return False


async def run_llm_extraction(
    conn: AsyncConnection,
    docs: list[DocRef],
    client: LLMClient,
    *,
    model: str,
    aliases: dict[str, tuple[str, ...]],
    budget_cny: float = 5.0,
    price_in_per_1k: float = 0.001,
    price_out_per_1k: float = 0.002,
    now: dt.datetime | None = None,
) -> LLMStats:
    """对给定原文做 LLM 抽取；预算耗尽后立即停止并标记 degraded。

    优化：
    - 关键词预筛：非相关文档直接跳过，不浪费 token
    - 负结果缓存：同一文档同一模型只调一次（写入 llm_doc_state）
    """
    now = now or dt.datetime.now(dt.UTC)
    stats = LLMStats()
    valid_codes = set(aliases)
    variety_block = _build_variety_block(aliases)
    spent, prior_calls = await _today_spend(conn, now)
    stats.cost_cny = spent
    for doc in docs:
        if not _doc_relevant(doc.title, doc.content, aliases):
            stats.skipped += 1
            continue
        # 已有抽取结果（任意 method）或负结果缓存，都跳过
        done = await conn.scalar(
            text(
                "SELECT 1 FROM llm_doc_state WHERE doc_id = :id AND model = :model LIMIT 1"
            ),
            {"id": doc.id, "model": model},
        )
        if done:
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
            raw, prompt_tokens, completion_tokens = await client.extract(
                doc.title, doc.content, system_extra=variety_block
            )
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
        # 负结果缓存：无论是否抽到，都记录该文档已处理，避免重复调用
        await conn.execute(
            text(
                "INSERT INTO llm_doc_state (doc_id, model, kept, dropped) VALUES"
                " (:id, :model, :kept, :dropped) ON CONFLICT (doc_id, model) DO NOTHING"
            ),
            {"id": doc.id, "model": model, "kept": len(extracts), "dropped": dropped},
        )
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
