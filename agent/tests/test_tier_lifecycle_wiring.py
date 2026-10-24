"""Configuration and authenticated entrypoints must reach the guarded runtime."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
import httpx
from pydantic import SecretStr
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import yaml

from app.api.knowledge import router
from app.clients.enterprise.milvus_vector import MilvusTieredVectorIndex
from app.modules.conversation.tools import ConversationTools
from app.modules.knowledge.document import KnowledgeDocument
from app.modules.knowledge.tier_lifecycle_config import load_knowledge_lifecycle_config
from app.modules.knowledge.tier_lifecycle_factory import build_lifecycle_runtime
from app.modules.knowledge.tier_lifecycle_store import _metadata
from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.knowledge_factory import build_native_knowledge_index, build_native_knowledge_search
from app.model_runtime.local_embedding import LocalHashEmbedding
from app.retrieval.tiered_vector import VectorTier


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
POLICY = DEPLOY / "knowledge-lifecycle.local.example.yml"
TENANT = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
AUTH = {"Authorization": "Bearer fixture-ingest-token", "X-Tenant-ID": TENANT}


class Database:
    def __init__(self, path):
        self.engine = create_engine(f"sqlite:///{path}")
        _metadata.create_all(self.engine)

    @asynccontextmanager
    async def session(self):
        with Session(self.engine) as current:
            async def execute(*args, **kwargs):
                return current.execute(*args, **kwargs)
            try:
                yield SimpleNamespace(execute=execute)
                current.commit()
            except BaseException:
                current.rollback()
                raise


@pytest.fixture
def setup(tmp_path):
    model = load_model_runtime_config(DEPLOY / "model-runtime.local.yml")
    config = load_knowledge_lifecycle_config(POLICY)
    database = Database(tmp_path / "wiring.sqlite")
    embedding = LocalHashEmbedding(
        model_routes=(config.embedding_version,), dimensions=config.expected_embedding_dimensions,
    )

    def build(**kwargs):
        return build_lifecycle_runtime(**{
            "lifecycle_config_path": str(POLICY), "search_config_path": None,
            "model_config": model, "embedding": embedding,
            "database": database, "environment": "e2e", **kwargs,
        })

    yield SimpleNamespace(
        model=model, config=config, database=database, embedding=embedding,
        build=build, runtime=build(),
    )
    database.engine.dispose()


def search_config(tmp_path, *, backend="milvus", collections=None, ef=4096):
    value = {"schema_version": 1, "backend": backend}
    if backend == "milvus":
        value["milvus"] = {
            "uri": "http://127.0.0.1:19530", "database": "lifecycle_fixture",
            "collections": collections or {tier.value: f"fixture_{tier.value}" for tier in VectorTier},
            "search_params": {"ef": ef},
        }
    elif backend == "bge_snapshot":
        value["bge_snapshot"] = {
            "package_dir": "unused-read-only-snapshot", "tenant_id": TENANT,
            "snapshot_version": "fixture-v1",
        }
    path = tmp_path / "search.yml"
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("environment", ["production", "development", "staging"])
def test_persistent_local_substitute_is_rejected_outside_e2e(setup, environment):
    with pytest.raises(ValueError, match="e2e"):
        setup.build(environment=environment)


def test_immutable_bge_target_rejected_without_opening_snapshot(setup, tmp_path):
    with pytest.raises(ValueError, match="immutable BGE"):
        setup.build(search_config_path=search_config(tmp_path, backend="bge_snapshot"))


@pytest.mark.parametrize("changes,match", [
    ({"model_routes": ("other-model",)}, "route"),
    ({"expected_embedding_dimensions": 1024}, "dimensions"),
    ({"max_batch_size": 1}, "batch"),
])
def test_model_contract_mismatch_fails_before_vector_connection(setup, changes, match, monkeypatch):
    connect = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(MilvusTieredVectorIndex, "connect", connect)
    endpoint = setup.model.embedding.model_copy(update=changes)
    model = setup.model.model_copy(update={"embedding": endpoint})
    with pytest.raises(ValueError, match=match):
        setup.build(model_config=model)
    connect.assert_not_called()


def test_milvus_requires_three_collections_and_ef_covering_refills(setup, tmp_path, monkeypatch):
    connect = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(MilvusTieredVectorIndex, "connect", connect)
    with pytest.raises(ValueError, match="three"):
        setup.build(search_config_path=search_config(tmp_path, collections={"hot": "fixture_hot"}))
    with pytest.raises(ValueError, match="ef"):
        setup.build(search_config_path=search_config(tmp_path, ef=setup.config.query_max_candidates - 1))
    connect.assert_not_called()


@pytest.mark.parametrize("ef", [None, True, "96", 4096.0])
def test_explicit_invalid_ef_is_rejected_before_connect(setup, tmp_path, monkeypatch, ef):
    connect = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(MilvusTieredVectorIndex, "connect", connect)
    with pytest.raises(ValueError, match="ef"):
        setup.build(search_config_path=search_config(tmp_path, ef=ef))
    connect.assert_not_called()


def test_milvus_lifecycle_forces_cross_process_strong_consistency(setup, tmp_path, monkeypatch):
    backend = SimpleNamespace(tiers=tuple(VectorTier))
    connect = Mock(return_value=backend)
    monkeypatch.setattr(MilvusTieredVectorIndex, "connect", connect)
    runtime = setup.build(search_config_path=search_config(tmp_path, ef=setup.config.query_max_candidates))
    assert connect.call_args.kwargs["consistency_level"] == "Strong"
    assert connect.call_args.kwargs["rpc_max_concurrency"] == setup.config.query_max_concurrency * 3
    assert set(connect.call_args.kwargs["collections"]) == set(VectorTier)
    assert runtime.index.lifecycle_query is runtime.query
    assert runtime.index.lifecycle_service is runtime.service


@pytest.mark.asyncio
async def test_runtime_preflight_passes_largest_refill_budget_through_guarded_index(setup, tmp_path, monkeypatch):
    backend = SimpleNamespace(tiers=tuple(VectorTier), validate_collections=AsyncMock(return_value={}))
    monkeypatch.setattr(MilvusTieredVectorIndex, "connect", Mock(return_value=backend))
    runtime = setup.build(search_config_path=search_config(tmp_path))
    await runtime.validate()
    backend.validate_collections.assert_awaited_once_with(
        expected_dimensions=setup.config.expected_embedding_dimensions,
        query_max_candidates=setup.config.query_max_candidates)


def test_hotspot_native_factory_returns_the_same_lifecycle_query_service(setup):
    index = build_native_knowledge_index(
        config=setup.model, embedding=setup.embedding, database=setup.database,
        lifecycle_config_path=str(POLICY), environment="e2e",
    )
    query = build_native_knowledge_search(config=setup.model, embedding=setup.embedding,
                                         database=setup.database, index=index)
    assert query is index.lifecycle_query
    assert query.index is index


def app_for(runtime):
    app = FastAPI()
    app.include_router(router)
    app.state.settings = SimpleNamespace(knowledge_ingest_token=SecretStr("fixture-ingest-token"))
    app.state.knowledge_store = runtime.service
    app.state.knowledge_lifecycle = runtime
    return app


def payload(news_id="article"):
    return {
        "document_id": f"document-{news_id}", "news_id": news_id, "content_version": 1,
        "title": "城市交通新闻", "text": "城市交通更新公交线路，市民可查询出行信息。",
        "publish_time": (datetime.now(UTC) - timedelta(seconds=5)).isoformat(),
    }


@pytest.mark.asyncio
async def test_protected_readiness_checks_tables_and_rejects_missing_vector_table(setup):
    from sqlalchemy import text
    transport = httpx.ASGITransport(app=app_for(setup.runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
        path = "/api/v1/knowledge/lifecycle/readiness"
        assert (await client.get(path, headers={"X-Tenant-ID": TENANT})).status_code == 401
        response = await client.get(path, headers=AUTH)
        assert response.status_code == 200
        assert response.json()["scope"] == "database_and_vector_contract_only"
        assert set(response.json()["tiers"]) == {"hot", "warm", "cold"}
        with setup.database.engine.begin() as connection:
            connection.execute(text("DROP TABLE tier_lifecycle_vectors"))
        response = await client.get(path, headers=AUTH)
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "knowledge_lifecycle_not_ready"


@pytest.mark.asyncio
async def test_api_health_exclusion_returns_reason_without_creating_job(setup):
    transport = httpx.ASGITransport(app=app_for(setup.runtime))
    item = payload("health") | {"metadata": {"category": "健康"}}
    async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
        response = await client.post("/api/v1/knowledge/documents", headers=AUTH,
                                     json={"documents": [item]})
        assert response.status_code == 200
        outcome = response.json()["outcomes"][0]
        assert outcome["status"] == "skipped"
        assert outcome["collection_id"] is None
        assert outcome["detail"].startswith("news_board_excluded:")
        response = await client.get("/api/v1/knowledge/documents/document-health/status", headers=AUTH)
        assert response.status_code == 404
    assert await setup.runtime.service.ledger.list_pending(limit=10) == []


@pytest.mark.asyncio
async def test_api_acceptance_status_and_withdrawal_follow_durable_state(setup):
    runtime = setup.runtime
    transport = httpx.ASGITransport(app=app_for(runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
        response = await client.post("/api/v1/knowledge/documents", headers=AUTH,
                                     json={"documents": [payload()]})
        assert response.status_code == 200
        outcome = response.json()["outcomes"][0]
        assert outcome["status"] == "created"
        job_id = outcome["collection_id"]
        response = await client.get("/api/v1/knowledge/documents/document-article/status", headers=AUTH)
        assert response.status_code == 200 and response.json()["status"] == "pending"
        response = await client.get(f"/api/v1/knowledge/index-jobs/{job_id}", headers=AUTH)
        assert response.json()["status"] == "pending"
        assert not {"records", "payload", "config", "body"}.intersection(response.json())
        await runtime.service.process_job(tenant_id=TENANT, job_id=job_id)
        response = await client.get("/api/v1/knowledge/documents/document-article/status", headers=AUTH)
        assert response.json()["status"] == "ready"
        response = await client.post("/api/v1/knowledge/search", headers=AUTH,
                                     json={"query": "城市交通", "limit": 3})
        assert response.status_code == 200 and len(response.json()["items"]) == 1
        assert response.json()["evidence_is_untrusted"] is True
        response = await client.post("/api/v1/knowledge/news/article/retract", headers=AUTH,
                                     json={"source_revision": 2})
        assert response.status_code == 202
        response = await client.get("/api/v1/knowledge/documents/document-article/status", headers=AUTH)
        assert response.json()["status"] == "retracted"
        response = await client.post("/api/v1/knowledge/search", headers=AUTH,
                                     json={"query": "城市交通", "limit": 3})
        assert response.json()["items"] == []  # Cleanup worker has not run yet.
        response = await client.post("/api/v1/knowledge/news/article/retract", headers=AUTH,
                                     json={"source_revision": 1})
        assert response.status_code == 409


@pytest.mark.asyncio
async def test_api_credentials_and_tenant_scope_cover_mutations_and_job_status(setup):
    transport = httpx.ASGITransport(app=app_for(setup.runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
        response = await client.post("/api/v1/knowledge/documents", headers=AUTH,
                                     json={"documents": [payload()]})
        job_id = response.json()["outcomes"][0]["collection_id"]
        other = {**AUTH, "X-Tenant-ID": OTHER}
        assert (await client.get(f"/api/v1/knowledge/index-jobs/{job_id}", headers=other)).status_code == 404
        assert (await client.get("/api/v1/knowledge/documents/document-article/status", headers=other)).status_code == 404
        for method, path, body in [
            ("GET", f"/api/v1/knowledge/index-jobs/{job_id}", None),
            ("POST", "/api/v1/knowledge/news/article/retract", {"source_revision": 2}),
            ("POST", "/api/v1/knowledge/news/article/reheat", None),
            ("POST", "/api/v1/knowledge/search", {"query": "新闻"}),
        ]:
            response = await client.request(method, path, json=body, headers={"X-Tenant-ID": TENANT})
            assert response.status_code == 401
        response = await client.post("/api/v1/knowledge/news/article/reheat", headers=other)
        assert response.status_code == 409


@pytest.mark.asyncio
async def test_conversation_uses_guarded_common_query_and_filters_retracted_articles(setup):
    runtime = setup.runtime
    now = datetime.now(UTC)
    for news_id in ("source", "related"):
        data = payload(news_id)
        doc = KnowledgeDocument(
            document_id=data["document_id"], title=data["title"], text=data["text"],
            media_type="article", text_source="fixture",
            metadata={"news_id": news_id, "content_version": "1", "publish_time": data["publish_time"]},
        )
        job = await runtime.service.accept(tenant_id=TENANT, document=doc, now=now)
        await runtime.service.process_job(tenant_id=TENANT, job_id=job["job_id"])
    tools = ConversationTools(
        hot_news=None, knowledge_store=runtime.index, embedding=setup.embedding,
        embedding_version=setup.config.embedding_version, knowledge_enabled=True,
    )
    result = await tools.execute(name="search_knowledge",
        arguments={"query": "城市交通", "limit": 3, "source_news_id": "source"},
        tenant_id=TENANT, trace_id="fixture-turn")
    assert [item["news_id"] for item in result["items"]] == ["related"]
    assert result["retrieval_backend"] == "tier_lifecycle"
    assert result["retrieval_trace"]["policy_version"] == setup.config.policy_version
    await runtime.service.retract(tenant_id=TENANT, news_id="related", source_revision=2)
    result = await tools.execute(name="search_knowledge",
        arguments={"query": "城市交通", "limit": 3, "source_news_id": "source"},
        tenant_id=TENANT, trace_id="fixture-after-withdrawal")
    assert result["items"] == []
