"""构建共享聊天/热门新闻搜索索引，无需更改摄取存储。"""

import os

from app.clients.enterprise.milvus_vector import MilvusTieredVectorIndex
from app.knowledge.search_config import load_knowledge_search_config

"""这边读取YAML配置文件,判断是连接 Postgre 还是连接 Miluvs"""
def build_knowledge_search_index(*, config_path: str | None, postgres_store):
    if config_path is None:
        return postgres_store
    config = load_knowledge_search_config(config_path)
    if config.backend == "postgres":
        return postgres_store
    connection = config.milvus
    token = None
    if connection.token_env:
        token = os.environ.get(connection.token_env)
        if not token or not token.strip():
            raise ValueError("Milvus token environment variable is missing or em")
        if not token or not token.strip():
            raise ValueError("Milvus token environment variable is missing or empty")
    return MilvusTieredVectorIndex.connect(
        uri=connection.uri, token=token, database=connection.database,
        collections=connection.collections, timeout_seconds=connection.timeout_seconds,
        metric_type=connection.metric_type, search_params=connection.search_params,
    )


async def close_knowledge_search_index(index) -> None:
    if isinstance(index, MilvusTieredVectorIndex):
        await index.close()
