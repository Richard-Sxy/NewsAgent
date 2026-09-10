"""Deterministic knowledge-search adapter for the local hot-news E2E stack.

The adapter only searches the checked-in scenario content.  It exists so the
Data Loop E2E stack can exercise the real enrichment boundary without copying
enterprise knowledge-base data or depending on a FastGPT dataset.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from app.analytics.news_content import NewsContent, NewsContentRepository
from app.clients.knowledge_base import RelatedNews, RelatedNewsSearchQuery


_NON_WHITESPACE = re.compile(r"\s+")
_ASCII_TOKEN = re.compile(r"[a-z0-9]{2,}")


def _terms(text: str) -> frozenset[str]:
    normalized = _NON_WHITESPACE.sub("", text).lower()
    grams = {
        normalized[index : index + 2]
        for index in range(max(0, len(normalized) - 1))
    }
    return frozenset(grams | set(_ASCII_TOKEN.findall(normalized)))


class OfflineScenarioKnowledgeSearchClient:
    """Search related news only inside the repository's synthetic scenario."""

    def __init__(
        self,
        content_repository: NewsContentRepository,
        candidate_news_ids: Sequence[str],
        *,
        min_overlap: int = 2,
    ) -> None:
        if min_overlap < 0:
            raise ValueError("min_overlap must be nonnegative")
        self._content_repository = content_repository
        self._candidate_news_ids = tuple(candidate_news_ids)
        self._min_overlap = min_overlap

    async def batch_search_related_news(
        self,
        queries: tuple[RelatedNewsSearchQuery, ...],
        *,
        tenant_id: str,
    ) -> dict[str, list[RelatedNews]]:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        contents = await self._content_repository.batch_get_by_news_ids(
            tenant_id=tenant_id,
            news_ids=self._candidate_news_ids,
        )
        return {
            query.query_id: self._recall(query, contents)
            for query in queries
        }

    def _recall(
        self,
        query: RelatedNewsSearchQuery,
        contents: Mapping[str, NewsContent],
    ) -> list[RelatedNews]:
        query.validate()
        excluded = set(query.exclude_news_ids)
        excluded.add(query.source_news_id)
        source_terms = _terms(query.query_text)

        scored: list[tuple[int, str, NewsContent]] = []
        for news_id in self._candidate_news_ids:
            if news_id in excluded:
                continue
            content = contents.get(news_id)
            if content is None:
                continue
            overlap = len(
                source_terms & _terms(f"{content.title} {content.summary}")
            )
            if overlap >= self._min_overlap:
                scored.append((overlap, news_id, content))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            RelatedNews(
                collection_id=f"offline-{news_id}",
                news_id=news_id,
                title=content.title,
                text=content.summary[:1000],
                source_url=content.source_url,
                publish_time=(
                    content.publish_time.isoformat()
                    if content.publish_time is not None
                    else None
                ),
                score=float(overlap),
            )
            for overlap, news_id, content in scored[: query.candidate_limit]
        ]
