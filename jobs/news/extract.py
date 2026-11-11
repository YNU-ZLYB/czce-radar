"""资讯规则抽取（method='rule'）。

硬约束（《02》§7 / M5 DoD ①）：evidence 必须是原文子串，否则该条抽取作废。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jobs.news.dictionary import (
    BEARISH_KEYWORDS,
    BULLISH_KEYWORDS,
    is_surprise_text,
    match_stage,
    mention_allowed,
)

_SENTENCE_RE = re.compile(r"[。！？!?；;\n]+")


@dataclass(slots=True)
class RuleExtract:
    variety_code: str
    direction: int
    strength: int
    chain_stage: str | None
    is_surprise: bool
    confidence: float
    evidence: str


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_RE.split(text) if part.strip()]


def _direction_score(sentence: str) -> int:
    pos = sum(1 for word in BULLISH_KEYWORDS if word in sentence)
    neg = sum(1 for word in BEARISH_KEYWORDS if word in sentence)
    return pos - neg


def _strength(abs_score: int) -> int:
    if abs_score >= 8:
        return 5
    if abs_score >= 6:
        return 4
    if abs_score >= 4:
        return 3
    if abs_score >= 2:
        return 2
    return 1


def extract_rule(
    title: str,
    content: str | None,
    aliases: dict[str, tuple[str, ...]],
) -> list[RuleExtract]:
    """按品种聚合句子，输出方向/强度/环节/预期差/原文依据。

    aliases: code → 关键词元组。一个文档同一品种只产出一条（便于幂等与聚合）。
    """
    text = f"{title}\n{content or ''}".strip()
    sentences = split_sentences(text)
    if not sentences:
        return []

    best: dict[str, tuple[int, str, int]] = {}  # code → (净分, 证据句, 命中句数)
    for code, words in aliases.items():
        score = 0
        hits = 0
        evidence = ""
        evidence_abs = -1
        for sentence in sentences:
            if not any(word in sentence for word in words):
                continue
            if not mention_allowed(code, sentence):
                continue
            hits += 1
            delta = _direction_score(sentence)
            score += delta
            if abs(delta) > evidence_abs:
                evidence_abs = abs(delta)
                evidence = sentence
        if hits == 0:
            continue
        if evidence not in text:  # 防幻觉：证据必须逐字出现在原文
            continue
        best[code] = (score, evidence, hits)

    extracts: list[RuleExtract] = []
    for code, (score, evidence, _) in best.items():
        direction = 0 if score == 0 else (1 if score > 0 else -1)
        strength = _strength(abs(score))
        extracts.append(
            RuleExtract(
                variety_code=code,
                direction=direction,
                strength=strength,
                chain_stage=match_stage(evidence),
                is_surprise=is_surprise_text(evidence),
                confidence=round(min(0.9, 0.4 + 0.1 * strength), 2),
                evidence=evidence,
            )
        )
    return extracts
