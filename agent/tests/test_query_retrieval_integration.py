"""Synthetic contract comparisons; these are not relevance quality labels."""
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
from pydantic import SecretStr
import pytest

from app.api.knowledge import router
from app.clients.knowledge_base import RelatedNewsSearchQuery
from app.modules.knowledge.tier_lifecycle_config import load_knowledge_lifecycle_config
from app.modules.knowledge.tier_lifecycle_index import GuardedTieredIndex
from app.modules.knowledge.tier_lifecycle_query import LifecycleKnowledgeSearch, LifecycleQueryUnavailable
from app.retrieval.query_policy import QueryRetrievalPolicy
from app.retrieval.tiered_vector import VectorSearchHit, VectorTier

TENANT = "11111111-1111-4111-8111-111111111111"


def hit(news, score, tier, chunk=None):
    return VectorSearchHit(chunk or news, news, "synthetic title", "synthetic evidence", None,
        datetime(2026, 10, 1, tzinfo=UTC), score, tier, 1, "model-v1")


class Backend:
    similarity_metric = "IP"
    tiers = tuple(VectorTier)
    def __init__(self, rows=()):
        self.rows, self.calls = list(rows), []
    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return [h for h in self.rows if h.tier == kwargs["tier"]][:kwargs["limit"]]


class Ledger:
    def __init__(self):
        self.withdrawn = set()
    async def filter_hits(self, *, tenant_id, hits):
        return [h for h in hits if h.news_id not in self.withdrawn]


class Embedder:
    version = "model-v1"
    def __init__(self):
        self.calls = []
    async def embed_queries(self, texts, *, tenant_id):
        self.calls.append((texts, tenant_id))
        return [(1., 0.) for _ in texts]


def make_query(rows=(), *, policy=True, ledger=None):
    policy = QueryRetrievalPolicy(version="candidate-v1", min_per_tier=2) if policy is True else policy
    cfg = SimpleNamespace(query_max_concurrency=2, query_candidate_limit=2, query_max_candidates=16,
        query_request_timeout_seconds=1, embedding_version="model-v1", expected_embedding_dimensions=2,
        policy_version="lifecycle-v1", query_weights={VectorTier.HOT: 1., VectorTier.WARM: 1., VectorTier.COLD: .7},
        query_rrf_k=60, query_retrieval=policy)
    backend, ledger, embedder = Backend(rows), ledger or Ledger(), Embedder()
    query = LifecycleKnowledgeSearch(index=GuardedTieredIndex(backend, ledger, max_candidates=16),
        ledger=ledger, embedder=embedder, config=cfg)
    return query, backend, embedder


@pytest.mark.asyncio
async def test_global_similarity_removes_tier_bias_with_legacy_control():
    rows = [hit("hot", .1, VectorTier.HOT), hit("cold", .99, VectorTier.COLD)]
    old, _, _ = make_query(rows, policy=None)
    new, backend, _ = make_query(rows)
    baseline = await old.search(query_text="news", tenant_id=TENANT, limit=2)
    candidate = await new.search(query_text="news", tenant_id=TENANT, limit=2)
    assert [h.news_id for h in baseline["hits"]] == ["hot", "cold"]
    assert [h.news_id for h in candidate["hits"]] == ["cold", "hot"]
    assert candidate["scores"] == {"cold": 1., "hot": 61 / 62}
    assert {call["tier"] for call in backend.calls} == set(VectorTier)
    assert candidate["trace"]["ranking"]["score_kind"] == "global_rank_feature"


@pytest.mark.asyncio
async def test_news_refill_prevents_duplicate_chunks_starving_other_articles():
    rows = [hit("long", .99, VectorTier.HOT, f"long-{i}") for i in range(5)]
    rows.append(hit("other", .8, VectorTier.HOT))
    query, backend, _ = make_query(rows)
    result = await query.search(query_text="news", tenant_id=TENANT, limit=2)
    assert [h.news_id for h in result["hits"]] == ["long", "other"]
    assert [c["limit"] for c in backend.calls if c["tier"] == VectorTier.HOT] == [4, 8]
    assert result["trace"]["tiers"]["hot"]["duplicate_chunks"] == 4


@pytest.mark.asyncio
async def test_intent_budgets_respect_explicit_override():
    query, backend, _ = make_query()
    result = await query.search(query_text="最新人工智能", tenant_id=TENANT, limit=2)
    assert result["trace"]["retrieval_plan"]["tier_limits"] == {"hot": 8, "warm": 4, "cold": 4}
    result = await query.search(query_text="最新人工智能", tenant_id=TENANT, limit=2, retrieval_intent="background")
    assert result["trace"]["retrieval_plan"]["tier_limits"] == {"hot": 4, "warm": 4, "cold": 8}
    assert len(backend.calls) == 6


@pytest.mark.asyncio
async def test_final_gate_blocks_withdrawal_after_layer_reads():
    class ChangingLedger(Ledger):
        async def filter_hits(self, *, tenant_id, hits):
            if len({h.tier for h in hits}) > 1:
                self.withdrawn.add("cold")
            return await super().filter_hits(tenant_id=tenant_id, hits=hits)
    query, _, _ = make_query([hit("hot", .1, VectorTier.HOT), hit("cold", .99, VectorTier.COLD)], ledger=ChangingLedger())
    result = await query.search(query_text="news", tenant_id=TENANT)
    assert [h.news_id for h in result["hits"]] == ["hot"]


@pytest.mark.asyncio
async def test_optional_semantics_fallback_rechecks_withdrawals():
    ledger = Ledger()
    query, _, _ = make_query([hit("withdrawn", .99, VectorTier.HOT)], ledger=ledger)
    class Semantics:
        config = SimpleNamespace(query_timeout_seconds=1)
        async def enrich(self, **kwargs):
            ledger.withdrawn.add("withdrawn")
            raise RuntimeError("do-not-expose")
    query.semantics = Semantics()
    result = await query.search(query_text="private-news", tenant_id=TENANT)
    assert result["hits"] == [] and result["trace"]["semantics"]["status"] == "deferred"
    assert "private-news" not in str(result["trace"]) and "do-not-expose" not in str(result["trace"])


@pytest.mark.asyncio
async def test_batch_score_is_not_normalized_twice():
    query, _, _ = make_query([hit("cold", .99, VectorTier.COLD)])
    result = await query.batch_search_related_news((RelatedNewsSearchQuery(
        query_id="q", source_news_id="source", title="news", summary="", candidate_limit=2),), tenant_id=TENANT)
    assert result["q"][0].score == 1.


@pytest.mark.asyncio
async def test_invalid_hints_are_rejected_before_remote_work():
    for policy, hints, message in [(True, {"retrieval_topic": "unconfigured"}, "unknown"),
                                    (None, {"retrieval_intent": "recent"}, "enabled")]:
        query, backend, embedder = make_query(policy=policy)
        with pytest.raises(ValueError, match=message):
            await query.search(query_text="news", tenant_id=TENANT, **hints)
        assert backend.calls == embedder.calls == []


@pytest.mark.parametrize("metric", [None, "L2", "mixed"])
def test_global_fusion_requires_comparable_metric(metric):
    query, _, _ = make_query()
    backend = Backend()
    backend.similarity_metric = metric
    with pytest.raises(ValueError, match="score contract"):
        LifecycleKnowledgeSearch(index=GuardedTieredIndex(backend, Ledger()), ledger=Ledger(), embedder=Embedder(), config=query.config)
    backend.similarity_metric = "IP"
    with pytest.raises(ValueError, match="news-level"):
        LifecycleKnowledgeSearch(index=backend, ledger=Ledger(), embedder=Embedder(), config=query.config)


@pytest.mark.asyncio
async def test_nonfinite_similarity_fails_closed():
    query, _, _ = make_query([hit("bad", float("nan"), VectorTier.HOT)])
    with pytest.raises(LifecycleQueryUnavailable) as error:
        await query.search(query_text="private", tenant_id=TENANT)
    assert error.value.trace["status"] == "failed" and "private" not in str(error.value.trace)


@pytest.mark.asyncio
async def test_http_hints_and_redacted_plan():
    policy = QueryRetrievalPolicy(version="topic-v1", min_per_tier=2,
        topic_rules={"ai": ("人工智能",)}, topic_intents={"ai": "general"})
    query, _, _ = make_query(policy=policy)
    app = FastAPI()
    app.include_router(router)
    app.state.settings = SimpleNamespace(knowledge_ingest_token=SecretStr("local-fixture"))
    app.state.knowledge_store = object()
    app.state.knowledge_lifecycle = SimpleNamespace(query=query)
    headers = {"X-Tenant-ID": TENANT, "Authorization": "Bearer local-fixture"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.post("/api/v1/knowledge/search", headers=headers,
            json={"query": "人工智能最新动态", "limit": 2, "retrieval_intent": "background", "retrieval_topic": "ai"})
        assert response.status_code == 200
        plan = response.json()["trace"]["retrieval_plan"]
        assert plan["intent"] == "background" and plan["topic"] == "ai"
        assert "人工智能" not in str(response.json()["trace"])
        response = await client.post("/api/v1/knowledge/search", headers=headers,
            json={"query": "news", "retrieval_topic": "unknown"})
        assert response.status_code == 422


def test_independent_candidate_yaml_loads_without_changing_legacy():
    deploy = Path(__file__).resolve().parents[2] / "deploy"
    legacy = load_knowledge_lifecycle_config(deploy / "knowledge-lifecycle.local.example.yml")
    candidate = load_knowledge_lifecycle_config(deploy / "knowledge-lifecycle.query-aware.example.yml")
    assert legacy.query_retrieval is None
    assert candidate.query_retrieval.version == "query-aware-global-similarity-candidate-v1"
    assert candidate.policy_version != legacy.policy_version
