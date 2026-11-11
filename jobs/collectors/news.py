"""资讯采集器：金十 60s / 东财 120s / 新浪 7×24 120s / 郑商所公告 30min。

约定：只负责拿到 RawItem；清洗、去重、入库在 jobs/ingest/news.py。
网络/接口变化一律由 BaseCollector 的重试与 fetch_log 兜底，不向上抛异常。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Any, ClassVar
from urllib.parse import urljoin

from jobs.collectors.base import BaseCollector, RawItem

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

BEIJING = dt.timezone(dt.timedelta(hours=8))


def _text(value: str | None) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub("", value or "")).strip()


def _cst(value: str | None) -> dt.datetime | None:
    """``YYYY-MM-DD HH:MM:SS``（北京时间）→ UTC；解析失败返回 None。"""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(value.strip(), fmt).replace(tzinfo=BEIJING)
            return parsed.astimezone(dt.UTC)
        except ValueError:
            continue
    return None


class Jin10FlashCollector(BaseCollector):
    """金十数据 7×24 快讯（flash-api）。"""

    code: ClassVar[str] = "jin10_flash"
    name: ClassVar[str] = "金十快讯"
    kind: ClassVar[str] = "news"
    interval_sec: ClassVar[int] = 60

    URL: ClassVar[str] = "https://flash-api.jin10.com/get_flash_list?channel=-8200&vip=1"
    HEADERS: ClassVar[dict[str, str]] = {
        "x-app-id": "bVBF4FyRTn5NJF5n",
        "x-version": "1.0.0",
        "User-Agent": "Mozilla/5.0",
    }

    async def fetch(self) -> list[RawItem]:
        response = await self._http.get(self.URL, headers=self.HEADERS)
        response.raise_for_status()
        return parse_jin10(response.json())


def parse_jin10(payload: Any) -> list[RawItem]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    items: list[RawItem] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        nested = row.get("data")
        body: dict[str, Any] = nested if isinstance(nested, dict) else row
        raw_content = body.get("content") or body.get("title") or row.get("content") or ""
        content = _text(str(raw_content))
        external_id = str(row.get("id") or "").strip()
        if not content or not external_id:
            continue
        title = _text(str(body.get("title") or "")) or content[:80]
        items.append(
            RawItem(
                source=Jin10FlashCollector.code,
                external_id=external_id,
                ts=_cst(row.get("time")),
                payload={
                    "title": title,
                    "content": content,
                    "url": f"https://flash.jin10.com/detail/{external_id}",
                },
            )
        )
    return items


class EastmoneyNewsCollector(BaseCollector):
    """东方财富财经快讯。"""

    code: ClassVar[str] = "eastmoney_news"
    name: ClassVar[str] = "东方财富快讯"
    kind: ClassVar[str] = "news"
    interval_sec: ClassVar[int] = 120

    URL: ClassVar[str] = (
        "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html"
    )

    async def fetch(self) -> list[RawItem]:
        response = await self._http.get(self.URL)
        response.raise_for_status()
        return parse_eastmoney(response.content.decode("utf-8", errors="replace"))


def parse_eastmoney(body: str) -> list[RawItem]:
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return []
    lives = payload.get("Lives") or payload.get("LivesList") or payload.get("lives") or []
    if not isinstance(lives, list):
        return []
    items: list[RawItem] = []
    for row in lives:
        if not isinstance(row, dict):
            continue
        external_id = str(row.get("id") or "").strip()
        title = _text(str(row.get("title") or ""))
        content = _text(str(row.get("digest") or ""))
        if not external_id or not (title or content):
            continue
        items.append(
            RawItem(
                source=EastmoneyNewsCollector.code,
                external_id=external_id,
                ts=_cst(row.get("showtime")),
                payload={
                    "title": title or content[:80],
                    "content": content or title,
                    "url": row.get("url_unique") or row.get("url"),
                },
            )
        )
    return items


class SinaNewsCollector(BaseCollector):
    """新浪财经 7×24 直播。"""

    code: ClassVar[str] = "sina_news"
    name: ClassVar[str] = "新浪财经7×24"
    kind: ClassVar[str] = "news"
    interval_sec: ClassVar[int] = 120

    URL: ClassVar[str] = (
        "https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=30"
        "&zhibo_id=152&tag_id=0&dire=f&dpc=1"
    )

    async def fetch(self) -> list[RawItem]:
        response = await self._http.get(self.URL)
        response.raise_for_status()
        return parse_sina_news(response.json())


def parse_sina_news(payload: Any) -> list[RawItem]:
    try:
        rows = payload["result"]["data"]["feed"]["list"]
    except (KeyError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    items: list[RawItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        external_id = str(row.get("id") or "").strip()
        content = _text(str(row.get("rich_text") or row.get("text") or ""))
        if not external_id or not content:
            continue
        items.append(
            RawItem(
                source=SinaNewsCollector.code,
                external_id=external_id,
                ts=_cst(row.get("create_time")),
                payload={
                    "title": content[:80],
                    "content": content,
                    "url": row.get("docurl") or row.get("url"),
                },
            )
        )
    return items


class CzceAnnouncementCollector(BaseCollector):
    """郑商所公告（HTML 列表；412/改版时静默失败并记 fetch_log）。"""

    code: ClassVar[str] = "czce_announcement"
    name: ClassVar[str] = "郑商所公告"
    kind: ClassVar[str] = "news"
    interval_sec: ClassVar[int] = 1800

    BASE: ClassVar[str] = "http://www.czce.com.cn"
    URL: ClassVar[str] = f"{BASE}/cn/jysj/jysgg/H770304index_1.htm"

    async def fetch(self) -> list[RawItem]:
        response = await self._http.get(
            self.URL, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True
        )
        if response.status_code in (403, 412):
            logger.info("czce_announcement_blocked status=%s", response.status_code)
            return []
        response.raise_for_status()
        body = response.content.decode("gb18030", errors="replace")
        return parse_czce_announcements(body, base=self.BASE)


_ANCHOR_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def parse_czce_announcements(body: str, *, base: str = "http://www.czce.com.cn") -> list[RawItem]:
    items: list[RawItem] = []
    for href, raw_title in _ANCHOR_RE.findall(body):
        title = _text(raw_title)
        if len(title) < 4 or "rootfiles" not in href:
            continue
        url = urljoin(base, href)
        published = None
        tail = body[body.find(href) : body.find(href) + 400]
        match = _DATE_RE.search(tail)
        if match:
            published = _cst(match.group(1))
        items.append(
            RawItem(
                source=CzceAnnouncementCollector.code,
                external_id=href,
                ts=published,
                payload={"title": title, "content": title, "url": url},
            )
        )
    return items
