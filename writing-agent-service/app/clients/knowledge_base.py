"""FastGPT 新闻知识库检索适配器。"""

from dataclasses import dataclass
import re
from typing import Any, Protocol

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict


class KnowledgeSearchSettings(BaseSettings):
    """知识库联调的最小配置，不依赖数据库、Temporal 或对象存储。"""

    fastgpt_base_url: str = "http://127.0.0.1:3000"
    fastgpt_api_key: str
    fastgpt_dataset_id: str

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


class KnowledgeSearchConfig(Protocol):
    fastgpt_base_url: str
    fastgpt_api_key: str
    fastgpt_dataset_id: str | None


@dataclass(frozen=True, slots=True)
class RelatedNews:
    collection_id: str
    news_id: str | None
    title: str
    text: str
    source_url: str | None
    publish_time: str | None
    score: float | None


class KnowledgeSearchClient(Protocol):
    async def search_related_news(
        self,
        query: str,
        *,
        exclude_news_id: str | None = None,
        limit: int = 5,
    ) -> list[RelatedNews]: ...


class FastGPTKnowledgeSearchClient:
    """调用 FastGPT 数据集搜索接口，并将切片结果归并到新闻 Collection。"""

    _TENCENT_SOURCE_NAME = re.compile(
        r"^tencent-news-(?P<news_id>[A-Za-z0-9_-]+)\.txt$",
        re.IGNORECASE,
    )

    def __init__(
        self,
        settings: KnowledgeSearchConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        if not settings.fastgpt_dataset_id:
            raise ValueError("FASTGPT_DATASET_ID is required for knowledge search")
        self.base_url = settings.fastgpt_base_url.rstrip("/")
        self.api_key = settings.fastgpt_api_key
        self.dataset_id = settings.fastgpt_dataset_id
        self.http_client = http_client or httpx.AsyncClient()
        self._owns_client = http_client is None
        self.timeout = httpx.Timeout(timeout_seconds, connect=10)

    async def close(self) -> None:
        if self._owns_client:
            await self.http_client.aclose()

    async def search_related_news(
        self,
        query: str,
        *,
        exclude_news_id: str | None = None,
        limit: int = 5,
    ) -> list[RelatedNews]:
        if not query.strip():
            raise ValueError("query cannot be empty")
        if limit <= 0:
            raise ValueError("limit must be greater than 0")

        response = await self.http_client.post(
            f"{self.base_url}/api/core/dataset/searchTest",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "datasetId": self.dataset_id,
                "text": query,
                "queryImageUrls": [],
                "limit": 3000,
                "searchMode": "embedding",
                "usingReRank": True,
                "datasetSearchUsingQueryRewrite": True,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") not in (None, 200):
            raise RuntimeError(body.get("message") or "FastGPT knowledge search failed")
        data = body.get("data", body)
        rows = data.get("list") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("FastGPT search response is missing data.list")

        results: list[RelatedNews] = []
        seen_collections: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            collection_id = str(row.get("collectionId") or "").strip()
            if not collection_id or collection_id in seen_collections:
                continue
            metadata = row.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            news_id = self._news_id(metadata, row.get("sourceName"))
            if exclude_news_id and news_id == exclude_news_id:
                continue
            seen_collections.add(collection_id)
            results.append(
                RelatedNews(
                    collection_id=collection_id,
                    news_id=news_id,
                    title=(
                        self._optional_text(metadata.get("original_title"))
                        or self._optional_text(row.get("sourceName"))
                        or "未命名关联报道"
                    ),
                    text=str(row.get("q") or row.get("a") or "")[:1000],
                    source_url=self._optional_text(metadata.get("source_url")),
                    publish_time=self._optional_text(metadata.get("publish_time")),
                    score=self._score(row.get("score")),
                )
            )
            if len(results) >= limit:
                break
        return results

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text or None

    @classmethod
    def _news_id(cls, metadata: dict[str, Any], source_name: Any) -> str | None:
        """兼容没有写入 metadata.news_id 的旧版 FastGPT 知识条目。"""
        metadata_news_id = cls._optional_text(metadata.get("news_id"))
        if metadata_news_id:
            return metadata_news_id

        normalized_source_name = cls._optional_text(source_name)
        if not normalized_source_name:
            return None
        match = cls._TENCENT_SOURCE_NAME.fullmatch(normalized_source_name)
        return match.group("news_id") if match else None

    @staticmethod
    def _score(value: Any) -> float | None:
        values = value if isinstance(value, list) else [value]
        numeric: list[float] = []
        for item in values:
            if isinstance(item, (int, float)):
                numeric.append(float(item))
            elif isinstance(item, dict):
                # FastGPT 不同版本可能返回 {"value": 0.8} 等评分对象。
                for key in ("value", "score", "similarity"):
                    candidate = item.get(key)
                    if isinstance(candidate, (int, float)):
                        numeric.append(float(candidate))
        return max(numeric) if numeric else None
