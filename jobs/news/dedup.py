"""标题近似去重：二元组 Jaccard 相似度。

跨源同一事件标题往往只有个别字不同；content_hash 只能抓完全相同的文本，
因此入库前再用二元组 Jaccard ≥ 0.9 兜一层（《03》M5 交付物 2）。
"""

from __future__ import annotations

import re

_STRIP_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize_title(title: str) -> str:
    return _STRIP_RE.sub("", title).lower()


def bigrams(title: str) -> set[str]:
    text = normalize_title(title)
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def jaccard(a: str, b: str) -> float:
    ga, gb = bigrams(a), bigrams(b)
    if not ga or not gb:
        return 1.0 if normalize_title(a) == normalize_title(b) else 0.0
    union = ga | gb
    return len(ga & gb) / len(union) if union else 0.0


def is_duplicate(title: str, existing: list[str], threshold: float = 0.9) -> bool:
    return any(jaccard(title, other) >= threshold for other in existing)
