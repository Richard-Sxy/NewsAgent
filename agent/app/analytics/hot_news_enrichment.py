"""补全榜单内容。获取正文、召回关联报道、补全候选内容，再调用 retrieval 重拍，形成 EnrichHotNews。"""

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.behavior_tags.runtime import BehaviorRetrievalContext
    from app.reader_profiles.service import ReaderProfileService

from app.observability.telemetry import operation, set_safe_attributes
from app.observability.decorators import traced
from app.analytics.news_content import NewsContent, NewsContentRepository
from app.analytics.ranking import RankedHotNews
from app.clients.knowledge_base import (
    KnowledgeSearchClient,
    RelatedNews,
    RelatedNewsSearchQuery,
)
from app.domain.errors import HotNewsDataQualityError
from app.reader_profiles.contracts import (
    MAX_NEWS_PER_QUERY,
    ReaderProfileBatch,
    ReaderProfileProviderError,
    ReaderProfileQuery,
    ReaderProfileSummary,
    reader_profile_query_fingerprint,
)
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
    retrieval_trace: dict | None = None
    reader_profile_summary: ReaderProfileSummary | None = None
    reader_profile_trace: dict | None = None


class HotNewsEnrichmentService:
    def __init__(
        self,
        content_repository: NewsContentRepository,
        knowledge_search: KnowledgeSearchClient,
        reranker: RelatedNewsReranker | None = None,
        behavior_context: "BehaviorRetrievalContext | None" = None,
        reader_profile_service: "ReaderProfileService | None" = None,
        reader_profile_window: tuple[datetime, datetime] | None = None,
    ) -> None:
        if (getattr(getattr(knowledge_search, "index", None), "lifecycle_guarded", False) is True
                and getattr(content_repository, "lifecycle_guarded", False) is not True):
            raise ValueError("lifecycle retrieval requires canonical lifecycle content")
        self.content_repository = content_repository
        self.knowledge_search = knowledge_search
        self.reranker = reranker or RelatedNewsReranker()
        self.behavior_context = behavior_context
        if reader_profile_service is not None and reader_profile_window is None:
            raise ValueError("reader profiles require an explicit run window")
        self.reader_profile_service = reader_profile_service
        self.reader_profile_window = reader_profile_window

    @traced("retrieval.enrich")
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
        if self.behavior_context is not None and self.behavior_context.tenant_id != tenant_id:
            raise HotNewsDataQualityError("behavior retrieval context crossed its tenant scope")
        if related_limit <= 0:
            raise ValueError("related_limit must be greater than 0")
        if candidate_limit < related_limit:
            raise ValueError("candidate_limit must be greater than equal to related_limit")

        if not ranked:
            return []

        ranked_news_ids = tuple(
            ranking.current.news_id for ranking in ranked
        )
        with operation("content.fetch"):
            contents = await self.content_repository.batch_get_by_news_ids(
                tenant_id=tenant_id,
                news_ids=ranked_news_ids,
            )
        unexpected_content_ids = set(contents) - set(ranked_news_ids)
        if unexpected_content_ids:
            raise HotNewsDataQualityError(
                "content repository returned unrequested news_ids"
            )
        reader_profiles, reader_traces = await self._fetch_reader_profiles(
            ranked, tenant_id=tenant_id, contents=contents,
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
        with operation("retrieval.search"):
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
        lifecycle_resolver = getattr(self.content_repository, "resolve_enrichment", None)
        if lifecycle_resolver is not None:
            contents, recalled = await lifecycle_resolver(
                tenant_id=tenant_id, sources=contents, recalled=recalled,
            )
            candidate_contents = {}
        else:
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
            retrieval_trace = None

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
                with operation("retrieval.rerank"):
                    related = self.reranker.rerank(
                        content,
                        hydrated_candidates,
                        limit=max(1, len(hydrated_candidates)) if self.behavior_context is not None else related_limit,
                    )
                    if self.behavior_context is not None:
                        related, retrieval_trace = self.behavior_context.rerank(
                            content, related, limit=related_limit,
                        )

            enriched.append(
                EnrichedHotNews(
                    ranking=ranking,
                    content=content,
                    related_news=tuple(related),
                    retrieval_trace=retrieval_trace,
                    reader_profile_summary=reader_profiles.get(news_id),
                    reader_profile_trace=reader_traces.get(news_id),
                )
            )
        return enriched

    async def _fetch_reader_profiles(
        self, ranked: list[RankedHotNews], *, tenant_id: str,
        contents: dict[str, NewsContent],
    ) -> tuple[dict[str, ReaderProfileSummary], dict[str, dict]]:
        """Fetch only aggregate visitors' profiles, at most five batches for Top100."""
        if self.reader_profile_service is None:
            return {}, {}
        assert self.reader_profile_window is not None
        start, end = self.reader_profile_window
        if len(ranked) > 100 or any(
            (item.current.window_start, item.current.window_end) != (start, end)
            for item in ranked
        ):
            raise HotNewsDataQualityError("reader profiles differ from the ranked run window")
        news_ids = tuple(item.current.news_id for item in ranked)
        try:
            queries = [ReaderProfileQuery(
                tenant_id=tenant_id, news_ids=news_ids[offset:offset + MAX_NEWS_PER_QUERY],
                window_start=start, window_end=end,
                source_release_sha256=self.reader_profile_service.source_release_sha256,
            ) for offset in range(0, len(news_ids), MAX_NEWS_PER_QUERY)]
        except ValueError as exc:
            raise HotNewsDataQualityError("reader profile query scope is invalid") from exc
        tasks = [asyncio.create_task(self.reader_profile_service.fetch(query)) for query in queries]
        try:
            # One budget covers all chunks, rather than one full timeout per article.
            async with asyncio.timeout(getattr(self.reader_profile_service, "run_timeout_seconds", 5.0)):
                batches = await asyncio.gather(*tasks)
        except TimeoutError:
            raise ReaderProfileProviderError("timeout", retryable=True) from None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        summaries: dict[str, ReaderProfileSummary] = {}
        traces: dict[str, dict] = {}
        release_binding = None
        for query, raw in zip(queries, batches, strict=True):
            try:
                batch = ReaderProfileBatch.model_validate(raw.model_dump(mode="json"))
                if (batch.tenant_id != query.tenant_id
                        or (batch.window_start, batch.window_end) != (start, end)
                        or batch.source_release_sha256 != query.source_release_sha256
                        or batch.query_fingerprint != reader_profile_query_fingerprint(query)
                        or {item.news_id for item in batch.items} != set(query.news_ids)
                        or not batch.is_complete or batch.watermark < end
                        or any(not item.trigger_article_excluded for item in batch.items)):
                    raise ValueError("reader profile response crossed its request scope")
                binding = (batch.source_release_sha256, batch.taxonomy_version, batch.profile_policy_version,
                    batch.source_mode, batch.source_revision)
                if release_binding is not None and binding != release_binding:
                    raise ValueError("reader profile batches differ from their immutable release revision")
                release_binding = binding
            except (ValueError, TypeError, AttributeError) as exc:
                raise HotNewsDataQualityError("reader profile response binding is invalid") from exc
            trace = {
                "source_mode": batch.source_mode, "snapshot_id": batch.snapshot_id,
                "source_release_sha256": batch.source_release_sha256,
                "query_fingerprint": batch.query_fingerprint, "source_revision": batch.source_revision,
                "taxonomy_version": batch.taxonomy_version, "profile_policy_version": batch.profile_policy_version,
                "window_start": batch.window_start.isoformat(), "window_end": batch.window_end.isoformat(),
                "watermark": batch.watermark.isoformat(), "generated_at": batch.generated_at.isoformat(),
                "expires_at": batch.expires_at.isoformat() if batch.expires_at is not None else None,
            }
            for summary in batch.items:
                content = contents.get(summary.news_id)
                if (summary.content_sha256 is not None and content is not None
                        and sha256((content.body or "").encode()).hexdigest() != summary.content_sha256):
                    raise HotNewsDataQualityError("reader profile content binding differs from the news body")
                summaries[summary.news_id] = summary
                topic_ids = set(summary.content_topic_ids) | {
                    topic.topic_id for interest in (summary.long_term, summary.short_term) for topic in interest.topics
                }
                catalog = getattr(self.reader_profile_service, "catalog", None)
                topic_names = {topic.topic_id: topic.name for topic in getattr(catalog, "topics", ())
                    if topic.topic_id in topic_ids}
                traces[summary.news_id] = {**trace, "topic_names": topic_names}
        return summaries, traces

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
