"""Opt-in three-tier read/write contract against pre-provisioned test collections.

Uses synthetic unit vectors and a random isolated tenant, not an enterprise
Embedding quality test. Never creates collections/indexes or deletes other IDs.
"""
from datetime import UTC, datetime
import hashlib
import os
from uuid import uuid4

import pytest

from app.clients.enterprise.milvus_vector import MilvusTieredVectorIndex
from app.knowledge.search_config import load_knowledge_search_config
from app.knowledge.tier_lifecycle_config import load_knowledge_lifecycle_config
from app.retrieval.tiered_vector import ChunkVectorRecord, VectorTier


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_three_tier_write_verify_search_delete_contract():
    if os.environ.get("NEWSAGENT_MILVUS_LIFECYCLE_LIVE") != "1":
        pytest.skip("set NEWSAGENT_MILVUS_LIFECYCLE_LIVE=1 to write isolated synthetic test rows")
    search = load_knowledge_search_config(os.environ["KNOWLEDGE_SEARCH_CONFIG_PATH"])
    cfg = load_knowledge_lifecycle_config(os.environ["KNOWLEDGE_LIFECYCLE_CONFIG_PATH"])
    assert search.backend == "milvus" and set(search.milvus.collections) == set(VectorTier)
    connection = search.milvus
    token = os.environ.get(connection.token_env) if connection.token_env else None
    assert not connection.token_env or token, "Milvus test credential is missing"
    index = MilvusTieredVectorIndex.connect(uri=connection.uri, token=token,
        database=connection.database, collections=connection.collections,
        timeout_seconds=connection.timeout_seconds, metric_type=connection.metric_type,
        search_params=connection.search_params, consistency_level="Strong")
    tenant, news = str(uuid4()), "lifecycle-contract-" + uuid4().hex
    vector = (1.0,) + (0.0,) * (cfg.expected_embedding_dimensions - 1)
    records = {tier: ChunkVectorRecord(
        chunk_id=hashlib.sha256(f"{tenant}:{tier}".encode()).hexdigest(), tenant_id=tenant,
        news_id=news, content_version=1, chunk_index=0, embedding_version=cfg.embedding_version,
        title="synthetic lifecycle contract", excerpt="synthetic isolated test row",
        source_url=None, publish_time=datetime.now(UTC), vector=vector,
    ) for tier in VectorTier}
    attempted, cleanup_errors = [], []
    try:
        await index.validate_collections(expected_dimensions=cfg.expected_embedding_dimensions,
            query_max_candidates=cfg.query_max_candidates)
        for tier, record in records.items():
            attempted.append(tier)  # A failed write response can still mean the row was written.
            await index.upsert(tier=tier, records=[record])
            assert await index.verify_records(tier=tier, records=[record])
            hits = await index.search(tier=tier, tenant_id=tenant, vector=vector,
                embedding_version=cfg.embedding_version, exclude_news_ids=frozenset(), limit=1)
            assert len(hits) == 1 and hits[0].chunk_id == record.chunk_id
            assert not await index.search(tier=tier, tenant_id=str(uuid4()), vector=vector,
                embedding_version=cfg.embedding_version, exclude_news_ids=frozenset(), limit=1)
    finally:
        for tier in attempted:
            try:
                ids = [records[tier].chunk_id]
                await index.delete(tier=tier, chunk_ids=ids)
                if await index.contains(tier=tier, chunk_ids=ids):
                    cleanup_errors.append(tier.value)
            except Exception:
                cleanup_errors.append(tier.value)
        await index.close()
        remaining = {tier.value: records[tier].chunk_id for tier in attempted
                     if tier.value in cleanup_errors}
        assert not cleanup_errors, (
            f"isolated live contract cleanup incomplete; synthetic tenant={tenant}; "
            f"synthetic chunk IDs by tier={remaining}")
