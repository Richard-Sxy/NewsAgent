"""Scaled fixture identity, bounded DIM lookup and persisted indexing gates."""

from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
import os
from types import SimpleNamespace

import pytest

from app.analytics.metric_source import HotNewsMetricQuery
from app.domain.errors import HotNewsDataQualityError
from app.knowledge.document import IngestOutcome, IngestReport, IngestStatus
from app.knowledge.native_indexing import split_text
from app.model_runtime.core import EmbeddingRequest
from app.model_runtime.local_embedding import LocalHashEmbedding
from app.retrieval.tiered_vector import VectorTier
from app.schemas.sql_assistant import SqlAssistantPreview, SqlAssistantResult
from app.sql_assistant.warehouse import DEMO_TENANT_ID, ISOLATION_TENANT_ID, WINDOW_START, warehouse_contract
from examples.native_hot_news_e2e_worker import ProfileScopedKnowledgeIndex
from examples.native_hot_news_sql_support import (
    SqlHotNewsContentRepository, SqlHotNewsDetailRepository,
    build_native_hot_news_sql_dependencies, seed_native_sql_knowledge,
)


TENANT = str(DEMO_TENANT_ID)
USER = "22222222-2222-4222-8222-222222222222"
PROFILE = "enterprise-v2"
END = WINDOW_START + timedelta(hours=1)


def metadata(identifier):
    return {"tenant_id": DEMO_TENANT_ID, "news_id": identifier,
            "title": f"[合成样本] 城市服务阶段进展 {identifier}", "content_type": "article",
            "category": "社会", "source": "本地合成新闻中心", "publish_time": WINDOW_START}


def bucket(identifier):
    return {**metadata(identifier), "event_time": WINDOW_START, "impressions": 100,
            "clicks": 10, "unique_users": 7, "total_duration_seconds": Decimal(100),
            "effective_consumptions": 5, "interactions": 1,
            "baseline_impressions": Decimal(100), "baseline_clicks": Decimal(10),
            "baseline_effective_consumptions": Decimal(5), "baseline_interactions": Decimal(1)}


class Rows:
    def __init__(self, rows=()):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class MemorySqlDatabase:
    """Unit-test SQL boundary double; real PG coverage below is opt-in."""
    def __init__(self):
        self.engine = self
        self.states = {}
        self.chunks = {}
        self.calls = []
        self.wrong_tenant = False

    @asynccontextmanager
    async def connect(self):
        yield self

    @asynccontextmanager
    async def begin(self):
        yield self

    async def execute(self, sql, parameters=None):
        statement = str(sql)
        self.calls.append((statement, parameters))
        if "FROM dw.dim_news" in statement:
            rows = [metadata(identifier) for identifier in parameters["news_ids"]]
            if self.wrong_tenant:
                rows[0]["tenant_id"] = ISOLATION_TENANT_ID
            return Rows(rows)
        if "FROM dw.news_behavior_aggregate" in statement:
            return Rows([bucket(identifier) for identifier in parameters["news_ids"]])
        if "FROM native_knowledge_documents" in statement:
            return Rows([{**state, "chunk_count": sum(chunk["document_id"] == identifier for chunk in self.chunks.values())}
                         for identifier, state in self.states.items() if identifier in parameters["document_ids"]])
        if "FROM native_knowledge_chunks" in statement:
            return Rows([chunk for chunk in self.chunks.values() if chunk["document_id"] in parameters["document_ids"]])
        return Rows()


class CountingEmbedding(LocalHashEmbedding):
    def __init__(self):
        super().__init__(model_routes=("synthetic-count-v1",), dimensions=8)
        self.calls = 0

    async def embed(self, request):
        self.calls += 1
        return await super().embed(request)


class MemoryIndexStore:
    """Persisted-state double exercising the real local Embedding Port."""
    embedding_version = "synthetic-count-v1"
    tier = VectorTier.HOT
    _chunk_chars = 800
    _overlap = 80

    def __init__(self, database, embedding):
        self.database = database
        self.embedding = embedding

    async def upsert_documents(self, *, tenant_id, documents):
        outcomes = []
        for document in documents:
            parts = split_text(document.text, max_chars=self._chunk_chars, overlap=self._overlap)
            result = await self.embedding.embed(EmbeddingRequest(
                tenant_id=tenant_id, trace_id="synthetic-contract", model_route=self.embedding_version,
                texts=tuple(f"{document.title}\n{part}" for part in parts),
            ))
            version = int(document.metadata["content_version"])
            self.database.states[document.document_id] = {
                "tenant_id": tenant_id, "document_id": document.document_id, "news_id": document.metadata["news_id"],
                "content_version": version, "content_sha256": sha256(f"{document.title}\x1f{document.text}".encode()).hexdigest(),
                "embedding_version": self.embedding_version, "title": document.title, "body": document.text,
                "metadata": dict(document.metadata),
            }
            for index, (part, vector) in enumerate(zip(parts, result.vectors, strict=True)):
                chunk_id = sha256(f"{tenant_id}\x1f{document.document_id}\x1f{version}\x1f{index}\x1f{self.embedding_version}".encode()).hexdigest()
                self.database.chunks[chunk_id] = {
                    "tenant_id": tenant_id, "document_id": document.document_id, "chunk_id": chunk_id,
                    "news_id": document.metadata["news_id"], "content_version": version, "chunk_index": index,
                    "embedding_version": self.embedding_version, "tier": self.tier.value, "title": document.title,
                    "excerpt": part[:1000], "source_url": document.source_url or None,
                    "publish_time": document.metadata["publish_time"], "vector_dimensions": len(vector),
                }
            outcomes.append(IngestOutcome(document.document_id, IngestStatus.CREATED))
        return IngestReport(tuple(outcomes))


def dependencies(database, ids=("scale-news-001100", "scale-news-001199"), *, news_per_tenant=1200):
    details = SqlHotNewsDetailRepository(database, tenant_id=TENANT, dataset_profile=PROFILE, news_per_tenant=news_per_tenant)
    return SimpleNamespace(tenant_id=TENANT, news_ids=ids, dataset_profile=PROFILE,
                           content_repository=SqlHotNewsContentRepository(details))


@pytest.mark.asyncio
async def test_scaled_dimension_lookup_is_bounded_and_rejects_unknown_or_cross_tenant():
    database = MemorySqlDatabase()
    deps = dependencies(database)
    ids = tuple(f"scale-news-{index:06d}" for index in range(950, 1151))
    contents = await deps.content_repository.batch_get_by_news_ids(tenant_id=TENANT, news_ids=ids)
    assert set(contents) == set(ids)
    metadata_calls = [params for sql, params in database.calls if "FROM dw.dim_news" in sql]
    assert [len(params["news_ids"]) for params in metadata_calls] == [200, 1]
    assert all(params["tenant_id"] == DEMO_TENANT_ID for params in metadata_calls)
    assert not any("FROM dw.news_behavior_aggregate" in sql for sql, _ in database.calls)
    for tenant, requested in ((str(ISOLATION_TENANT_ID), ids), (TENANT, ("scale-news-001201",)),
                              (TENANT, ("demo-news-001",))):
        previous = len(database.calls)
        with pytest.raises(HotNewsDataQualityError):
            await deps.content_repository.batch_get_by_news_ids(tenant_id=tenant, news_ids=requested)
        assert len(database.calls) == previous
    database.wrong_tenant = True
    with pytest.raises(HotNewsDataQualityError, match="identity/scope"):
        await deps.content_repository.batch_get_by_news_ids(tenant_id=TENANT, news_ids=ids[:1])


@pytest.mark.asyncio
async def test_scaled_approved_sql_is_executed_once_and_only_candidates_are_seeded():
    database = MemorySqlDatabase()
    ids = ("scale-news-001100", "scale-news-001199")
    contract = warehouse_contract(PROFILE)
    sql = "SELECT approved synthetic candidates"
    query_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    preview = SqlAssistantPreview(query_id=query_id, question="查询点击量最高的前100条新闻", scenario_id="news-ranking",
        sql=sql, sql_hash=sha256(sql.encode()).hexdigest(), schema_version=contract.version,
        schema_sha256=contract.sha256, explanation="synthetic unit fixture", model_request_id="local",
        model_provider="local", expires_at=END, stages=[], parameters={"tenant_id": TENANT,
            "window_start": WINDOW_START.isoformat(), "window_end": END.isoformat(), "row_limit": 100})
    snapshot = {"tenant_id": TENANT, "user_id": USER, "scenario": {"result_mode": "ranking"},
                "preview": preview.model_dump(mode="json")}

    async def get(*_args):
        return snapshot

    candidate_rows = [{key: bucket(identifier)[key] for key in (
        "news_id", "title", "content_type", "category", "source", "impressions", "clicks",
        "effective_consumptions", "interactions")} | {"ctr": "0.1", "hot_score": "0.5"} for identifier in ids]
    calls = []

    async def execute(query):
        calls.append(query)
        return SqlAssistantResult(query_id=query, sql_hash=preview.sql_hash, columns=list(candidate_rows[0]),
                                  rows=candidate_rows, row_count=2, elapsed_ms=1, truncated=False, summary="synthetic", stages=[])

    service = SimpleNamespace(store=SimpleNamespace(get=get), dataset_profile=PROFILE, news_per_tenant=1200)
    deps = await build_native_hot_news_sql_dependencies(database=database, sql_service=service,
        query_id=query_id, tenant_id=TENANT, user_id=USER, execute_for_run=execute)
    assert deps.news_ids == ids
    assert len(calls) == 1
    assert len(await deps.metric_source.fetch_snapshots(HotNewsMetricQuery(
        tenant_id=TENANT, window_start=WINDOW_START, window_end=END, ranking_limit=100))) == 2
    assert len(calls) == 1
    trace = await deps.metric_source.get_tool_trace()
    assert trace["preview"]["schema_version"] == contract.version
    assert trace["preview"]["schema_sha256"] == contract.sha256


@pytest.mark.asyncio
async def test_persisted_preflight_skips_embedding_on_repeat_and_worker_restart():
    database = MemorySqlDatabase()
    deps = dependencies(database)
    embedding = CountingEmbedding()
    store = MemoryIndexStore(database, embedding)
    await seed_native_sql_knowledge(deps, store=store)
    assert embedding.calls == 2
    await seed_native_sql_knowledge(deps, store=store)
    assert embedding.calls == 2
    restarted_embedding = CountingEmbedding()
    await seed_native_sql_knowledge(deps, store=MemoryIndexStore(database, restarted_embedding))
    assert restarted_embedding.calls == 0
    assert {state["news_id"] for state in database.states.values()} == set(deps.news_ids)
    assert all(state["metadata"]["warehouse_schema_version"] == "news-warehouse-v2" for state in database.states.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["same-version-content", "missing-chunk", "changed-excerpt", "foreign-chunk", "wrong-version"])
async def test_existing_index_corruption_is_rejected_without_embedding_or_repair(drift):
    database = MemorySqlDatabase()
    deps = dependencies(database)
    embedding = CountingEmbedding()
    store = MemoryIndexStore(database, embedding)
    await seed_native_sql_knowledge(deps, store=store)
    if drift == "same-version-content":
        next(iter(database.states.values()))["body"] = "unexpected persisted change"
    elif drift == "missing-chunk":
        database.chunks.pop(next(iter(database.chunks)))
    else:
        chunk = next(iter(database.chunks.values()))
        if drift == "changed-excerpt":
            chunk["excerpt"] = "unexpected persisted excerpt"
        elif drift == "foreign-chunk":
            chunk["tenant_id"] = str(ISOLATION_TENANT_ID)
        else:
            chunk["content_version"] = 2
    with pytest.raises(HotNewsDataQualityError):
        await seed_native_sql_knowledge(deps, store=store)
    assert embedding.calls == 2


@pytest.mark.asyncio
async def test_scaled_knowledge_scope_excludes_legacy_and_foreign_ids():
    calls = []

    async def search(**kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(news_id="scale-news-001100"), SimpleNamespace(news_id="legacy"),
                SimpleNamespace(news_id="unregistered")]

    index = ProfileScopedKnowledgeIndex(SimpleNamespace(search=search),
                                        allowed_news_ids=("scale-news-001100",), legacy_news_ids=("legacy",))
    hits = await index.search(tenant_id=TENANT, exclude_news_ids=frozenset({"source"}), limit=20)
    assert [hit.news_id for hit in hits] == ["scale-news-001100"]
    assert calls[0]["exclude_news_ids"] == frozenset({"source", "legacy"})
    with pytest.raises(HotNewsDataQualityError):
        await index.search(tenant_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", exclude_news_ids=frozenset(), limit=20)
    assert len(calls) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_scaled_postgres_content_and_restart_embedding_skip():
    url = os.getenv("SCALE_WAREHOUSE_INTEGRATION_URL")
    if not url:
        pytest.skip("set SCALE_WAREHOUSE_INTEGRATION_URL to an isolated v2 acceptance database")
    news_per_tenant = int(os.getenv("SCALE_WAREHOUSE_NEWS_PER_TENANT", "1200"))
    if news_per_tenant < 1200:
        pytest.skip("this identity-above-1000 acceptance requires at least 1200 news per tenant")
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.knowledge.postgres_store import PostgresKnowledgeStore, _metadata
    from app.sql_assistant.warehouse import initialize_demo_warehouse

    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session():
        async with sessions() as current:
            try:
                yield current
                await current.commit()
            except Exception:
                await current.rollback()
                raise

    database = SimpleNamespace(engine=engine, session=session)
    try:
        await initialize_demo_warehouse(database, environment="e2e", dataset_profile=PROFILE, news_per_tenant=news_per_tenant)
        async with engine.begin() as connection:
            await connection.run_sync(_metadata.create_all)
        deps = dependencies(database, news_per_tenant=news_per_tenant)
        embedding = CountingEmbedding()
        store = PostgresKnowledgeStore(database=database, embedding=embedding, embedding_version="synthetic-count-v1")
        await seed_native_sql_knowledge(deps, store=store)
        assert embedding.calls in {0, 2}  # An existing identical fixture is legitimately reused.
        first_count = embedding.calls
        await seed_native_sql_knowledge(deps, store=store)
        assert embedding.calls == first_count
        restarted = CountingEmbedding()
        await seed_native_sql_knowledge(deps, store=PostgresKnowledgeStore(
            database=database, embedding=restarted, embedding_version="synthetic-count-v1"))
        assert restarted.calls == 0
        contents = await deps.content_repository.batch_get_by_news_ids(tenant_id=TENANT, news_ids=deps.news_ids)
        assert set(contents) == {"scale-news-001100", "scale-news-001199"}
    finally:
        await engine.dispose()
