"""跨进程共享常量。"""

from __future__ import annotations

WORKER_HEARTBEAT_KEY = "czce:worker:heartbeat"

SECTOR_ORDER: tuple[str, ...] = ("能源化工", "油脂油料", "软商品", "黑色", "农副")
DELISTED_SECTOR = "已下市"
