"""
将 Embedding 与配置选择的 PostgreSQL/Milvus 向量检索组装起来。
"""

from __future__ import annotations

from app.db.session import Database
from app.knowledge.postgres_store import PostgresKnowledgeStore
from app.knowledge.search_factory import build_knowledge_search_index, build_knowledge_query_embedder
from app.model_runtime.config_file import ModelRuntimeConfig
from app.model_runtime.core import EmbeddingPort
from app.retrieval.tiered_vector import (
    TieredVectorKnowledgeSearchClient,
    VectorTier,
)

def build_native_knowledge_index(
    *,
    config: ModelRuntimeConfig,
    embedding: EmbeddingPort,
    database: Database,
    config_path: str | None = None,
):
    index = PostgresKnowledgeStore(
        database=database,
        embedding=embedding,
        embedding_version=config.embedding.model_routes[0],
        embedding_batch_size=config.embedding.max_batch_size,
    )
    return build_knowledge_search_index(config_path=config_path, postgres_store=index)

def build_native_knowledge_search(
    *, config: ModelRuntimeConfig | None, embedding: EmbeddingPort | None, database: Database | None,
    index=None,
) -> TieredVectorKnowledgeSearchClient:
    if index is None:
        if config is None or embedding is None or database is None:
            raise ValueError("native knowledge config, embedding and database are required without an index")
        index = build_native_knowledge_index(config=config, embedding=embedding, database=database)
    tiers = getattr(index, "tiers", (VectorTier.HOT,))
    return TieredVectorKnowledgeSearchClient(
        embedder=build_knowledge_query_embedder(
            index=index, embedding=embedding,
            embedding_version=config.embedding.model_routes[0] if config else "",
        ),
        index=index,
        tiers=tiers,
    )
