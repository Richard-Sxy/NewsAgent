"""Latency decomposition, bounded failure and redacted HTTP contracts."""
import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
from pydantic import SecretStr
import pytest

from app.api.knowledge import router
from app.modules.knowledge.tier_lifecycle_index import GuardedTieredIndex
from app.modules.knowledge.tier_lifecycle_query import (
    LifecycleKnowledgeSearch, LifecycleQueryTimeout, LifecycleQueryUnavailable,
)
from app.retrieval.tiered_vector import VectorSearchHit, VectorTier


TENANT = "11111111-1111-4111-8111-111111111111"
AUTH = {"X-Tenant-ID": TENANT, "Authorization": "Bearer local-fixture"}


def config(**changes):
    return SimpleNamespace(**{
        "query_max_concurrency": 1, "query_candidate_limit": 2,
        "query_max_candidates": 8, "query_request_timeout_seconds": 1,
        "embedding_version": "model-v1", "expected_embedding_dimensions": 2,
        "policy_version": "policy-v1", "query_weights": {tier: 1 for tier in VectorTier},
        "query_rrf_k": 60, **changes,
    })


class Embedder:
    version = "model-v1"
    async def embed_queries(self, texts, *, tenant_id):
        return [(1., 0.)]


class Ledger:
    async def filter_hits(self, *, tenant_id, hits):
        return list(hits)


def hit(chunk):
    return VectorSearchHit(chunk_id=chunk, news_id=chunk, title="合成新闻", excerpt="正文",
        source_url=None, publish_time=datetime(2026, 10, 16, tzinfo=UTC), raw_score=1,
        tier=VectorTier.HOT, content_version=1, embedding_version="model-v1")


@pytest.mark.asyncio
async def test_trace_decomposes_every_refill_using_request_local_clocks(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("app.modules.knowledge.tier_lifecycle_index.perf_counter", lambda: clock[0])
    rows = [hit("old-1"), hit("old-2"), hit("new-1"), hit("new-2")]
    class Backend:
        async def search(self, **kwargs):
            clock[0] += 0.020
            return rows[:kwargs["limit"]]
    class Gate:
        async def filter_hits(self, **kwargs):
            clock[0] += 0.003
            return [h for h in kwargs["hits"] if h.chunk_id.startswith("new")]
    index = GuardedTieredIndex(Backend(), Gate(), max_candidates=8)
    hits, trace = await index.search_with_trace(tier=VectorTier.HOT, tenant_id=TENANT,
        vector=(1., 0.), embedding_version="model-v1", exclude_news_ids=(), limit=2)
    assert hits == rows[2:]
    assert trace["vector_search_ms"] == pytest.approx(40)
    assert trace["state_gate_ms"] == pytest.approx(6)
    assert trace["filter_ms"] == 0
    assert trace["search_and_gate_ms"] == pytest.approx(46)
    assert trace["refill_rounds"] == 1
    assert [r["requested"] for r in trace["rounds"]] == [2, 4]
    assert [r["accepted"] for r in trace["rounds"]] == [0, 2]
    assert trace["status"] == "complete"


@pytest.mark.asyncio
async def test_success_trace_keeps_legacy_fields_and_new_layer_components_isolated():
    class Backend:
        async def search(self, **kwargs):
            await asyncio.sleep(0)
            return []
    ledger = Ledger()
    query = LifecycleKnowledgeSearch(index=GuardedTieredIndex(Backend(), ledger),
        ledger=ledger, embedder=Embedder(), config=config(query_max_concurrency=2))
    a, b = await asyncio.gather(*(query.search(query_text="合成", tenant_id=TENANT) for _ in range(2)))
    for result in (a, b):
        trace = result["trace"]
        assert trace["trace_schema_version"] == 2 and trace["status"] == "complete"
        assert all(trace[key] >= 0 for key in ("queue_ms", "embedding_ms", "fusion_ms", "final_gate_ms", "total_ms"))
        assert set(trace["tiers"]) == {"hot", "warm", "cold"}
        for layer in trace["tiers"].values():
            assert layer["components_available"] is True
            assert layer["vector_search_ms"] >= 0 and layer["state_gate_ms"] >= 0
            assert layer["refill_rounds"] == 0 and len(layer["rounds"]) == 1
    assert a["trace"]["tiers"]["hot"]["rounds"] is not b["trace"]["tiers"]["hot"]["rounds"]


@pytest.mark.asyncio
async def test_failed_layer_cancels_and_drains_siblings_before_query_returns():
    all_started = asyncio.Event()
    started, cancelled = set(), set()
    class Backend:
        async def search(self, **kwargs):
            tier = kwargs["tier"]
            started.add(tier)
            if len(started) == 3:
                all_started.set()
            await all_started.wait()
            if tier == VectorTier.HOT:
                raise RuntimeError("secret-token and original text must never escape")
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.add(tier)
                raise
    query = LifecycleKnowledgeSearch(index=Backend(), ledger=Ledger(), embedder=Embedder(), config=config())
    with pytest.raises(LifecycleQueryUnavailable) as error:
        await query.search(query_text="private-query", tenant_id=TENANT)
    assert cancelled == {VectorTier.WARM, VectorTier.COLD}
    assert error.value.trace["stage"] == "tiers"
    assert error.value.trace["error_type"] == "RuntimeError"
    assert "secret" not in str(error.value) and "private-query" not in str(error.value.trace)
    assert all(t["status"] == "interrupted" for t in error.value.trace["tiers"].values())
    assert all("vector_search_ms" not in t for t in error.value.trace["tiers"].values())


@pytest.mark.asyncio
async def test_deadline_includes_queue_and_does_not_call_dependencies_when_queued():
    class MustNotRun:
        version = "model-v1"
        async def embed_queries(self, *args, **kwargs):
            pytest.fail("queued request must time out before calling model")
    query = LifecycleKnowledgeSearch(index=object(), ledger=Ledger(), embedder=MustNotRun(),
        config=config(query_request_timeout_seconds=0.01))
    await query._semaphore.acquire()
    try:
        with pytest.raises(LifecycleQueryTimeout) as error:
            await query.search(query_text="合成", tenant_id=TENANT)
        assert error.value.trace["stage"] == "queue"
        assert error.value.trace["queue_ms"] == error.value.trace["total_ms"]
    finally:
        query._semaphore.release()


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_converted_to_dependency_failure():
    started = asyncio.Event()
    class SlowEmbedding(Embedder):
        async def embed_queries(self, *args, **kwargs):
            started.set()
            await asyncio.Future()
    query = LifecycleKnowledgeSearch(index=object(), ledger=Ledger(), embedder=SlowEmbedding(), config=config())
    task = asyncio.create_task(query.search(query_text="合成", tenant_id=TENANT))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert query._semaphore._value == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout,expected", [(False, 503), (True, 504)])
async def test_http_dependency_errors_are_redacted_and_have_partial_trace(timeout, expected):
    class Backend:
        async def search(self, **kwargs):
            if timeout:
                await asyncio.Future()
            raise RuntimeError("credential=do-not-return")
    query = LifecycleKnowledgeSearch(index=Backend(), ledger=Ledger(), embedder=Embedder(),
        config=config(query_request_timeout_seconds=0.01))
    app = FastAPI()
    app.include_router(router)
    app.state.settings = SimpleNamespace(knowledge_ingest_token=SecretStr("local-fixture"))
    app.state.knowledge_store = object()
    app.state.knowledge_lifecycle = SimpleNamespace(query=query)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.post("/api/v1/knowledge/search", headers=AUTH, json={"query":"正文", "limit":2})
    assert response.status_code == expected
    detail = response.json()["detail"]
    assert detail["code"] == ("knowledge_search_timeout" if timeout else "knowledge_search_unavailable")
    assert detail["trace"]["total_ms"] >= 0 and detail["trace"]["stage"] == "tiers"
    assert "credential" not in response.text and "正文" not in response.text
