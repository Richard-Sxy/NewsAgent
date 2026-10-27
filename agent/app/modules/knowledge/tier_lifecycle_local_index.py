"""Persistent exact-search substitute, restricted to isolated local E2E runs.

This exercises the production index Port and persistence boundaries. Its SQL
scan latency is not an ANN / enterprise Milvus performance measurement.
"""
from __future__ import annotations

import math
from sqlalchemy import select, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.modules.knowledge.tier_lifecycle_store import _vectors
from app.modules.knowledge.tier_lifecycle import record_from_dict, record_to_dict
from app.retrieval.tiered_vector import VectorTier, VectorSearchHit


class LocalLifecycleVectorIndex:
    tiers = tuple(VectorTier)
    similarity_metric = "IP"

    def __init__(self, *, database, max_search_rows, environment):
        if environment != "e2e":
            raise ValueError("local lifecycle vector index is restricted to e2e")
        self.database, self.max_search_rows = database, max_search_rows

    async def validate_collections(self, *, expected_dimensions, query_max_candidates=None):
        async with self.database.session() as session:
            await session.execute(select(_vectors.c.chunk_id).limit(0))
        return {tier.value: {"status": "ready", "backend": "local_exact_sql",
                "dimensions": expected_dimensions} for tier in self.tiers}

    async def upsert(self, *, tier, records):
        dialect = self.database.engine.dialect.name
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        async with self.database.session() as session:
            for record in records:
                if not record.vector or any(not math.isfinite(v) for v in record.vector):
                    raise ValueError("invalid vector")
                values = dict(chunk_id=record.chunk_id, tenant_id=record.tenant_id,
                              tier=tier.value, record=record_to_dict(record))
                await session.execute(insert(_vectors).values(**values).on_conflict_do_update(
                    index_elements=[_vectors.c.chunk_id], set_=values))
        return len(records)

    async def contains(self, *, tier, chunk_ids):
        async with self.database.session() as session:
            result = await session.execute(select(_vectors.c.chunk_id).where(
                _vectors.c.tier == tier.value, _vectors.c.chunk_id.in_(chunk_ids)))
            return frozenset(result.scalars())

    async def verify_records(self, *, tier, records):
        async with self.database.session() as session:
            result = await session.execute(select(_vectors.c.chunk_id, _vectors.c.record).where(
                _vectors.c.tier == tier.value, _vectors.c.chunk_id.in_([r.chunk_id for r in records])))
            found = {row.chunk_id: row.record for row in result}
        return all(found.get(record.chunk_id) == record_to_dict(record) for record in records)

    async def delete(self, *, tier, chunk_ids):
        async with self.database.session() as session:
            result = await session.execute(delete(_vectors).where(
                _vectors.c.tier == tier.value, _vectors.c.chunk_id.in_(chunk_ids)))
            return result.rowcount

    async def search(self, *, tier, tenant_id, vector, embedding_version, exclude_news_ids, limit):
        if limit <= 0 or not vector or any(not math.isfinite(v) for v in vector):
            raise ValueError("invalid search")
        async with self.database.session() as session:
            result = await session.execute(select(_vectors.c.record).where(
                _vectors.c.tenant_id == tenant_id, _vectors.c.tier == tier.value).limit(self.max_search_rows + 1))
            rows = list(result.scalars())
        if len(rows) > self.max_search_rows:
            raise ValueError("local exact-search row cap exceeded")
        hits = []
        for value in rows:
            record = record_from_dict(value)
            if record.embedding_version != embedding_version or record.news_id in exclude_news_ids:
                continue
            if len(record.vector) != len(vector):
                raise ValueError("query/index dimension mismatch")
            score = sum(a*b for a, b in zip(record.vector, vector, strict=True))
            hits.append(VectorSearchHit(record.chunk_id, record.news_id, record.title,
                record.excerpt, record.source_url, record.publish_time, score, tier,
                record.content_version, record.embedding_version))
        return sorted(hits, key=lambda hit: (-hit.raw_score, hit.chunk_id))[:limit]

    async def close(self):
        pass
