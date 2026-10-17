"""Filter vector candidates against the durable news lifecycle before fusion."""

from __future__ import annotations

import inspect
from time import perf_counter
from typing import Protocol, Sequence

from app.retrieval.tiered_vector import (
    ChunkVectorRecord,
    TieredVectorIndexPort,
    VectorSearchHit,
    VectorTier,
)


class LifecycleHitLedger(Protocol):
    async def filter_hits(
        self, *, tenant_id: str, hits: Sequence[VectorSearchHit]
    ) -> list[VectorSearchHit]: ...


class GuardedTieredIndex:
    """Bounded refill plus a fail-closed gate for every layer's candidates.

    The ledger validates tenant, projection identity, version, current tier and
    withdrawal state. Its read is the search's lifecycle linearization point:
    searches started after a committed mutation cannot serve the previous
    state. Searches overlapping a mutation may observe either valid state.
    There is no state cache, fallback to unguarded hits or error suppression.
    """

    lifecycle_guarded = True

    def __init__(
        self,
        index: TieredVectorIndexPort,
        ledger: LifecycleHitLedger,
        *,
        max_candidates: int = 1000,
        refill_factor: int = 2,
        max_refill_rounds: int = 3,
    ) -> None:
        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or max_candidates < 1:
            raise ValueError("max_candidates must be a positive integer")
        if isinstance(refill_factor, bool) or not isinstance(refill_factor, int) or refill_factor < 2:
            raise ValueError("refill_factor must be an integer of at least 2")
        if isinstance(max_refill_rounds, bool) or not isinstance(max_refill_rounds, int) or max_refill_rounds < 0:
            raise ValueError("max_refill_rounds must be a non-negative integer")
        self._index = index
        self._ledger = ledger
        self._max_candidates = max_candidates
        self._refill_factor = refill_factor
        self._max_refill_rounds = max_refill_rounds
        if hasattr(index, "query_embedder"):
            self.query_embedder = index.query_embedder

    @property
    def tiers(self) -> tuple[VectorTier, ...]:
        return tuple(getattr(self._index, "tiers", tuple(VectorTier)))

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
        hits, _ = await self.search_with_trace(tier=tier, tenant_id=tenant_id,
            vector=vector, embedding_version=embedding_version,
            exclude_news_ids=exclude_news_ids, limit=limit)
        return hits

    async def search_with_trace(self, *, tier, tenant_id, vector, embedding_version,
                                exclude_news_ids, limit, trace=None):
        """Request-local timings include every refill, never global mutable state.

        vector_search_ms measures the adapter await (SDK queue, network and
        response mapping included), not Milvus server-only execution time.
        state_gate_ms measures the ledger await; filter_ms is local filtering.
        The optional sink preserves completed stages on cancellation/failure.
        """
        if limit < 1 or limit > self._max_candidates:
            raise ValueError("search limit must be between 1 and max_candidates")
        trace = {} if trace is None else trace
        trace.update(vector_search_ms=0.0, state_gate_ms=0.0, filter_ms=0.0,
                     rounds=[], refill_rounds=0, hits=0, status="running",
                     components_available=True)
        started = perf_counter()
        try:
            hits = await self._search_rounds(tier=tier, tenant_id=tenant_id, vector=vector,
                embedding_version=embedding_version, exclude_news_ids=exclude_news_ids,
                limit=limit, trace=trace)
            trace.update(hits=len(hits), status="complete")
            return hits, trace
        except BaseException as exc:
            trace.update(status="interrupted", error_type=type(exc).__name__)
            raise
        finally:
            trace["search_and_gate_ms"] = (perf_counter() - started) * 1000

    async def _search_rounds(self, *, tier, tenant_id, vector, embedding_version,
                              exclude_news_ids, limit, trace):
        requested = limit
        for round_number in range(self._max_refill_rounds + 1):
            current = {"requested": requested, "retrieved": None, "accepted": None}
            trace["rounds"].append(current)
            trace["refill_rounds"] = round_number
            begin = perf_counter()
            try:
                candidates = await self._index.search(
                    tier=tier, tenant_id=tenant_id, vector=vector,
                    embedding_version=embedding_version,
                    exclude_news_ids=exclude_news_ids, limit=requested)
                current["retrieved"] = len(candidates)
            finally:
                current["vector_search_ms"] = (perf_counter() - begin) * 1000
                trace["vector_search_ms"] += current["vector_search_ms"]
            # Recheck the whole latest candidate set on every refill. Carrying
            # approvals between rounds could return a revision withdrawn while
            # a later remote search was in flight.
            begin = perf_counter()
            try:
                approved = await self._ledger.filter_hits(tenant_id=tenant_id, hits=candidates)
            finally:
                current["state_gate_ms"] = (perf_counter() - begin) * 1000
                trace["state_gate_ms"] += current["state_gate_ms"]
            begin = perf_counter()
            approved_keys = {
                (hit.chunk_id, hit.news_id, hit.tier, hit.content_version, hit.embedding_version)
                for hit in approved
            }
            filtered = [
                hit for hit in candidates
                if (hit.chunk_id, hit.news_id, hit.tier, hit.content_version, hit.embedding_version)
                in approved_keys
            ]
            current["accepted"] = len(filtered)
            current["filter_ms"] = (perf_counter() - begin) * 1000
            trace["filter_ms"] += current["filter_ms"]
            if (
                len(filtered) >= limit
                or len(candidates) < requested
                or requested >= self._max_candidates
                or round_number == self._max_refill_rounds
            ):
                return filtered[:limit]
            requested = min(requested * self._refill_factor, self._max_candidates)
        raise AssertionError("unreachable bounded refill state")

    async def upsert(
        self, *, tier: VectorTier, records: Sequence[ChunkVectorRecord]
    ) -> int:
        return await self._index.upsert(tier=tier, records=records)

    async def validate_collections(self, *, expected_dimensions, query_max_candidates=None):
        return await self._index.validate_collections(expected_dimensions=expected_dimensions,
            query_max_candidates=query_max_candidates)

    async def contains(
        self, *, tier: VectorTier, chunk_ids: Sequence[str]
    ) -> frozenset[str]:
        return await self._index.contains(tier=tier, chunk_ids=chunk_ids)

    async def verify_records(
        self, *, tier: VectorTier, records: Sequence[ChunkVectorRecord]
    ) -> bool:
        verifier = getattr(self._index, "verify_records", None)
        if verifier is None:
            raise RuntimeError("lifecycle index must support full record verification")
        return await verifier(tier=tier, records=records)

    async def delete(self, *, tier: VectorTier, chunk_ids: Sequence[str]) -> int:
        return await self._index.delete(tier=tier, chunk_ids=chunk_ids)

    async def close(self) -> None:
        closer = getattr(self._index, "close", None)
        if closer is not None:
            result = closer()
            if inspect.isawaitable(result):
                await result
