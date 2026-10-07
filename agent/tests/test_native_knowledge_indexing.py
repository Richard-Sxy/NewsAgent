"""Native document -> chunk -> embedding -> index -> retrieval loop."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.clients.knowledge_base import RelatedNewsSearchQuery
from app.knowledge.document import IngestStatus, KnowledgeDocument
from app.knowledge.native_indexing import NativeKnowledgeIndexingService, split_text
from app.model_runtime.core import EmbeddingRequest, EmbeddingResult
from app.model_runtime.local_embedding import LocalHashEmbedding
from app.model_runtime.query_embedding import ModelQueryEmbedder
from app.retrieval.in_memory_index import InMemoryTieredVectorIndex
from app.retrieval.tiered_vector import TieredVectorKnowledgeSearchClient, VectorTier
from examples.hot_news_e2e_support import build_hot_news_e2e_dependencies
from examples.native_hot_news_e2e_knowledge import build_native_e2e_knowledge_search


def document(*, version: int = 1, text: str = "人工智能算力持续增长") -> KnowledgeDocument:
    return KnowledgeDocument(
        document_id="news:one",
        title="算力新闻",
        text=text,
        media_type="article",
        text_source="test",
        metadata={
            "news_id": "one",
            "content_version": str(version),
            "publish_time": datetime(2026, 10, 1, tzinfo=UTC).isoformat(),
        },
    )


def test_chunking_is_deterministic_and_bounded() -> None:
    assert split_text("abcdef", max_chars=4, overlap=1) == ("abcd", "def")
    with pytest.raises(ValueError, match="invalid"):
        split_text("abc", max_chars=2, overlap=2)


@pytest.mark.asyncio
async def test_indexing_is_idempotent_versioned_and_tenant_scoped() -> None:
    embedding = LocalHashEmbedding(model_routes=("embedding-v1",), dimensions=16)
    index = InMemoryTieredVectorIndex()
    service = NativeKnowledgeIndexingService(
        embedding=embedding,
        index=index,
        embedding_version="embedding-v1",
        max_chunk_chars=8,
        chunk_overlap=2,
    )
    first = await service.upsert_documents(tenant_id="tenant-1", documents=(document(),))
    repeated = await service.upsert_documents(tenant_id="tenant-1", documents=(document(),))
    changed_without_version = await service.upsert_documents(
        tenant_id="tenant-1", documents=(document(text="不同正文"),)
    )
    assert first.outcomes[0].status is IngestStatus.CREATED
    assert repeated.outcomes[0].status is IngestStatus.SKIPPED
    assert changed_without_version.outcomes[0].status is IngestStatus.FAILED
    previous = service.get_indexed(tenant_id="tenant-1", document_id="news:one")
    assert previous is not None
    assert len(previous.chunk_ids) >= 2

    updated = await service.upsert_documents(
        tenant_id="tenant-1", documents=(document(version=2, text="不同正文"),)
    )
    assert updated.outcomes[0].status is IngestStatus.UPDATED
    assert not await index.contains(tier=VectorTier.HOT, chunk_ids=previous.chunk_ids)
    assert service.get_indexed(tenant_id="tenant-2", document_id="news:one") is None

    search = TieredVectorKnowledgeSearchClient(
        embedder=ModelQueryEmbedder(embedding, version="embedding-v1"),
        index=index,
        tiers=(VectorTier.HOT,),
    )
    query = RelatedNewsSearchQuery(
        query_id="q-1", source_news_id="other", title="不同正文", summary=""
    )
    own = await search.batch_search_related_news((query,), tenant_id="tenant-1")
    other = await search.batch_search_related_news((query,), tenant_id="tenant-2")
    assert [item.news_id for item in own["q-1"]] == ["one"]
    assert other["q-1"] == []


@pytest.mark.asyncio
async def test_embedding_version_drift_does_not_mark_document_ready() -> None:
    class WrongVersion:
        async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
            return EmbeddingResult(((1.0, 0.0),) * len(request.texts), "wrong-version")

    service = NativeKnowledgeIndexingService(
        embedding=WrongVersion(),
        index=InMemoryTieredVectorIndex(),
        embedding_version="embedding-v1",
    )
    with pytest.raises(ValueError, match="version drifted"):
        await service.upsert_documents(tenant_id="tenant-1", documents=(document(),))
    assert service.get_indexed(tenant_id="tenant-1", document_id="news:one") is None


@pytest.mark.asyncio
async def test_synthetic_scenario_uses_embedding_retrieval_port() -> None:
    scenario = build_hot_news_e2e_dependencies()
    embedding = LocalHashEmbedding(
        model_routes=("native-local-embedding-v1",), dimensions=32
    )
    search = await build_native_e2e_knowledge_search(
        scenario,
        embedding=embedding,
        embedding_version="native-local-embedding-v1",
        embedding_batch_size=16,
    )
    result = await search.batch_search_related_news(
        (
            RelatedNewsSearchQuery(
                query_id="q-1",
                source_news_id=scenario.news_ids[0],
                title="AI 数据中心",
                summary="算力",
                candidate_limit=3,
            ),
        ),
        tenant_id=scenario.tenant_id,
    )
    assert result["q-1"]
    assert all(item.news_id != scenario.news_ids[0] for item in result["q-1"])
