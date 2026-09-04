"""将热点排行与新闻内容、知识库关联报道组合。"""

from dataclasses import dataclass, replace

from app.analytics.news_content import NewsContent, NewsContentRepository
from app.analytics.ranking import RankedHotNews
from app.clients.knowledge_base import KnowledgeSearchClient, RelatedNews
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
        related_limit: int = 3,
        candidate_limit: int = 20,
    ) -> list[EnrichedHotNews]:
        if related_limit <= 0:
            raise ValueError("related_limit must be greater than 0")
        if candidate_limit < related_limit:
            raise ValueError("candidate_limit must be greater than equal to related_limit")

        enriched: list[EnrichedHotNews] = []
        for ranking in ranked:
            # 获取排行对象的ID/从仓库里面查找内容/列出相关文章
            news_id = ranking.current.news_id
            content = self.content_repository.get_by_news_id(news_id)
            related: list[RerankRelatedNews] = []

            if content is not None:
                query = " ".join(
                    part.strip() for part in (content.title, content.summary) if part.strip()
                )
                candidates = (
                    await self.knowledge_search.search_related_news(
                        query,
                        exclude_news_id=news_id,
                        limit=candidate_limit,
                    )
                )
                # 候选文章的混合检索
                hydrated_candidates = [
                    self._hydrate_related_news(candidate)
                    for candidate in candidates
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

    def _hydrate_related_news(self, related: RelatedNews) -> RelatedNews:
        """用内容库补全旧版知识条目缺失的标题、链接和发布时间。"""
        if not related.news_id:
            return related
        content = self.content_repository.get_by_news_id(related.news_id)
        if content is None:
            return related
        return replace(
            related,
            title=content.title,
            text=related.text or content.summary,
            source_url=content.source_url,
            publish_time=content.publish_time.isoformat(),
        )
