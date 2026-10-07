"""本地内容向量索引。实现向量写入、检索、存在检查和删除。检索时按租户与 Embedding 版本过滤。用于本地验收，进程退出后数据不会保留。"""

from __future__ import annotations

import math
from typing import Sequence

from app.retrieval.tiered_vector import (
    ChunkVectorRecord,
    VectorSearchHit,
    VectorTier,
)


class InMemoryTieredVectorIndex:
    def __init__(self) -> None:
        self._rows: dict[VectorTier, dict[str, ChunkVectorRecord]] = {
            tier: {} for tier in VectorTier
        }

    async def upsert(
        self,
        *,
        tier: VectorTier,
        records: Sequence[ChunkVectorRecord],
    ) -> int:
        for record in records:
            if not record.chunk_id or not record.tenant_id or not record.news_id:
                raise ValueError("vector record identity is incomplete")
            if not record.vector or any(not math.isfinite(v) for v in record.vector):
                raise ValueError("vector record contains invalid coordinates")
            self._rows[tier][record.chunk_id] = record
        return len(records)

    async def contains(
        self,
        *,
        tier: VectorTier,
        chunk_ids: Sequence[str],
    ) -> frozenset[str]:
        return frozenset(chunk_id for chunk_id in chunk_ids if chunk_id in self._rows[tier])

    async def delete(
        self,
        *,
        tier: VectorTier,
        chunk_ids: Sequence[str],
    ) -> int:
        removed = 0
        for chunk_id in chunk_ids:
            if self._rows[tier].pop(chunk_id, None) is not None:
                removed += 1
        return removed

    async def search(
        self,
        *,
        tier: VectorTier,
        tenant_id: str,
        vector: Sequence[float],
        embedding_version: str,
        exclude_news_ids: frozenset[str],
        limit: int,
    ) -> list[VectorSearchHit]:
        if not tenant_id.strip() or not embedding_version.strip() or limit <= 0:
            raise ValueError("vector search context is invalid")
        if not vector or any(not math.isfinite(value) for value in vector):
            raise ValueError("query vector is invalid")
        scored: list[tuple[float, ChunkVectorRecord]] = []
        for record in self._rows[tier].values():
            if record.tenant_id != tenant_id:
                continue
            if record.embedding_version != embedding_version:
                continue
            if record.news_id in exclude_news_ids:
                continue
            if len(record.vector) != len(vector):
                raise ValueError("query/index embedding dimensions differ")
            score = sum(a * b for a, b in zip(record.vector, vector, strict=True))
            scored.append((score, record))
        scored.sort(key=lambda row: (-row[0], row[1].chunk_id))
        return [
            VectorSearchHit(
                chunk_id=record.chunk_id,
                news_id=record.news_id,
                title=record.title,
                excerpt=record.excerpt,
                source_url=record.source_url,
                publish_time=record.publish_time,
                raw_score=score,
                tier=tier,
                content_version=record.content_version,
                embedding_version=record.embedding_version,
            )
            for score, record in scored[:limit]
        ]
