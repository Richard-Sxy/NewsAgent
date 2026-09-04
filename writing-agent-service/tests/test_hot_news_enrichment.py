from dataclasses import dataclass, field
from datetime import datetime

import pytest

from app.analytics.hot_news_enrichment import HotNewsEnrichmentService
from app.analytics.entities import ContentType
from app.analytics.news_content import InMemoryNewsContentRepository, NewsContent
from app.clients.knowledge_base import RelatedNews
from examples.hot_news_demo import build_ranking, load_scenario


@dataclass
class FakeKnowledgeSearch:
    calls: list[tuple[str, str | None, int]] = field(default_factory=list)

    async def search_related_news(
        self,
        query: str,
        *,
        exclude_news_id: str | None = None,
        limit: int = 5,
    ) -> list[RelatedNews]:
        self.calls.append((query, exclude_news_id, limit))
        return [
            RelatedNews(
                collection_id=f"related-{exclude_news_id}",
                news_id=f"history-{exclude_news_id}",
                title="相关历史报道",
                text="这是知识库召回的相关报道片段。",
                source_url="https://news.example.com/history",
                publish_time="2026-09-01T08:00:00+08:00",
                score=0.91,
            )
        ]


def content_repository_from_scenario() -> InMemoryNewsContentRepository:
    scenario = load_scenario()
    return InMemoryNewsContentRepository(
        [
            NewsContent(
                news_id=item["news_id"],
                title=item["title"],
                summary=item["summary"],
                content_type=ContentType(item["content_type"]),
                publish_time=datetime.fromisoformat(item["publish_time"]),
                source_url=item["source_url"],
            )
            for item in scenario["news"]
        ]
    )


@pytest.mark.asyncio
async def test_enriches_ranking_with_exact_content_and_related_news() -> None:
    scenario = load_scenario()
    ranked = build_ranking(scenario, limit=2)
    search = FakeKnowledgeSearch()
    service = HotNewsEnrichmentService(content_repository_from_scenario(), search)

    enriched = await service.enrich(ranked, related_limit=2)
    reranked = enriched[0].related_news[0]

    assert [item.content.news_id for item in enriched if item.content] == [
        "20260827A0C5VA00",
        "20260828A009SN00",
    ]
    assert reranked.news.score == 0.91
    assert reranked.final_score >= 0.45
    assert "AI数据中心" in enriched[0].content.title
    assert enriched[0].related_news[0].score == 0.91
    assert search.calls[0][1:] == ("20260827A0C5VA00", 2)
    assert "AI数据中心" in search.calls[0][0]


@pytest.mark.asyncio
async def test_missing_content_does_not_call_knowledge_search() -> None:
    ranked = build_ranking(load_scenario(), limit=1)
    search = FakeKnowledgeSearch()
    service = HotNewsEnrichmentService(InMemoryNewsContentRepository([]), search)

    enriched = await service.enrich(ranked)

    assert enriched[0].content is None
    assert enriched[0].related_news == ()
    assert search.calls == []


@pytest.mark.asyncio
async def test_enrichment_hydrates_legacy_result_from_content_repository() -> None:
    scenario = load_scenario()
    ranked = build_ranking(scenario, limit=1)
    repository = content_repository_from_scenario()

    class LegacySearch:
        async def search_related_news(self, query, *, exclude_news_id=None, limit=5):
            return [
                RelatedNews(
                    collection_id="legacy-related",
                    news_id="20260828A009SN00",
                    title="tencent-news-20260828A009SN00.txt",
                    text="",
                    source_url=None,
                    publish_time=None,
                    score=None,
                )
            ]

    service = HotNewsEnrichmentService(repository, LegacySearch())
    enriched = await service.enrich(ranked)
    related = enriched[0].related_news[0]

    expected = repository.get_by_news_id("20260828A009SN00")
    assert expected is not None
    assert related.title == expected.title
    assert related.source_url == expected.source_url
    assert related.publish_time == expected.publish_time.isoformat()
    assert related.text == expected.summary
