"""One timed, guarded retrieval path shared by chat, hotspot and benchmark API."""
from __future__ import annotations

import asyncio
import math
from time import perf_counter
from app.clients.knowledge_base import RelatedNews
from app.retrieval.tiered_vector import VectorTier


class LifecycleQueryUnavailable(RuntimeError):
    """A redacted dependency failure with request-local diagnostic timings."""
    code = "knowledge_search_unavailable"

    def __init__(self, trace):
        super().__init__(self.code)
        self.trace = trace


class LifecycleQueryTimeout(TimeoutError):
    code = "knowledge_search_timeout"

    def __init__(self, trace):
        super().__init__(self.code)
        self.trace = trace


async def _gather_or_cancel(*coroutines):
    """A failed/cancelled request must not leave sibling layer tasks running."""
    tasks = [asyncio.create_task(call) for call in coroutines]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


class LifecycleKnowledgeSearch:
    def __init__(self, *, index, ledger, embedder, config):
        self.index, self.ledger, self.embedder, self.config = index, ledger, embedder, config
        self._semaphore = asyncio.Semaphore(config.query_max_concurrency)

    async def search(self, *, query_text, tenant_id, limit=None, exclude_news_ids=()):
        limit = self.config.query_candidate_limit if limit is None else limit
        if not tenant_id.strip() or not query_text.strip() or not 1 <= limit <= self.config.query_max_candidates:
            raise ValueError("invalid lifecycle query")
        started = perf_counter()
        trace = {"trace_schema_version": 2, "policy_version": self.config.policy_version,
                 "embedding_version": self.config.embedding_version,
                 "stage": "queue", "status": "running", "tiers": {}}
        try:
            async with asyncio.timeout(self.config.query_request_timeout_seconds):
                async with self._semaphore:
                    return await self._search(query_text=query_text, tenant_id=tenant_id,
                        limit=limit, exclude_news_ids=exclude_news_ids, trace=trace, started=started)
        except TimeoutError as exc:
            trace.update(status="timeout", error_type=type(exc).__name__)
            raise LifecycleQueryTimeout(trace) from None
        except asyncio.CancelledError:
            trace["status"] = "cancelled"
            raise
        except Exception as exc:
            # SDK/model errors may contain endpoint credentials or input text.
            trace.update(status="failed", error_type=type(exc).__name__)
            raise LifecycleQueryUnavailable(trace) from None
        finally:
            trace["total_ms"] = (perf_counter() - started) * 1000
            if trace["stage"] == "queue":
                trace["queue_ms"] = trace["total_ms"]

    async def _search(self, *, query_text, tenant_id, limit, exclude_news_ids, trace, started):
        queued = perf_counter()
        trace.update(queue_ms=(queued-started)*1000, stage="embedding")
        try:
            vectors = await self.embedder.embed_queries((query_text,), tenant_id=tenant_id)
            if len(vectors) != 1:
                raise ValueError("query embedding count drift")
            if (self.embedder.version != self.config.embedding_version
                    or len(vectors[0]) != self.config.expected_embedding_dimensions
                    or any(not math.isfinite(value) for value in vectors[0])):
                raise ValueError("query embedding model, dimension or coordinate drift")
        finally:
            trace["embedding_ms"] = (perf_counter()-queued)*1000
        trace["stage"] = "tiers"

        async def layer(tier):
            begin = perf_counter()
            layer_trace = {"status": "running", "components_available": False}
            trace["tiers"][tier.value] = layer_trace
            kwargs = dict(tier=tier, tenant_id=tenant_id, vector=vectors[0],
                embedding_version=self.embedder.version, exclude_news_ids=frozenset(exclude_news_ids),
                limit=max(limit, self.config.query_candidate_limit))
            try:
                traced = getattr(self.index, "search_with_trace", None)
                if traced is not None:
                    hits, _ = await traced(**kwargs, trace=layer_trace)
                else:
                    hits = await self.index.search(**kwargs)
                layer_trace.update(status="complete", hits=len(hits))
                return hits
            except BaseException as exc:
                layer_trace.update(status="interrupted", error_type=type(exc).__name__)
                raise
            finally:
                layer_trace["search_and_gate_ms"] = (perf_counter()-begin)*1000

        tiers = tuple(VectorTier)
        results = await _gather_or_cancel(*(layer(tier) for tier in tiers))
        # Reconcile mutations between the separate layer reads before fusion.
        trace["stage"] = "final_gate"
        gate_started = perf_counter()
        try:
            allowed = await self.ledger.filter_hits(tenant_id=tenant_id,
                hits=[hit for hits in results for hit in hits])
            valid = {(hit.tier, hit.chunk_id) for hit in allowed}
        finally:
            trace["final_gate_ms"] = (perf_counter()-gate_started)*1000
        trace["stage"] = "fusion"
        fused_at = perf_counter()
        scores, representatives = {}, {}
        for tier, hits in zip(tiers, results, strict=True):
            seen = set()
            for hit in hits:
                if (tier, hit.chunk_id) not in valid or hit.news_id in seen:
                    continue
                seen.add(hit.news_id)
                scores[hit.news_id] = self.config.query_weights[tier] / (self.config.query_rrf_k + len(seen))
                representatives[hit.news_id] = hit
        ids = sorted(scores, key=lambda key: (-scores[key], key))[:limit]
        hits = [representatives[key] for key in ids]
        trace.update(fusion_ms=(perf_counter()-fused_at)*1000, returned_news=len(hits),
                     stage="complete", status="complete")
        return {"hits": hits, "scores": scores, "trace": trace}

    async def batch_search_related_news(self, queries, *, tenant_id):
        if len({q.query_id for q in queries}) != len(queries):
            raise ValueError("query ids must be unique")
        async def one(query):
            query.validate()
            result = await self.search(query_text=query.query_text, tenant_id=tenant_id,
                limit=query.candidate_limit,
                exclude_news_ids=(*query.exclude_news_ids, query.source_news_id))
            max_score = max(self.config.query_weights.values()) / (self.config.query_rrf_k + 1)
            return query.query_id, [RelatedNews(collection_id=hit.chunk_id, news_id=hit.news_id,
                title=hit.title, text=hit.excerpt, source_url=hit.source_url,
                publish_time=hit.publish_time.isoformat() if hit.publish_time else None,
                score=result["scores"][hit.news_id]/max_score if max_score else 0.0) for hit in result["hits"]]
        return dict(await _gather_or_cancel(*(one(query) for query in queries)))
