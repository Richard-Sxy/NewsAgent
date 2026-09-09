"""热点新闻精确内容查询模型与仓储接口。"""

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from app.analytics.entities import ContentType


@dataclass(frozen=True, slots=True)
class NewsContent:
    news_id: str
    title: str
    summary: str
    content_type: ContentType
    publish_time: datetime
    source_url: str
    collection_id: str | None = None
    body: str = ""

    def validate(self) -> None:
        if not self.news_id.strip():
            raise ValueError("news_id cannot be empty")
        if not self.title.strip():
            raise ValueError("title cannot be empty")
        if self.publish_time.tzinfo is None or self.publish_time.utcoffset() is None:
            raise ValueError("publish_time must be timezone-aware")


class NewsContentRepository(Protocol):
    async def batch_get_by_news_ids(
        self,
        *,
        tenant_id: str,
        news_ids: tuple[str, ...],
    ) -> dict[str, NewsContent]: ...


class InMemoryNewsContentRepository:
    """本地演示仓储；真实环境可替换为企业内容库适配器。"""

    def __init__(self, contents: list[NewsContent]) -> None:
        self._contents: dict[str, NewsContent] = {}
        for content in contents:
            content.validate()
            if content.news_id in self._contents:
                raise ValueError(f"duplicate news_id: {content.news_id}")
            self._contents[content.news_id] = content

    def get_by_news_id(self, news_id: str) -> NewsContent | None:
        return self._contents.get(news_id)

    def list_all(self) -> list[NewsContent]:
        """返回内存快照，供离线检索演示使用。"""
        return list(self._contents.values())

    async def batch_get_by_news_ids(
        self,
        *,
        tenant_id: str,
        news_ids: tuple[str, ...],
    ) -> dict[str, NewsContent]:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        return {
            news_id: self._contents[news_id]
            for news_id in dict.fromkeys(news_ids)
            if news_id in self._contents
        }


class TencentNewsCacheRepository:
    """读取 ``tencent-news-crawler/data/articles`` 形式的本地 JSON 缓存。

    这是本地联调适配器，不是企业内容存储方案。真实环境只需实现相同的
    ``get_by_news_id`` 接口即可替换它。
    """

    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir)
        self._contents = self._load_contents()

    def get_by_news_id(self, news_id: str) -> NewsContent | None:
        return self._contents.get(news_id)

    def list_all(self) -> list[NewsContent]:
        """返回内容快照，供离线联调检索使用。"""
        return list(self._contents.values())

    async def batch_get_by_news_ids(
        self,
        *,
        tenant_id: str,
        news_ids: tuple[str, ...],
    ) -> dict[str, NewsContent]:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        return {
            news_id: self._contents[news_id]
            for news_id in dict.fromkeys(news_ids)
            if news_id in self._contents
        }

    def _load_contents(self) -> dict[str, NewsContent]:
        if not self.cache_dir.is_dir():
            raise ValueError(f"news cache directory does not exist: {self.cache_dir}")

        contents: dict[str, NewsContent] = {}
        for path in sorted(self.cache_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                content = self._map_payload(payload)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid news cache file: {path}") from exc
            if content.news_id in contents:
                raise ValueError(f"duplicate news_id in cache: {content.news_id}")
            contents[content.news_id] = content
        return contents

    @staticmethod
    def _map_payload(payload: dict[str, object]) -> NewsContent:
        source_url = str(payload["url"])
        news_id = str(payload.get("news_id") or "").strip()
        if not news_id:
            news_id = urlparse(source_url).path.rstrip("/").split("/")[-1].strip()
        if not news_id:
            raise ValueError("news cache URL is missing news_id")

        raw_publish_time = str(payload["publish_time"]).strip()
        publish_time = datetime.fromisoformat(raw_publish_time)
        if publish_time.tzinfo is None:
            publish_time = publish_time.replace(tzinfo=ZoneInfo("Asia/Shanghai"))

        body = str(payload["content"]).strip()
        title = str(payload["title"]).strip()
        content = NewsContent(
            news_id=news_id,
            title=title,
            summary=body[:300],
            content_type=ContentType(str(payload.get("content_type") or "article")),
            publish_time=publish_time,
            source_url=source_url,
            collection_id=(
                str(payload["collection_id"]).strip()
                if payload.get("collection_id")
                else None
            ),
            body=body,
        )
        content.validate()
        return content
