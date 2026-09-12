"""基于本地内容语料的确定性关联新闻检索。

作为 FastGPT 知识库检索的离线回退：只用字符二元组重叠度打分，
不调用任何外部服务，相同语料和查询必须得到相同排序。
"""

from __future__ import annotations

from typing import Protocol

from app.analytics.news_content import NewsContent, NewsContentRepository
from app.clients.knowledge_base import (
    RelatedNews,
    RelatedNewsSearchQuery,
)


class SearchableNewsContentRepository(NewsContentRepository, Protocol):
    """可枚举全部内容的仓储；内存和本地缓存实现均满足。"""

    def list_all(self) -> list[NewsContent]: ...


class CorpusKnowledgeSearchClient:
    """在本地内容语料上做确定性词面召回。"""

    def __init__(
        self,
        repository: SearchableNewsContentRepository,
        *,
        text_scan_chars: int = 2000,
    ) -> None:
        if text_scan_chars <= 0:
            raise ValueError("text_scan_chars must be greater than 0")
        self._repository = repository
        self._text_scan_chars = text_scan_chars

    async def batch_search_related_news(
        self,
        queries: tuple[RelatedNewsSearchQuery, ...],
        *,
        tenant_id: str,
    ) -> dict[str, list[RelatedNews]]:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        query_ids = [query.query_id for query in queries]
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("queries cannot contain duplicate query_id values")

        candidates = self._repository.list_all()
        indexed = [
            (
                content,
                self._grams(
                    f"{content.title}\n{content.body[: self._text_scan_chars]}"
                ),
            )
            for content in candidates
        ]

        results: dict[str, list[RelatedNews]] = {}
        for query in queries:
            query.validate()
            excluded = set(query.exclude_news_ids)
            excluded.add(query.source_news_id)
            query_grams = self._grams(query.query_text)
            scored = [
                (self._overlap(query_grams, grams), content)
                for content, grams in indexed
                if content.news_id not in excluded
            ]
            ranked = sorted(
                (
                    (score, content)
                    for score, content in scored
                    if score > 0
                ),
                key=lambda item: (-item[0], item[1].news_id),
            )
            results[query.query_id] = [
                self._to_related_news(content, score)
                for score, content in ranked[: query.candidate_limit]
            ]
        return results

    @staticmethod
    def _to_related_news(content: NewsContent, score: float) -> RelatedNews:
        return RelatedNews(
            collection_id=content.collection_id or content.news_id,
            news_id=content.news_id,
            title=content.title,
            text=content.body[:1000],
            source_url=content.source_url,
            publish_time=content.publish_time.isoformat(),
            score=score,
        )

    @classmethod
    def _grams(cls, text: str) -> frozenset[str]:
        normalized = "".join(
            character.lower()
            for character in text
            if not character.isspace() and not cls._is_punctuation(character)
        )
        if len(normalized) < 2:
            return frozenset({normalized}) if normalized else frozenset()
        return frozenset(
            normalized[index : index + 2]
            for index in range(len(normalized) - 1)
        )

    @staticmethod
    def _is_punctuation(character: str) -> bool:
        return not (character.isalnum() or "\u4e00" <= character <= "\u9fff")

    @staticmethod
    def _overlap(
        query_grams: frozenset[str],
        candidate_grams: frozenset[str],
    ) -> float:
        if not query_grams:
            return 0.0
        return len(query_grams & candidate_grams) / len(query_grams)
