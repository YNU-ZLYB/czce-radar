"""品种 ↔ 数据源符号映射（采集与归一化的唯一依据）。

数据来自 2026-11-10 对 hq.sinajs.cn 的实测：
- 国内主力连续：``nf_{CODE}0``，GBK 编码，19+ 字段
- 外盘：``hf_{EXT}``，15 字段（0 现价 / 4 最高 / 5 最低 / 6 时间 / 7 昨结算
  / 8 开盘 / 9 持仓 / 12 日期 / 13 名称 / 14 成交量），时间为交易所当地时刻
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class ForeignSymbol:
    ext_code: str
    sina_symbol: str
    name: str
    tz: ZoneInfo
    source: str = "sina"


DOMESTIC_SINA_SYMBOLS: dict[str, str] = {
    # variety_code -> sina 主力连续代码（nf_ 前缀在采集器拼接）
    "MA": "MA0",
    "TA": "TA0",
    "PX": "PX0",
    "PR": "PR0",
    "PF": "PF0",
    "SH": "SH0",
    "UR": "UR0",
    "SA": "SA0",
    "FG": "FG0",
    "PL": "PL0",
    "OI": "OI0",
    "RM": "RM0",
    "RS": "RS0",
    "PK": "PK0",
    "SR": "SR0",
    "CF": "CF0",
    "CY": "CY0",
    "SF": "SF0",
    "SM": "SM0",
    "AP": "AP0",
    "CJ": "CJ0",
}

FOREIGN_SYMBOLS: dict[str, ForeignSymbol] = {
    "OIL": ForeignSymbol("OIL", "hf_OIL", "布伦特原油", ZoneInfo("Europe/London")),
    "CT": ForeignSymbol("CT", "hf_CT", "ICE美棉", ZoneInfo("America/New_York")),
    "S": ForeignSymbol("S", "hf_S", "CBOT大豆", ZoneInfo("America/Chicago")),
    "SM": ForeignSymbol("SM", "hf_SM", "CBOT豆粕", ZoneInfo("America/Chicago")),
    "BO": ForeignSymbol("BO", "hf_BO", "CBOT豆油", ZoneInfo("America/Chicago")),
    "C": ForeignSymbol("C", "hf_C", "CBOT玉米", ZoneInfo("America/Chicago")),
    "W": ForeignSymbol("W", "hf_W", "CBOT小麦", ZoneInfo("America/Chicago")),
    "NG": ForeignSymbol("NG", "hf_NG", "NYMEX天然气", ZoneInfo("America/New_York")),
    "HG": ForeignSymbol("HG", "hf_HG", "COMEX铜", ZoneInfo("America/New_York")),
    # 2026-11-10 实测：hf_DX 返回空（新浪已停供），改为 FRED 广义美元指数
    # DTWEXBGS 日频（jobs/collectors/fred.py），仅日线、无盘中价
    "DX": ForeignSymbol("DX", "hf_DX", "美元指数", ZoneInfo("America/New_York"), source="fred"),
}

#: 可由新浪实时接口采集的外盘代码（source="sina"）
SINA_FOREIGN_CODES: tuple[str, ...] = tuple(
    code for code, info in FOREIGN_SYMBOLS.items() if info.source == "sina"
)
