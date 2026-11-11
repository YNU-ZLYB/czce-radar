"""品种种子数据（冻结数据，仅供迁移与测试读取，不承载业务逻辑）。

21 个可交易品种 + 2 个已下市品种（动力煤 ZC / 小麦 WH，保留主数据但不预测）。
修改本文件等于修改历史迁移的输入，必须新增迁移而不是直接改动。
"""

from __future__ import annotations

from dataclasses import dataclass

from czce_schema.constants import DELISTED_SECTOR


@dataclass(frozen=True, slots=True)
class VarietySeed:
    code: str
    name: str
    sector: str
    sort_order: int
    is_tradable: bool = True
    note: str | None = None


VARIETY_SEED: tuple[VarietySeed, ...] = (
    # 能源化工（10）
    VarietySeed("MA", "甲醇", "能源化工", 10),
    VarietySeed("TA", "PTA", "能源化工", 20),
    VarietySeed("PX", "对二甲苯", "能源化工", 30),
    VarietySeed("PR", "瓶片", "能源化工", 40),
    VarietySeed("PF", "短纤", "能源化工", 50),
    VarietySeed("SH", "烧碱", "能源化工", 60),
    VarietySeed("UR", "尿素", "能源化工", 70),
    VarietySeed("SA", "纯碱", "能源化工", 80),
    VarietySeed("FG", "玻璃", "能源化工", 90),
    VarietySeed("PL", "丙烯", "能源化工", 100),
    # 油脂油料（4）
    VarietySeed("OI", "菜油", "油脂油料", 110),
    VarietySeed("RM", "菜粕", "油脂油料", 120),
    VarietySeed("RS", "菜籽", "油脂油料", 130),
    VarietySeed("PK", "花生", "油脂油料", 140),
    # 软商品（3）
    VarietySeed("SR", "白糖", "软商品", 150),
    VarietySeed("CF", "棉花", "软商品", 160),
    VarietySeed("CY", "棉纱", "软商品", 170),
    # 黑色（2）
    VarietySeed("SF", "硅铁", "黑色", 180),
    VarietySeed("SM", "锰硅", "黑色", 190),
    # 农副（2）
    VarietySeed("AP", "苹果", "农副", 200),
    VarietySeed("CJ", "红枣", "农副", 210),
    # 已下市 / 不可交易（2）：保留主数据，首页不展示，不生成预测
    VarietySeed(
        "ZC",
        "动力煤",
        DELISTED_SECTOR,
        900,
        is_tradable=False,
        note="已停止交易，仅保留历史主数据",
    ),
    VarietySeed(
        "WH",
        "小麦",
        DELISTED_SECTOR,
        910,
        is_tradable=False,
        note="已停止交易，仅保留历史主数据",
    ),
)

TRADABLE_CODES: frozenset[str] = frozenset(v.code for v in VARIETY_SEED if v.is_tradable)
