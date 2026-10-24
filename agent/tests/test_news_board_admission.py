"""Excluded boards must cause no model/index work and no keyword false positives."""
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.analytics.entities import ContentType
from app.analytics.news_content import NewsContent, TencentNewsCacheRepository
from app.model_runtime.local_embedding import LocalHashEmbedding
from app.modules.knowledge.board_policy import (
    BOARD_FIELDS, NewsBoardExcluded, board_exclusion_detail,
)
from app.modules.knowledge.document import KnowledgeDocument
from app.modules.knowledge.ingest import ContentIngestPolicy, ContentIngestService
from app.modules.knowledge.native_indexing import NativeKnowledgeIndexingService
from app.modules.knowledge.postgres_store import PostgresKnowledgeStore
from app.modules.knowledge.qa import NativeQAGenerationService
from app.modules.knowledge.tier_lifecycle import KnowledgeLifecycleService
from app.retrieval.in_memory_index import InMemoryTieredVectorIndex


class NoIO:
    def __getattr__(self, name):
        raise AssertionError(f"excluded board attempted downstream I/O: {name}")


def document(news_id="health", category="健康"):
    return KnowledgeDocument(
        document_id=f"news-{news_id}", title="产业健康发展", text="产业政策与科技企业发展新闻。",
        media_type="article", text_source="test", metadata={
            "news_id": news_id, "content_version": "1", "category": category,
            "publish_time": datetime(2026, 10, 1, tzinfo=UTC).isoformat(),
        },
    )


@pytest.mark.parametrize("key", BOARD_FIELDS)
@pytest.mark.parametrize("value", ["health", " HEALTH ", "健康", "健康频道", "健康板块", "news_news_health"])
def test_explicit_health_aliases_are_excluded(key, value):
    assert board_exclusion_detail({key: value}) == (
        "news_board_excluded: board=health; policy=news-board-admission-v1"
    )


def test_only_classification_fields_are_used_and_conflicts_do_not_bypass_exclusion():
    assert board_exclusion_detail({"category": "finance", "board": "健康"}) is not None
    assert board_exclusion_detail({"category": "finance", "title": "健康产业发展",
                                   "topic_ids": "health", "body": "健康"}) is None
    assert board_exclusion_detail({}) is None
    assert board_exclusion_detail({"category": "public"}) is None
    assert board_exclusion_detail({"category": "健康产业投资"}) is None


@pytest.mark.asyncio
async def test_all_document_writers_skip_before_database_embedding_or_index():
    writers = [
        NativeKnowledgeIndexingService(embedding=NoIO(), index=NoIO(), embedding_version="v1"),
        PostgresKnowledgeStore(database=NoIO(), embedding=NoIO(), embedding_version="v1"),
        KnowledgeLifecycleService(ledger=NoIO(), index=NoIO(), embedding=NoIO(), config=None),
    ]
    for writer in writers:
        report = await writer.upsert_documents(tenant_id="t1", documents=(document(),))
        assert report.written == 0
        assert len(report.skipped) == 1
        assert report.skipped[0].collection_id is None
        assert "news-board-admission-v1" in report.skipped[0].detail
    with pytest.raises(NewsBoardExcluded):
        await writers[-1].accept(tenant_id="t1", document=document())


@pytest.mark.asyncio
async def test_excluded_source_does_not_generate_qa_or_textualize_video():
    qa = NativeQAGenerationService(agent=NoIO(), store=NoIO())
    assert len((await qa.generate_and_index(tenant_id="t1", source=document())).skipped) == 1
    service = ContentIngestService(textualizer=NoIO(), writer=NoIO(),
        policy=ContentIngestPolicy(version="test"), video_asset_resolver=NoIO())
    news = NewsContent(news_id="h1", title="健康视频", summary="", body="",
        content_type=ContentType.VIDEO, publish_time=datetime(2026, 10, 1, tzinfo=UTC),
        source_url="https://example.test/h1", category="health")
    report = await service.ingest(tenant_id="t1", contents=(news,))
    assert len(report.skipped) == 1
    assert "news_board_excluded" in report.skipped[0].detail


@pytest.mark.asyncio
async def test_mixed_batch_preserves_order_and_does_not_filter_health_words_in_finance():
    index = InMemoryTieredVectorIndex()
    service = NativeKnowledgeIndexingService(
        embedding=LocalHashEmbedding(model_routes=("v1",), dimensions=16),
        index=index, embedding_version="v1")
    docs = (document(), document("finance", "finance"), document("health2"))
    report = await service.upsert_documents(tenant_id="t1", documents=docs)
    assert [o.document_id for o in report.outcomes] == [d.document_id for d in docs]
    assert [o.status.value for o in report.outcomes] == ["skipped", "created", "skipped"]
    assert service.get_indexed(tenant_id="t1", document_id="news-health") is None
    assert service.get_indexed(tenant_id="t1", document_id="news-finance") is not None


def test_cache_mapping_preserves_source_category():
    row = {"news_id": "h1", "url": "https://example.test/h1", "title": "新闻",
           "content": "新闻正文", "publish_time": "2026-10-01T00:00:00+08:00", "category": "health"}
    content = TencentNewsCacheRepository._map_payload(row)
    assert content.category == "health"
    assert replace(content, category="finance").category == "finance"
