from datetime import UTC, datetime

import pytest

from app.clients.knowledge_base import RelatedNewsSearchQuery
from app.retrieval.tiered_vector import (
    ChunkVectorRecord,
    TierMigrationService,
    TierSearchPolicy,
    TieredVectorKnowledgeSearchClient,
    VectorSearchHit,
    VectorTier,
)


class FakeEmbedder:
    version = "embedding-v1"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed_queries(self, texts):
        self.calls.append(list(texts))
        return [(1.0, 0.0) for _ in texts]


class FakeIndex:
    def __init__(self, hits=None) -> None:
        self.hits = hits or {}
        self.searches = []
        self.rows = {tier: {} for tier in VectorTier}
        self.operations = []

    async def search(self, **kwargs):
        self.searches.append(kwargs)
        return self.hits.get(kwargs["tier"], [])

    async def upsert(self, *, tier, records):
        self.operations.append(("upsert", tier))
        for record in records:
            self.rows[tier][record.chunk_id] = record
        return len(records)

    async def contains(self, *, tier, chunk_ids):
        self.operations.append(("contains", tier))
        return frozenset(item for item in chunk_ids if item in self.rows[tier])

    async def delete(self, *, tier, chunk_ids):
        self.operations.append(("delete", tier))
        for chunk_id in chunk_ids:
            self.rows[tier].pop(chunk_id, None)
        return len(chunk_ids)


def hit(tier, chunk_id, news_id, score):
    return VectorSearchHit(
        chunk_id=chunk_id,
        news_id=news_id,
        title=f"title-{news_id}",
        excerpt=f"excerpt-{news_id}",
        source_url=None,
        publish_time=datetime(2026, 9, 1, tzinfo=UTC),
        raw_score=score,
        tier=tier,
        content_version=1,
        embedding_version="embedding-v1",
    )


@pytest.mark.asyncio
async def test_tiered_search_embeds_once_and_fuses_by_news_id() -> None:
    embedder = FakeEmbedder()
    index = FakeIndex(
        {
            VectorTier.HOT: [
                hit(VectorTier.HOT, "h-a-1", "news-a", 0.8),
                hit(VectorTier.HOT, "h-a-2", "news-a", 0.7),
                hit(VectorTier.HOT, "h-b", "news-b", 0.9),
            ],
            VectorTier.WARM: [
                hit(VectorTier.WARM, "w-a", "news-a", 0.6),
            ],
            VectorTier.COLD: [],
        }
    )
    client = TieredVectorKnowledgeSearchClient(
        embedder=embedder,
        index=index,
        policy=TierSearchPolicy(cold_weight=0.5),
    )

    result = await client.batch_search_related_news(
        (
            RelatedNewsSearchQuery(
                query_id="query-1",
                source_news_id="source-1",
                title="腾讯新闻",
                summary="热点",
                candidate_limit=2,
            ),
        ),
        tenant_id="tenant-1",
    )

    assert embedder.calls == [["腾讯新闻 热点"]]
    assert [item.news_id for item in result["query-1"]] == ["news-a", "news-b"]
    assert len(index.searches) == 3
    assert all(call["embedding_version"] == "embedding-v1" for call in index.searches)
    assert all("source-1" in call["exclude_news_ids"] for call in index.searches)


@pytest.mark.asyncio
async def test_migration_verifies_target_before_deleting_source() -> None:
    index = FakeIndex()
    record = ChunkVectorRecord(
        chunk_id="tenant-1:news-1:1:0:embedding-v1",
        tenant_id="tenant-1",
        news_id="news-1",
        content_version=1,
        chunk_index=0,
        embedding_version="embedding-v1",
        title="标题",
        excerpt="正文",
        source_url=None,
        publish_time=datetime(2026, 9, 1, tzinfo=UTC),
        vector=(1.0, 0.0),
    )
    index.rows[VectorTier.HOT][record.chunk_id] = record

    moved = await TierMigrationService(index).migrate(
        source=VectorTier.HOT,
        target=VectorTier.WARM,
        records=(record,),
    )

    assert moved == 1
    assert index.operations == [
        ("upsert", VectorTier.WARM),
        ("contains", VectorTier.WARM),
        ("delete", VectorTier.HOT),
    ]
    assert record.chunk_id in index.rows[VectorTier.WARM]
    assert record.chunk_id not in index.rows[VectorTier.HOT]
