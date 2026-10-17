"""构建共享聊天/热门新闻搜索索引，无需更改摄取存储。"""

import os

from app.clients.enterprise.milvus_vector import MilvusTieredVectorIndex
from app.modules.knowledge.search_config import load_knowledge_search_config
from app.model_runtime.query_embedding import ModelQueryEmbedder

"""这边读取YAML配置文件,判断是连接 Postgre 还是连接 Miluvs"""
def build_knowledge_search_index(*, config_path: str | None, postgres_store):
    if config_path is None:
        return postgres_store
    config = load_knowledge_search_config(config_path)
    if config.backend == "postgres":
        return postgres_store
    if config.backend == "bge_snapshot":
        from app.retrieval.bge_snapshot import BgeSnapshotVectorIndex

        return BgeSnapshotVectorIndex.connect(config.bge_snapshot)
    connection = config.milvus
    token = None
    if connection.token_env:
        token = os.environ.get(connection.token_env)
        if not token or not token.strip():
            raise ValueError("Milvus token environment variable is missing or empty")
    return MilvusTieredVectorIndex.connect(
        uri=connection.uri, token=token, database=connection.database,
        collections=connection.collections, timeout_seconds=connection.timeout_seconds,
        metric_type=connection.metric_type, search_params=connection.search_params,
    )


async def close_knowledge_search_index(index) -> None:
    from app.retrieval.bge_snapshot import BgeSnapshotVectorIndex
    if getattr(index, "lifecycle_guarded", False):
        await index.close()
        return

    if isinstance(index, (MilvusTieredVectorIndex, BgeSnapshotVectorIndex)):
        await index.close()


def build_knowledge_query_embedder(*, index, embedding, embedding_version: str):
    """Keep prebuilt BGE vectors paired with the matching local query encoder."""
    query_embedder = getattr(index, "query_embedder", None)
    if query_embedder is not None:
        return query_embedder
    return ModelQueryEmbedder(embedding, version=embedding_version)
