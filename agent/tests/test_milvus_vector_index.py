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


def verification_record():
    return ChunkVectorRecord(
        chunk_id="projection-1-0", tenant_id="tenant-1", news_id="news-1",
        content_version=2, chunk_index=0, embedding_version="embedding-v1",
        title="更正标题", excerpt="更正切片", source_url=None,
        publish_time=datetime(2026, 10, 15, tzinfo=UTC), vector=(0.1, 0.2),
    )


@pytest.mark.asyncio
async def test_lifecycle_metadata_verification_uses_strong_consistency_and_timeout():
    record = verification_record()

    class MetadataMilvus(FakeMilvusClient):
        def query(self, **kwargs):
            self.calls.append(("query", kwargs))
            return [MilvusTieredVectorIndex._record_payload(record)]

    milvus = MetadataMilvus()
    index = MilvusTieredVectorIndex(
        milvus, collections={VectorTier.HOT: "news_hot_v1"},
        consistency_level="Strong", timeout_seconds=2.5,
    )
    assert await index.verify_records(tier=VectorTier.HOT, records=[record])
    _, call = milvus.calls[0]
    assert call["consistency_level"] == "Strong"
    assert call["timeout"] == 2.5
    assert "tenant_id" in call["output_fields"]
    assert "vector" not in call["output_fields"]
    await index.upsert(tier=VectorTier.HOT, records=[record])
    await index.contains(tier=VectorTier.HOT, chunk_ids=[record.chunk_id])
    await index.delete(tier=VectorTier.HOT, chunk_ids=[record.chunk_id])
    await index.search(
        tier=VectorTier.HOT, tenant_id=record.tenant_id, vector=record.vector,
        embedding_version=record.embedding_version, exclude_news_ids=frozenset(), limit=1,
    )
    for operation, kwargs in milvus.calls:
        assert kwargs["timeout"] == 2.5
        if operation in {"search", "query"}:
            assert kwargs["consistency_level"] == "Strong"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,wrong_value",
    [("tenant_id", "other-tenant"), ("news_id", "other-news"),
     ("content_version", 1), ("chunk_index", 1), ("embedding_version", "old-model"),
     ("title", "旧标题"), ("excerpt", "旧切片"), ("source_url", "https://wrong.test"),
     ("publish_time_epoch", 0)],
)
async def test_lifecycle_verification_rejects_correct_id_with_wrong_metadata(field, wrong_value):
    record = verification_record()

    class IncorrectMetadata(FakeMilvusClient):
        def query(self, **kwargs):
            row = MilvusTieredVectorIndex._record_payload(record)
            row[field] = wrong_value
            return [row]

    assert not await make_index(IncorrectMetadata()).verify_records(
        tier=VectorTier.HOT, records=[record]
    )


@pytest.mark.asyncio
async def test_verification_rejects_missing_and_duplicate_records():
    record = verification_record()
    assert not await make_index(FakeMilvusClient()).verify_records(
        tier=VectorTier.HOT, records=[record]
    )
    with pytest.raises(ValueError, match="duplicate chunk_id"):
        await make_index(FakeMilvusClient()).verify_records(
            tier=VectorTier.HOT, records=[record, record]
        )


@pytest.mark.asyncio
async def test_legacy_milvus_reader_keeps_collection_consistency_default():
    milvus = FakeMilvusClient()
    await make_index(milvus).contains(tier=VectorTier.HOT, chunk_ids=["chunk-1"])
    assert "consistency_level" not in milvus.calls[0][1]
