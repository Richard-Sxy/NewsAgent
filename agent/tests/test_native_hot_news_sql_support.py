from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import os
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.analytics.entities import ContentType
from app.analytics.metric_source import HotNewsMetricQuery
from app.analytics.ranking import HotNewsRanker
from app.clients.enterprise.sql_warehouse import SqlQuery
from app.domain.errors import HotNewsDataQualityError
from app.knowledge.document import InMemoryKnowledgeBaseWriter
from app.schemas.sql_assistant import SqlAssistantIntent, SqlAssistantPreview, SqlAssistantResult
from app.sql_assistant.planner import compile_query
from app.sql_assistant.scenarios import load_sql_scenarios
from app.sql_assistant.warehouse import (
    DEMO_TENANT_ID, SCHEMA_SHA256, SCHEMA_VERSION, WINDOW_START,
    LocalPostgresSqlWarehouseClient, _fixture_rows, initialize_demo_warehouse,
)
from examples.native_hot_news_sql_support import (
    DEMO_NEWS_IDS, SqlAssistantHotNewsMetricSource, SqlHotNewsContentRepository,
    build_native_hot_news_sql_dependencies, seed_native_sql_knowledge,
)

TENANT = str(DEMO_TENANT_ID)
USER = "22222222-2222-4222-8222-222222222222"
END = WINDOW_START + timedelta(hours=1)
BUNDLE = "sql-hot-news-v1"


def preview(*, end=END, query_id="44444444-4444-4444-8444-444444444444"):
    scenario = load_sql_scenarios("deploy/text2sql-scenes.local.yml").resolve("news-ranking")
    intent = SqlAssistantIntent(sort_by="clicks", row_limit=5, explanation="test")
    sql = compile_query(intent, scenario)
    value = SqlAssistantPreview(
        query_id=query_id, question="查询点击量最高的前5条新闻", scenario_id="news-ranking",
        sql=sql, parameters={"tenant_id": TENANT, "window_start": WINDOW_START.isoformat(),
                             "window_end": end.isoformat(), "row_limit": 5},
        sql_hash=sha256(sql.encode()).hexdigest(), schema_version=SCHEMA_VERSION,
        schema_sha256=SCHEMA_SHA256, explanation="test", model_request_id="model-local-1",
        model_provider="local", expires_at=datetime.now(timezone.utc) + timedelta(minutes=15), stages=[],
    )
    return value, scenario


def fixture_detail_rows():
    news, metrics, baselines = _fixture_rows()
    dims = {row["news_id"]: row for row in news if row["tenant_id"] == DEMO_TENANT_ID}
    refs = {row["news_id"]: row for row in baselines if row["tenant_id"] == DEMO_TENANT_ID and row["event_time"] == WINDOW_START}
    return tuple({**dims[row["news_id"]], **row, **refs[row["news_id"]]} for row in metrics
                 if row["tenant_id"] == DEMO_TENANT_ID and row["event_time"] == WINDOW_START)


def candidate_result(value, details):
    rows = [
        {key: row[key] for key in ("news_id", "title", "content_type", "category", "source",
                                   "impressions", "clicks", "effective_consumptions", "interactions")}
        | {"ctr": "0.9999", "hot_score": "0.9999"}
        for row in details[:5]
    ]
    return SqlAssistantResult(
        query_id=value.query_id, columns=list(rows[0]), rows=rows, row_count=len(rows),
        elapsed_ms=1, truncated=False, summary="test", stages=[], sql_hash=value.sql_hash,
    )


class FakeStore:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def get(self, query_id, tenant_id, user_id):
        return self.snapshot if (query_id, tenant_id, user_id) == (
            self.snapshot["preview"]["query_id"], self.snapshot["tenant_id"], self.snapshot["user_id"]
        ) else None


class FakeService:
    def __init__(self, value, scenario):
        self.store = FakeStore({"preview": value.model_dump(mode="json"), "scenario": scenario.model_dump(mode="json"),
                                "tenant_id": TENANT, "user_id": USER})
        self.result = candidate_result(value, fixture_detail_rows())
        self.calls = []

    async def execute(self, query_id, *, tenant_id, user_id):
        self.calls.append((query_id, tenant_id, user_id))
        return self.result


class FakeDetails:
    tenant_id = TENANT

    def __init__(self):
        self.rows = fixture_detail_rows()
        self.calls = []

    async def fetch_details(self, *, window_start, window_end, news_ids):
        self.calls.append((window_start, window_end, news_ids))
        return tuple(row for row in self.rows if row["news_id"] in news_ids)


def source(*, callback=None):
    value, scenario = preview()
    service = FakeService(value, scenario)
    details = FakeDetails()
    return SqlAssistantHotNewsMetricSource(
        sql_service=service, preview=value, tenant_id=TENANT, user_id=USER,
        production_bundle_version=BUNDLE, details=details, execute_for_run=callback,
    ), service, details


def query(**updates):
    return HotNewsMetricQuery(**{"tenant_id": TENANT, "window_start": WINDOW_START, "window_end": END, **updates})


@pytest.mark.asyncio
async def test_sql_tool_maps_real_hourly_uv_recomputes_ctr_and_baselines():
    metric_source, service, details = source()
    snapshots = await metric_source.fetch_snapshots(query())
    assert len(snapshots) == 5
    for item in snapshots:
        row = next(row for row in details.rows if row["news_id"] == item.news_id)
        assert item.unique_users == row["unique_users"]
        assert item.total_duration_seconds == int(row["total_duration_seconds"])
        assert item.ctr == Decimal(row["clicks"]) / Decimal(row["impressions"])
        assert item.ctr != Decimal("0.9999")  # Model/SQL display rate is never authoritative.
    keys = frozenset((item.news_id, item.content_type) for item in snapshots)
    baselines = await metric_source.get_baselines(
        tenant_id=TENANT, window_start=WINDOW_START, window_end=END,
        production_bundle_version=BUNDLE, metric_keys=keys,
    )
    assert all(item.unique_users is None and item.total_duration_seconds is None for item in baselines.values())
    ranked = HotNewsRanker().rank(snapshots, baselines, limit=5)
    assert len(ranked) == 5
    assert all(item.hot_score.score != Decimal("0.9999") for item in ranked)
    assert await metric_source.fetch_snapshots(query()) == snapshots
    assert len(service.calls) == len(details.calls) == 1
    trace = await metric_source.get_tool_trace()
    assert trace["query_id"] == service.result.query_id
    assert trace["result"]["row_count"] == 5
    assert "unique_users" in trace["supplemental_sql"]
    assert any("禁止跨小时求和" in note for note in trace["metric_mapping_notes"])


@pytest.mark.asyncio
async def test_claimed_run_callback_is_used_instead_of_preview_execution():
    value, scenario = preview()
    calls = []

    async def execute_for_run(query_id):
        calls.append(query_id)
        return candidate_result(value, fixture_detail_rows())

    metric_source, service, _ = source(callback=execute_for_run)
    assert len(await metric_source.fetch_snapshots(query())) == 5
    assert calls == [value.query_id]
    assert not service.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"tenant_id": "33333333-3333-4333-8333-333333333333"},
    {"window_end": END + timedelta(hours=1)},
    {"requested_metrics": frozenset({"revenue"})},
])
async def test_sql_tool_rejects_scope_or_metric_expansion(updates):
    metric_source, service, _ = source()
    with pytest.raises(HotNewsDataQualityError):
        await metric_source.fetch_snapshots(query(**updates))
    assert not service.calls


@pytest.mark.asyncio
async def test_candidate_counter_must_match_authoritative_bucket():
    metric_source, service, _ = source()
    service.result.rows[0]["clicks"] += 1
    with pytest.raises(HotNewsDataQualityError, match="candidate metrics differ"):
        await metric_source.fetch_snapshots(query())


@pytest.mark.asyncio
async def test_baseline_tool_cannot_run_before_metric_tool():
    metric_source, _, _ = source()
    with pytest.raises(HotNewsDataQualityError, match="before its metric tool"):
        await metric_source.get_baselines(
            tenant_id=TENANT, window_start=WINDOW_START, window_end=END,
            production_bundle_version=BUNDLE, metric_keys=frozenset(),
        )


@pytest.mark.asyncio
async def test_helper_requires_single_hour_and_ranking_not_trend():
    value, scenario = preview(end=END + timedelta(hours=1))
    service = FakeService(value, scenario)
    with pytest.raises(HotNewsDataQualityError, match="exactly one hourly bucket"):
        await build_native_hot_news_sql_dependencies(
            database=SimpleNamespace(), sql_service=service, query_id=value.query_id, tenant_id=TENANT, user_id=USER,
        )
    value, scenario = preview()
    service = FakeService(value, scenario)
    service.store.snapshot["scenario"]["result_mode"] = "trend"
    with pytest.raises(HotNewsDataQualityError, match="not cross-news hourly trends"):
        await build_native_hot_news_sql_dependencies(
            database=SimpleNamespace(), sql_service=service, query_id=value.query_id, tenant_id=TENANT, user_id=USER,
        )


@pytest.mark.asyncio
async def test_sql_content_and_knowledge_share_all_12_news_ids():
    contents = SqlHotNewsContentRepository(FakeDetails())
    found = await contents.batch_get_by_news_ids(tenant_id=TENANT, news_ids=DEMO_NEWS_IDS)
    assert set(found) == set(DEMO_NEWS_IDS)
    assert all("合成" in item.body for item in found.values())
    with pytest.raises(HotNewsDataQualityError, match="crossed"):
        await contents.batch_get_by_news_ids(
            tenant_id="33333333-3333-4333-8333-333333333333", news_ids=DEMO_NEWS_IDS,
        )
    writer = InMemoryKnowledgeBaseWriter()
    await seed_native_sql_knowledge(SimpleNamespace(
        content_repository=contents, tenant_id=TENANT, news_ids=DEMO_NEWS_IDS,
    ), store=writer)
    assert {item.metadata["news_id"] for item in writer.documents} == set(DEMO_NEWS_IDS)
    assert all(int(item.metadata["content_version"]) > 0 for item in writer.documents)
    assert all(item.metadata["warehouse_schema_version"] == SCHEMA_VERSION for item in writer.documents)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_query_is_mapped_to_hot_news_snapshots():
    url = os.getenv("SQL_WAREHOUSE_INTEGRATION_URL")
    if not url:
        pytest.skip("set SQL_WAREHOUSE_INTEGRATION_URL to isolated local PostgreSQL")
    database = SimpleNamespace(engine=create_async_engine(url))
    try:
        await initialize_demo_warehouse(database, environment="e2e")
        value, scenario = preview()
        service = FakeService(value, scenario)

        async def execute_for_run(query_id):
            execution = await LocalPostgresSqlWarehouseClient(database, tenant_id=TENANT).execute(SqlQuery(
                sql=value.sql, params={"tenant_id": TENANT, "window_start": WINDOW_START,
                                       "window_end": END, "row_limit": 5}, timeout_ms=10000, max_rows=5,
            ))
            rows = [{key: str(cell) if isinstance(cell, Decimal) else cell for key, cell in row.items()}
                    for row in execution.rows]
            return SqlAssistantResult(
                query_id=query_id, columns=list(rows[0]), rows=rows, row_count=len(rows),
                elapsed_ms=execution.elapsed_ms, truncated=False, summary="local Pg", stages=[], sql_hash=value.sql_hash,
            )

        dependencies = await build_native_hot_news_sql_dependencies(
            database=database, sql_service=service, query_id=value.query_id,
            tenant_id=TENANT, user_id=USER, execute_for_run=execute_for_run,
        )
        snapshots = await dependencies.metric_source.fetch_snapshots(query())
        assert len(snapshots) == 5
        assert all(item.unique_users > 0 and item.total_duration_seconds > 0 for item in snapshots)
        keys = frozenset((item.news_id, item.content_type) for item in snapshots)
        baselines = await dependencies.baseline_provider.get_baselines(
            tenant_id=TENANT, window_start=WINDOW_START, window_end=END,
            production_bundle_version=dependencies.production_bundle_version, metric_keys=keys,
        )
        assert len(HotNewsRanker().rank(snapshots, baselines)) == 5
        contents = await dependencies.content_repository.batch_get_by_news_ids(
            tenant_id=TENANT, news_ids=dependencies.news_ids,
        )
        assert set(contents) == set(DEMO_NEWS_IDS)
        trace = await dependencies.metric_source.get_tool_trace()
        assert trace["preview"]["query_id"] == value.query_id
        assert trace["result"]["row_count"] == 5
    finally:
        await database.engine.dispose()
