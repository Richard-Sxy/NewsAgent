"""Opt-in read-only Milvus contract check; never creates collections or writes data."""

import os

import pytest

from app.knowledge.search_factory import build_knowledge_search_index, close_knowledge_search_index
from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.core import EmbeddingRequest
from app.model_runtime.factory import build_model_runtime_ports


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_milvus_search_contract():
    if os.environ.get("NEWSAGENT_MILVUS_LIVE") != "1":
        pytest.skip("set NEWSAGENT_MILVUS_LIVE=1 to enable real read-only search")
    path = os.environ["KNOWLEDGE_SEARCH_CONFIG_PATH"]
    model = load_model_runtime_config(os.environ["MODEL_RUNTIME_CONFIG_PATH"])
    ports = build_model_runtime_ports(model, environment="production")
    index = None
    try:
        index = build_knowledge_search_index(config_path=path, postgres_store=None)
        assert index is not None and hasattr(index, "tiers"), "live check requires Milvus YAML"
        version = model.embedding.model_routes[0]
        tenant = os.environ["NEWSAGENT_MILVUS_TEST_TENANT_ID"]
        embedding = await ports.embedding.embed(EmbeddingRequest(
            tenant_id=tenant, trace_id="milvus-contract-test", model_route=version,
            texts=(os.environ.get("NEWSAGENT_MILVUS_TEST_QUERY", "新闻"),),
        ))
        assert embedding.model_version == version and len(embedding.vectors) == 1
        hits = []
        for tier in index.tiers:
            hits.extend(await index.search(tier=tier, tenant_id=tenant,
                vector=embedding.vectors[0], embedding_version=version,
                exclude_news_ids=frozenset(), limit=3))
        assert hits, "no results: check tenant, embedding version and collection data"
        assert all(hit.news_id and hit.chunk_id and hit.embedding_version == version for hit in hits)
    finally:
        await close_knowledge_search_index(index)
        await ports.close()
