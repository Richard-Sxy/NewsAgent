"""External recall may miss local fixture content without weakening SQL guards."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.analytics.entities import ContentType
from app.analytics.hot_news_enrichment import HotNewsEnrichmentService
from app.analytics.news_content import NewsContent
from app.domain.errors import HotNewsDataQualityError
from app.sql_assistant.warehouse import DEMO_TENANT_ID, demo_news_ids
from examples.hot_news_demo import build_ranking, load_scenario
from examples.native_hot_news_e2e_worker import ProfileScopedContentRepository
from examples.native_hot_news_sql_support import SqlHotNewsContentRepository, SqlHotNewsDetailRepository
from app.clients.knowledge_base import RelatedNews


TENANT = str(DEMO_TENANT_ID)


def content(news_id: str) -> NewsContent:
    return NewsContent(
        news_id=news_id, title="AI 数据中心新消息", summary="AI 数据中心算力需求增长。",
        content_type=ContentType.ARTICLE, publish_time=datetime(2026, 10, 11, tzinfo=timezone.utc),
        source_url="https://example.invalid/local-news/" + news_id,
    )


def repository(*news_ids):
    rows = {news_id: content(news_id) for news_id in news_ids}
    store = SimpleNamespace(batch_get_by_news_ids=AsyncMock(return_value=rows))
    wrapped = ProfileScopedContentRepository(store, tenant_id=TENANT, allowed_news_ids=news_ids)
    return wrapped, store, rows


@pytest.mark.asyncio
async def test_only_fixture_ids_are_forwarded_in_requested_order_without_duplicates():
    wrapped, store, rows = repository("fixture-a", "fixture-b")

    result = await wrapped.batch_get_by_news_ids(
        tenant_id=TENANT, news_ids=("bge-news", "fixture-b", "fixture-a", "fixture-b"),
    )

    assert result == rows
    store.batch_get_by_news_ids.assert_awaited_once_with(
        tenant_id=TENANT, news_ids=("fixture-b", "fixture-a"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("news_ids", [(), ("20260719A04P4Y00",)])
async def test_unknown_snapshot_candidate_needs_no_sql_call(news_ids):
    wrapped, store, _ = repository("fixture-a")

    assert await wrapped.batch_get_by_news_ids(tenant_id=TENANT, news_ids=news_ids) == {}
    store.batch_get_by_news_ids.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("news_ids", [(), ("20260719A04P4Y00",), ("fixture-a",)])
async def test_foreign_tenant_is_rejected_before_candidate_filtering(news_ids):
    wrapped, store, _ = repository("fixture-a")

    with pytest.raises(HotNewsDataQualityError, match="tenant scope"):
        await wrapped.batch_get_by_news_ids(tenant_id="foreign-tenant", news_ids=news_ids)

    store.batch_get_by_news_ids.assert_not_awaited()


@pytest.mark.asyncio
async def test_sql_repository_extra_ids_are_rejected_instead_of_silently_filtered():
    wrapped, store, _ = repository("fixture-a")
    store.batch_get_by_news_ids.return_value = {"unexpected-news": content("unexpected-news")}

    with pytest.raises(HotNewsDataQualityError, match="unrequested"):
        await wrapped.batch_get_by_news_ids(tenant_id=TENANT, news_ids=("fixture-a",))


@pytest.mark.asyncio
async def test_sql_content_id_must_match_its_mapping_key():
    wrapped, store, _ = repository("fixture-a")
    store.batch_get_by_news_ids.return_value = {"fixture-a": content("different-news")}

    with pytest.raises(HotNewsDataQualityError, match="mismatched news identity"):
        await wrapped.batch_get_by_news_ids(tenant_id=TENANT, news_ids=("fixture-a",))


@pytest.mark.asyncio
async def test_sql_failure_for_an_allowed_source_is_not_swallowed():
    wrapped, store, _ = repository("fixture-a")
    store.batch_get_by_news_ids.side_effect = HotNewsDataQualityError("missing source content")

    with pytest.raises(HotNewsDataQualityError, match="missing source content"):
        await wrapped.batch_get_by_news_ids(tenant_id=TENANT, news_ids=("fixture-a",))


@pytest.mark.asyncio
async def test_real_timeline_sql_guard_remains_strict_while_external_candidate_is_missing():
    # None proves no DB is touched: the raw repository rejects this foreign ID
    # before querying, and the wrapper returns missing metadata before calling it.
    details = SqlHotNewsDetailRepository(None, tenant_id=TENANT, dataset_profile="timeline-v4")
    strict = SqlHotNewsContentRepository(details)
    external_id = "20260719A04P4Y00"
    with pytest.raises(HotNewsDataQualityError, match="unknown synthetic news_id"):
        await strict.batch_get_by_news_ids(tenant_id=TENANT, news_ids=(external_id,))
    wrapped = ProfileScopedContentRepository(
        strict, tenant_id=TENANT, allowed_news_ids=demo_news_ids("timeline-v4"),
    )

    assert await wrapped.batch_get_by_news_ids(tenant_id=TENANT, news_ids=(external_id,)) == {}


@pytest.mark.asyncio
async def test_enrichment_preserves_snapshot_evidence_when_fixture_has_no_matching_article():
    scenario = load_scenario()
    ranked = build_ranking(scenario, limit=1)
    source_id = ranked[0].current.news_id
    row = next(item for item in scenario["news"] if item["news_id"] == source_id)
    source = NewsContent(
        news_id=source_id, title=row["title"], summary=row["summary"],
        content_type=ContentType(row["content_type"]),
        publish_time=datetime.fromisoformat(row["publish_time"]), source_url=row["source_url"],
    )
    strict = SimpleNamespace(batch_get_by_news_ids=AsyncMock(return_value={source_id: source}))
    wrapped = ProfileScopedContentRepository(strict, tenant_id=TENANT, allowed_news_ids=(source_id,))
    candidate = RelatedNews(
        collection_id="bge-snapshot:manifest:3", news_id="external-ai-news",
        title=source.title, text=source.summary, source_url=None, publish_time=None, score=0.95,
    )
    search = SimpleNamespace(batch_search_related_news=AsyncMock(return_value={source_id: [candidate]}))

    enriched = await HotNewsEnrichmentService(wrapped, search).enrich(ranked, tenant_id=TENANT)

    strict.batch_get_by_news_ids.assert_awaited_once_with(tenant_id=TENANT, news_ids=(source_id,))
    assert enriched[0].related_news[0].news == candidate
    assert enriched[0].related_news[0].time_score == 0.0
