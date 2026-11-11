"""资讯关键词词典：品种别名、方向词、预期差词、产业链环节词。

规则抽取（method='rule'）是主路径：无需外部服务、可回溯、零成本；
LLM 抽取只是可选增强（见 jobs/news/llm.py）。
"""

from __future__ import annotations

# 品种代码 → 关键词别名（含常用简称/俗称）。官方名称由 variety 表补充。
VARIETY_ALIASES: dict[str, tuple[str, ...]] = {
    "MA": ("甲醇", "郑醇", "煤制甲醇"),
    "TA": ("PTA", "精对苯二甲酸"),
    "PX": ("PX", "对二甲苯"),
    "PR": ("瓶片", "聚酯瓶片"),
    "PF": ("短纤", "涤纶短纤"),
    "SH": ("烧碱", "片碱", "液碱"),
    "UR": ("尿素", "尿基"),
    "SA": ("纯碱", "重碱", "轻碱"),
    "FG": ("玻璃", "浮法玻璃"),
    "PL": ("丙烯", "聚丙烯"),
    "OI": ("菜油", "菜籽油"),
    "RM": ("菜粕", "菜籽粕"),
    "RS": ("菜籽", "油菜籽"),
    "PK": ("花生", "花生仁"),
    "SR": ("白糖", "食糖", "原糖", "蔗糖"),
    "CF": ("棉花", "棉价", "皮棉"),
    "CY": ("棉纱",),
    "SF": ("硅铁",),
    "SM": ("锰硅",),
    "AP": ("苹果", "红富士"),
    "CJ": ("红枣", "灰枣"),
}

BULLISH_KEYWORDS: tuple[str, ...] = (
    "上涨", "大涨", "走高", "拉升", "涨超", "涨停", "利多", "利好", "支撑",
    "去库", "去库存", "减产", "限产", "检修", "停产", "供应紧张", "短缺",
    "需求旺盛", "需求回暖", "补库", "提价", "涨价", "上调", "超预期增长",
    "涨多跌少", "高开", "收涨", "走强", "上行", "涨幅",
)

BEARISH_KEYWORDS: tuple[str, ...] = (
    "下跌", "大跌", "走低", "跳水", "跌停", "利空", "压制", "累库", "累库存",
    "增产", "复产", "投产", "供应宽松", "供应增加", "需求疲软", "需求低迷",
    "降价", "下调", "过剩", "库存高企", "需求转弱",
    "跌多涨少", "低开", "收跌", "走弱", "下行", "跌幅",
)

# 同名歧义：科技股新闻里的「苹果」不是郑商所苹果期货
AMBIGUOUS_CONTEXT: dict[str, tuple[str, ...]] = {
    "AP": ("美股", "纳斯达克", "纳指", "英伟达", "iPhone", "苹果公司", "库克", "股价", "科技股"),
}
AMBIGUOUS_OVERRIDE: dict[str, tuple[str, ...]] = {
    "AP": ("期货", "现货", "产区", "果农", "冷库", "纸袋", "郑商所", "红枣", "苹果期货"),
}

SURPRISE_KEYWORDS: tuple[str, ...] = (
    "超预期", "不及预期", "意外", "大幅高于预期", "大幅低于预期",
    "远高于预期", "远低于预期", "爆冷", "出乎意料", "远超预期",
)

STAGE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "upstream": ("原料", "成本", "原油", "煤炭", "天然气", "供应", "减产", "检修", "限产", "开工"),
    "midstream": ("装置", "生产", "加工", "产能", "负荷"),
    "downstream": ("下游", "需求", "采购", "订单", "消费", "终端"),
    "demand": ("库存", "港口", "出口", "进口", "基差", "现货"),
}


def aliases_for(code: str, official_name: str | None = None) -> tuple[str, ...]:
    extra = (official_name,) if official_name else ()
    return tuple(dict.fromkeys((*VARIETY_ALIASES.get(code, ()), *extra)))


def match_stage(sentence: str) -> str | None:
    for stage, words in STAGE_KEYWORDS.items():
        if any(word in sentence for word in words):
            return stage
    return None


def is_surprise_text(sentence: str) -> bool:
    return any(word in sentence for word in SURPRISE_KEYWORDS)


def mention_allowed(code: str, sentence: str) -> bool:
    """过滤同名歧义（如科技股新闻中的「苹果」）。"""
    ambiguous = AMBIGUOUS_CONTEXT.get(code)
    if not ambiguous:
        return True
    if not any(word in sentence for word in ambiguous):
        return True
    return any(word in sentence for word in AMBIGUOUS_OVERRIDE.get(code, ()))
