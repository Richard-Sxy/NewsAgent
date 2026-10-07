"""
将 Embedding、PostgreSQL 知识存储和向量检索组装起来。
"""

from __future__ import annotations

from app.db.session import Database
from app.knowledge.postgres_store import PostgresKnowledgeStore
from app.model_runtime.config_file import ModelRuntimeConfig
from app.model_runtime.core import EmbeddingPort
from app.model_runtime.query_embedding import ModelQueryEmbedder
from app.retrieval.tiered_vector import (
    TieredVectorKnowledgeSearchClient,
    VectorTier,
)


def build_native_knowledge_search(
    *,
    config: ModelRuntimeConfig,
    embedding: EmbeddingPort,
    database: Database,
) -> TieredVectorKnowledgeSearchClient:
    index = PostgresKnowledgeStore(
        database=database,
        embedding=embedding,
        embedding_version=config.embedding.model_routes[0],
        embedding_batch_size=config.embedding.max_batch_size,
    )
    return TieredVectorKnowledgeSearchClient(
        embedder=ModelQueryEmbedder(
            embedding, version=config.embedding.model_routes[0]
        ),
        index=index,
        tiers=(VectorTier.HOT,),
    )
