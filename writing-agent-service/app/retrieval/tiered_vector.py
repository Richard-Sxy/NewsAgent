"""Production-facing contracts for tiered news vector retrieval.

The domain service in this module deliberately does not import ``pymilvus``.
Milvus is a rebuildable search projection; callers still own canonical chunk
text and version metadata outside the vector database.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, Sequence

from app.clients.knowledge_base import (
    KnowledgeSearchClient,
    RelatedNews,
    RelatedNewsSearchQuery,
)


class VectorTier(StrEnum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


@dataclass(frozen=True, slots=True)
class TierSearchPolicy:
    """Versioned knobs used by one retrieval request."""

    version: str = "tiered-rrf-v1"
    rrf_k: int = 60
    per_tier_limit: int = 50
    hot_weight: float = 1.0
    warm_weight: float = 1.0
    cold_weight: float = 0.7

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("retrieval policy version cannot be empty")
        if self.rrf_k <= 0:
            raise ValueError("rrf_k must be greater than 0")
        if self.per_tier_limit <= 0:
            raise ValueError("per_tier_limit must be greater than 0")
        if min(self.weights.values()) < 0:
            raise ValueError("tier weights cannot be negative")

    @property
    def weights(self) -> dict[VectorTier, float]:
        return {
            VectorTier.HOT: self.hot_weight,
            VectorTier.WARM: self.warm_weight,
            VectorTier.COLD: self.cold_weight,
        }


@dataclass(frozen=True, slots=True)
class ChunkVectorRecord:
    """A versioned vector projection row, not the canonical article."""

    chunk_id: str
    tenant_id: str
    news_id: str
    content_version: int
    chunk_index: int
    embedding_version: str
    title: str
    excerpt: str
    source_url: str | None
    publish_time: datetime
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class VectorSearchHit:
    chunk_id: str
    news_id: str
    title: str
    excerpt: str
    source_url: str | None
    publish_time: datetime | None
    raw_score: float
    tier: VectorTier
    content_version: int
    embedding_version: str


class QueryEmbeddingPort(Protocol):
    @property
    def version(self) -> str: ...

    async def embed_queries(
        self,
        texts: Sequence[str],
    ) -> list[tuple[float, ...]]: ...


class TieredVectorIndexPort(Protocol):
    """Storage boundary implemented by Milvus or an enterprise vector RPC."""

    async def search(
        self,
        *,
        tier: VectorTier,
        tenant_id: str,
        vector: Sequence[float],
        embedding_version: str,
        exclude_news_ids: frozenset[str],
        limit: int,
    ) -> list[VectorSearchHit]: ...

    async def upsert(
        self,
        *,
        tier: VectorTier,
        records: Sequence[ChunkVectorRecord],
    ) -> int: ...

    async def contains(
        self,
        *,
        tier: VectorTier,
        chunk_ids: Sequence[str],
    ) -> frozenset[str]: ...

    async def delete(
        self,
        *,
        tier: VectorTier,
        chunk_ids: Sequence[str],
    ) -> int: ...


class TieredVectorKnowledgeSearchClient(KnowledgeSearchClient):
    """Three-tier concurrent recall followed by rank-only weighted RRF."""

    def __init__(
        self,
        *,
        embedder: QueryEmbeddingPort,
        index: TieredVectorIndexPort,
        policy: TierSearchPolicy | None = None,
        tiers: tuple[VectorTier, ...] = (
            VectorTier.HOT,
            VectorTier.WARM,
            VectorTier.COLD,
        ),
        max_query_concurrency: int = 8,
    ) -> None:
        if max_query_concurrency <= 0:
            raise ValueError("max_query_concurrency must be greater than 0")
        if not tiers:
            raise ValueError("at least one vector tier is required")
        self._embedder = embedder
        self._index = index
        self._policy = policy or TierSearchPolicy()
        self._tiers = tuple(dict.fromkeys(tiers))
        self._max_query_concurrency = max_query_concurrency

    async def batch_search_related_news(
        self,
        queries: tuple[RelatedNewsSearchQuery, ...],
        *,
        tenant_id: str,
    ) -> dict[str, list[RelatedNews]]:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        for query in queries:
            query.validate()
        query_ids = [query.query_id for query in queries]
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("queries cannot contain duplicate query_id values")
        if not queries:
            return {}

        vectors = await self._embedder.embed_queries(
            [query.query_text for query in queries]
        )
        if len(vectors) != len(queries):
            raise RuntimeError("embedding response count does not match query count")

        semaphore = asyncio.Semaphore(self._max_query_concurrency)

        async def search_one(
            query: RelatedNewsSearchQuery,
            vector: tuple[float, ...],
        ) -> tuple[str, list[RelatedNews]]:
            excluded = frozenset(
                (*query.exclude_news_ids, query.source_news_id)
            )
            async with semaphore:
                tier_results = await asyncio.gather(
                    *(
                        self._index.search(
                            tier=tier,
                            tenant_id=tenant_id,
                            vector=vector,
                            embedding_version=self._embedder.version,
                            exclude_news_ids=excluded,
                            limit=max(
                                self._policy.per_tier_limit,
                                query.candidate_limit,
                            ),
                        )
                        for tier in self._tiers
                    )
                )
            return query.query_id, self._fuse(
                query=query,
                tier_results=zip(self._tiers, tier_results, strict=True),
            )

        pairs = await asyncio.gather(
            *(
                search_one(query, vector)
                for query, vector in zip(queries, vectors, strict=True)
            )
        )
        return dict(pairs)

    def _fuse(
        self,
        *,
        query: RelatedNewsSearchQuery,
        tier_results,
    ) -> list[RelatedNews]:
        scores: dict[str, float] = {}
        representatives: dict[str, VectorSearchHit] = {}
        max_score = sum(
            self._policy.weights[tier] / (self._policy.rrf_k + 1)
            for tier in self._tiers
        )

        for tier, hits in tier_results:
            seen_news: set[str] = set()
            rank = 0
            for hit in hits:
                if hit.news_id in seen_news:
                    continue
                seen_news.add(hit.news_id)
                rank += 1
                scores[hit.news_id] = scores.get(hit.news_id, 0.0) + (
                    self._policy.weights[tier]
                    / (self._policy.rrf_k + rank)
                )
                current = representatives.get(hit.news_id)
                if current is None or hit.raw_score > current.raw_score:
                    representatives[hit.news_id] = hit

        ranked_ids = sorted(
            scores,
            key=lambda news_id: (-scores[news_id], news_id),
        )[: query.candidate_limit]
        return [
            RelatedNews(
                collection_id=representatives[news_id].chunk_id,
                news_id=news_id,
                title=representatives[news_id].title,
                text=representatives[news_id].excerpt,
                source_url=representatives[news_id].source_url,
                publish_time=(
                    representatives[news_id].publish_time.isoformat()
                    if representatives[news_id].publish_time is not None
                    else None
                ),
                score=(scores[news_id] / max_score if max_score else 0.0),
            )
            for news_id in ranked_ids
        ]


class TierMigrationService:
    """Copy, verify, then delete. Ledger/routing is updated by the caller."""

    def __init__(self, index: TieredVectorIndexPort) -> None:
        self._index = index

    async def migrate(
        self,
        *,
        source: VectorTier,
        target: VectorTier,
        records: Sequence[ChunkVectorRecord],
    ) -> int:
        if source == target or not records:
            return 0
        chunk_ids = tuple(record.chunk_id for record in records)
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("migration records contain duplicate chunk_id values")

        await self._index.upsert(tier=target, records=records)
        present = await self._index.contains(
            tier=target,
            chunk_ids=chunk_ids,
        )
        missing = set(chunk_ids) - present
        if missing:
            raise RuntimeError(
                f"target tier verification failed for {len(missing)} chunks"
            )
        await self._index.delete(tier=source, chunk_ids=chunk_ids)
        return len(chunk_ids)
