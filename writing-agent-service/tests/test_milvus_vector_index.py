from datetime import UTC, datetime

import pytest

from app.clients.enterprise.milvus_vector import MilvusTieredVectorIndex
from app.retrieval.tiered_vector import ChunkVectorRecord, VectorTier


class FakeMilvusClient:
    def __init__(self) -> None:
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(("search", kwargs))
        return [[{
            "distance": 0.91,
            "entity": {
                "chunk_id": "chunk-2",
                "news_id": "news-2",
                "title": "关联报道",
                "excerpt": "报道正文",
                "source_url": "https://news.qq.com/2",
                "publish_time_epoch": 1788220800,
                "content_version": 2,
                "embedding_version": "embedding-v1",
            },
        }]]

    def upsert(self, **kwargs):
        self.calls.append(("upsert", kwargs))
        return {"upsert_count": len(kwargs["data"])}

    def query(self, **kwargs):
        self.calls.append(("query", kwargs))
        return [{"chunk_id": "chunk-1"}]

    def delete(self, **kwargs):
        self.calls.append(("delete", kwargs))
        return {"delete_count": 1}


def make_index(client):
    return MilvusTieredVectorIndex(
        client,
        collections={
            VectorTier.HOT: "news_hot_v1",
            VectorTier.WARM: "news_warm_v1",
            VectorTier.COLD: "news_cold_v1",
        },
    )


@pytest.mark.asyncio
async def test_milvus_search_enforces_tenant_model_and_exclusion_filters() -> None:
    milvus = FakeMilvusClient()
    result = await make_index(milvus).search(
        tier=VectorTier.HOT,
        tenant_id="tenant-1",
        vector=(1.0, 0.0),
        embedding_version="embedding-v1",
        exclude_news_ids=frozenset({"news-1"}),
        limit=20,
    )

    _, call = milvus.calls[0]
    assert call["collection_name"] == "news_hot_v1"
    assert 'tenant_id == "tenant-1"' in call["filter"]
    assert 'embedding_version == "embedding-v1"' in call["filter"]
    assert 'news_id not in ["news-1"]' in call["filter"]
    assert result[0].news_id == "news-2"
    assert result[0].tier == VectorTier.HOT


@pytest.mark.asyncio
async def test_milvus_upsert_uses_versioned_chunk_fields() -> None:
    milvus = FakeMilvusClient()
    record = ChunkVectorRecord(
        chunk_id="chunk-1",
        tenant_id="tenant-1",
        news_id="news-1",
        content_version=2,
        chunk_index=3,
        embedding_version="embedding-v1",
        title="标题",
        excerpt="切片",
        source_url=None,
        publish_time=datetime(2026, 9, 1, tzinfo=UTC),
        vector=(0.1, 0.2),
    )

    count = await make_index(milvus).upsert(
        tier=VectorTier.WARM,
        records=(record,),
    )

    assert count == 1
    _, call = milvus.calls[0]
    assert call["collection_name"] == "news_warm_v1"
    assert call["data"][0]["content_version"] == 2
    assert call["data"][0]["chunk_index"] == 3
    assert call["data"][0]["vector"] == [0.1, 0.2]


@pytest.mark.asyncio
async def test_milvus_rejects_filter_injection() -> None:
    with pytest.raises(ValueError, match="invalid tenant_id"):
        await make_index(FakeMilvusClient()).search(
            tier=VectorTier.HOT,
            tenant_id='tenant" or true',
            vector=(1.0,),
            embedding_version="embedding-v1",
            exclude_news_ids=frozenset(),
            limit=5,
        )
