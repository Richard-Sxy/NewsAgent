"""将热点排行与新闻内容、知识库关联报道组合。"""

from dataclasses import dataclass, replace

from app.analytics.news_content import NewsContent, NewsContentRepository
from app.analytics.ranking import RankedHotNews
from app.clients.knowledge_base import (
    KnowledgeSearchClient,
    RelatedNews,
    RelatedNewsSearchQuery,
)
from app.domain.errors import HotNewsDataQualityError
from app.retrieval.related_news_reranker import (
    RelatedNewsReranker,
    RerankRelatedNews,
)


@dataclass(frozen=True, slots=True)
class EnrichedHotNews:
    """热点新闻实体/新闻内容/相关的新闻(召回)"""
    ranking: RankedHotNews
    content: NewsContent | None
    related_news: tuple[RerankRelatedNews, ...]


class HotNewsEnrichmentService:
    def __init__(
        self,
        content_repository: NewsContentRepository,
        knowledge_search: KnowledgeSearchClient,
        reranker: RelatedNewsReranker | None = None,
    ) -> None:
        self.content_repository = content_repository
        self.knowledge_search = knowledge_search
        self.reranker = reranker or RelatedNewsReranker()

    async def enrich(
        self,
        ranked: list[RankedHotNews],
        *,
        tenant_id: str,
        related_limit: int = 3,
        candidate_limit: int = 20,
    ) -> list[EnrichedHotNews]:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        if related_limit <= 0:
            raise ValueError("related_limit must be greater than 0")
        if candidate_limit < related_limit:
            raise ValueError("candidate_limit must be greater than equal to related_limit")

        if not ranked:
            return []

        ranked_news_ids = tuple(
            ranking.current.news_id for ranking in ranked
        )
        contents = await self.content_repository.batch_get_by_news_ids(
            tenant_id=tenant_id,
            news_ids=ranked_news_ids,
        )
        unexpected_content_ids = set(contents) - set(ranked_news_ids)
        if unexpected_content_ids:
            raise HotNewsDataQualityError(
                "content repository returned unrequested news_ids"
            )
        queries = tuple(
            RelatedNewsSearchQuery(
                query_id=ranking.current.news_id,
                source_news_id=ranking.current.news_id,
                title=contents[ranking.current.news_id].title,
                summary=contents[ranking.current.news_id].summary,
                exclude_news_ids=(ranking.current.news_id,),
                candidate_limit=candidate_limit,
            )
            for ranking in ranked
            if ranking.current.news_id in contents
        )
        recalled = await self.knowledge_search.batch_search_related_news(
            queries,
            tenant_id=tenant_id,
        )
        expected_query_ids = {query.query_id for query in queries}
        if set(recalled) != expected_query_ids:
            raise HotNewsDataQualityError(
                "related-news response does not match requested query_ids"
            )

        candidate_news_ids = tuple(
            dict.fromkeys(
                candidate.news_id
                for candidates in recalled.values()
                for candidate in candidates
                if candidate.news_id
            )
        )
        candidate_contents = (
            await self.content_repository.batch_get_by_news_ids(
                tenant_id=tenant_id,
                news_ids=candidate_news_ids,
            )
            if candidate_news_ids
            else {}
        )
        if set(candidate_contents) - set(candidate_news_ids):
            raise HotNewsDataQualityError(
                "content repository returned unrequested candidate news_ids"
            )

        enriched: list[EnrichedHotNews] = []
        for ranking in ranked:
            news_id = ranking.current.news_id
            content = contents.get(news_id)
            related: list[RerankRelatedNews] = []

            if content is not None:
                candidates = recalled.get(news_id, [])
                hydrated_candidates = [
                    self._hydrate_related_news(
                        candidate,
                        candidate_contents,
                    )
                    for candidate in candidates
                    if candidate.news_id != news_id
                ]
                related = self.reranker.rerank(
                    content,
                    hydrated_candidates,
                    limit=related_limit,
                )

            enriched.append(
                EnrichedHotNews(
                    ranking=ranking,
                    content=content,
                    related_news=tuple(related),
                )
            )
        return enriched

    @staticmethod
    def _hydrate_related_news(
        related: RelatedNews,
        contents: dict[str, NewsContent],
    ) -> RelatedNews:
        """用内容库补全旧版知识条目缺失的标题、链接和发布时间。"""
        if not related.news_id:
            return related
        content = contents.get(related.news_id)
        if content is None:
            return related
        return replace(
            related,
            title=content.title,
            text=related.text or content.summary,
            source_url=content.source_url,
            publish_time=content.publish_time.isoformat(),
        )
