"""Index synthetic news through the native EmbeddingPort before local runs."""

from __future__ import annotations

from app.clients.knowledge_base import KnowledgeSearchClient
from app.knowledge.document import KnowledgeDocument
from app.knowledge.native_indexing import NativeKnowledgeIndexingService
from app.knowledge.postgres_store import PostgresKnowledgeStore
from app.model_runtime.core import EmbeddingPort
from app.model_runtime.query_embedding import ModelQueryEmbedder
from app.retrieval.in_memory_index import InMemoryTieredVectorIndex
from app.retrieval.tiered_vector import (
    TieredVectorKnowledgeSearchClient,
    VectorTier,
)
from examples.hot_news_e2e_support import HotNewsE2EDependencies


async def build_native_e2e_knowledge_search(
    dependencies: HotNewsE2EDependencies,
    *,
    embedding: EmbeddingPort,
    embedding_version: str,
    embedding_batch_size: int,
) -> KnowledgeSearchClient:
    documents = await _scenario_documents(dependencies)
    index = InMemoryTieredVectorIndex()
    indexing = NativeKnowledgeIndexingService(
        embedding=embedding,
        index=index,
        embedding_version=embedding_version,
        embedding_batch_size=embedding_batch_size,
    )
    report = await indexing.upsert_documents(
        tenant_id=dependencies.tenant_id,
        documents=documents,
    )
    if not report.is_clean or report.written != len(documents):
        raise RuntimeError("synthetic knowledge index did not become ready")
    return TieredVectorKnowledgeSearchClient(
        embedder=ModelQueryEmbedder(embedding, version=embedding_version),
        index=index,
        tiers=(VectorTier.HOT,),
    )


async def seed_native_e2e_knowledge(
    dependencies: HotNewsE2EDependencies,
    *,
    store: PostgresKnowledgeStore,
) -> None:
    documents = await _scenario_documents(dependencies)
    report = await store.upsert_documents(
        tenant_id=dependencies.tenant_id, documents=documents
    )
    if not report.is_clean or len(report.outcomes) != len(documents):
        raise RuntimeError("synthetic PostgreSQL knowledge index did not become ready")


async def _scenario_documents(
    dependencies: HotNewsE2EDependencies,
) -> tuple[KnowledgeDocument, ...]:
    contents = await dependencies.content_repository.batch_get_by_news_ids(
        tenant_id=dependencies.tenant_id,
        news_ids=dependencies.news_ids,
    )
    if len(contents) != len(dependencies.news_ids):
        raise ValueError("synthetic scenario is missing news content")
    documents = tuple(
        KnowledgeDocument(
            document_id=f"news:{content.news_id}",
            title=content.title,
            text=content.body or content.summary,
            media_type=content.content_type.value,
            text_source="synthetic-scenario",
            source_url=content.source_url,
            metadata={
                "news_id": content.news_id,
                "content_version": "1",
                "publish_time": content.publish_time.isoformat(),
            },
        )
        for content in (contents[news_id] for news_id in dependencies.news_ids)
    )
    return documents
