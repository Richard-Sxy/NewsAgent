"""Behavior topics alter related evidence, never authoritative hot-news metrics."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

import app.services.active_bundle_hot_news as active_module
from app.analytics.entities import ContentType
from app.analytics.hot_news_enrichment import HotNewsEnrichmentService
from app.analytics.news_content import InMemoryNewsContentRepository, NewsContent
from app.behavior_tags.runtime import BehaviorRetrievalContext
from app.behavior_tags.tagging import TopicPolicy
from app.clients.knowledge_base import RelatedNews
from app.domain.errors import AgentOutputValidationError, HotNewsDataQualityError
from app.services.active_bundle_hot_news import ActiveProductionBundleHotNewsService, HotNewsRunDependencies
from app.services.hot_news_analysis import HotNewsAnalysisInputBuilder, HotNewsAnalysisService, HotNewsAnalysisValidator
from app.services.hot_news_orchestration import (
    EmptyHotNewsBaselineProvider, HotNewsOrchestrationPolicy, HotNewsOrchestrationService,
    HotNewsRunRequest, HotNewsRunResult,
)
from app.services.hot_news_query import HotNewsQueryService
from app.services.hot_news_run_store import PostgresHotNewsRunStore
from examples.hot_news_demo import build_ranking, load_scenario
from tests.test_hot_news_orchestration import START, END, RecordingRunner
from tests.test_production_bundle import active_bundle
from tests.test_sql_assistant_service import harness as sql_harness


def context(tenant_id="tenant-1", digest="a" * 64):
    policy = TopicPolicy.model_validate({
        "policy_version": "test-topic-rules-v1",
        "catalog": {"taxonomy_version": "test-topics-v1", "status": "draft", "topics": [
            {"topic_id": "science", "name": "科学", "provenance": "draft_extension"},
            {"topic_id": "finance", "name": "财经", "provenance": "draft_extension"},
        ]},
        "rules": [
            {"rule_id": "science-rule", "topic_id": "science", "title_terms": ["星云"], "body_terms": ["星云"]},
            {"rule_id": "finance-rule", "topic_id": "finance", "title_terms": ["股市"], "body_terms": ["股市"]},
        ],
    })
    return BehaviorRetrievalContext(digest, "p" * 64, tenant_id, "all", START.isoformat(),
        "test-aggregate-v1", policy, (("science", 1.0), ("finance", 0.0)), 200, .08)


def domain_chain(behavior_context=None):
    ranking = build_ranking(load_scenario(), limit=1)[0]
    source = NewsContent(ranking.current.news_id, "行业研究观察", "星云与股市研究资料", ContentType.ARTICLE,
        ranking.current.window_start, "https://example.com/source", body="星云与股市研究资料")
    candidates = [RelatedNews(
        collection_id=f"chunk-{news_id}", news_id=news_id, title=title, text=text,
        source_url=f"https://example.com/{news_id}", publish_time=source.publish_time.isoformat(), score=score,
    ) for news_id, title, text, score in [
        ("finance", "市场资料甲", "股市研究资料", .90),
        ("science", "科学资料乙", "星云研究资料", .88),
        ("irrelevant", "星云资料丙", "星云无关资料", .01),
    ]]
    repository = InMemoryNewsContentRepository([source])
    search = SimpleNamespace(batch_search_related_news=AsyncMock(return_value={source.news_id: candidates}))
    enrichment = HotNewsEnrichmentService(repository, search, behavior_context=behavior_context)
    runner = RecordingRunner()
    service = HotNewsOrchestrationService(
        metric_source=SimpleNamespace(fetch_snapshots=AsyncMock(return_value=[ranking.current])),
        baseline_provider=EmptyHotNewsBaselineProvider(), enrichment_service=enrichment,
        analysis_service=HotNewsAnalysisService(input_builder=HotNewsAnalysisInputBuilder(), runner=runner,
            validator=HotNewsAnalysisValidator()),
        policy=HotNewsOrchestrationPolicy(production_bundle_version="bundle-v1", ranking_limit=1,
            related_limit=1, candidate_limit=20),
    )
    request = HotNewsRunRequest("tenant-1", ranking.current.window_start, ranking.current.window_end, "bundle-v1",
        retrieval_context_sha256=behavior_context.identity_sha256 if behavior_context else None)
    return service, request, runner, search


@pytest.mark.asyncio
async def test_real_enrichment_reorders_only_survivors_and_persists_trace_with_model_evidence():
    plain, request, plain_runner, _ = domain_chain()
    boosted, bound_request, runner, _ = domain_chain(context())
    old = await plain.run(request)
    result = await boosted.run(bound_request)

    assert plain_runner.inputs[0].related_news[0].news_id == "finance"
    assert runner.inputs[0].related_news[0].news_id == "science"
    assert old.metric_snapshots == result.metric_snapshots
    assert old.ranked_news == result.ranked_news
    trace = result.analyzed_news[0].retrieval_trace
    assert trace["before_news_ids"] == ["finance", "science"]
    assert trace["after_news_ids"] == ["science"]
    assert all(row["news_id"] != "irrelevant" for row in trace["candidates"])
    assert trace["hot_score_changed"] is False
    assert "合成兴趣主题加分" in runner.inputs[0].related_news[0].rerank_reasons[-1]
    assert old.analyzed_news[0].retrieval_trace is None

    payload = PostgresHotNewsRunStore._serialize_result(result)
    assert payload["analyzed_news"][0]["retrieval_trace"] == trace
    assert payload["request"]["retrieval_context_sha256"] == "a" * 64
    view = HotNewsQueryService._parse_ranked_news(SimpleNamespace(payload_schema_version="2.0", result_payload=payload))
    assert view[0].retrieval_trace == trace
    assert "synthetic_user_id" not in json.dumps(payload)

    invalid = replace(result.analyzed_news[0].analysis, value=result.analyzed_news[0].analysis.value.model_copy(
        update={"evidence_news_ids": ["irrelevant"]}))
    with pytest.raises(AgentOutputValidationError):
        HotNewsAnalysisValidator().validate(analysis_input=runner.inputs[0], result=invalid)


@pytest.mark.asyncio
async def test_enrichment_rejects_foreign_context_before_reading_content_or_retrieval():
    service, request, _, search = domain_chain(context(tenant_id="different-tenant"))
    with pytest.raises(HotNewsDataQualityError, match="tenant scope"):
        await service.run(request)
    search.batch_search_related_news.assert_not_awaited()


@pytest.mark.parametrize("digest", ["", "a" * 63, "A" * 64, "z" * 64, 3, False])
def test_request_rejects_invalid_retrieval_digest(digest):
    with pytest.raises(ValueError, match="retrieval context"):
        HotNewsRunRequest("tenant-1", START, END, "bundle-v1", retrieval_context_sha256=digest).validate()


def test_request_identity_preserves_old_keys_and_separates_contexts():
    legacy = HotNewsRunRequest("tenant-1", START, END, "bundle-v1")
    identity = "\x1f".join(("tenant-1", START.isoformat(), END.isoformat(), "bundle-v1"))
    assert legacy.idempotency_key == f"hot-news-{sha256(identity.encode()).hexdigest()}"
    first = replace(legacy, retrieval_context_sha256="a" * 64)
    second = replace(first, retrieval_context_sha256="b" * 64)
    assert len({legacy.idempotency_key, first.idempotency_key, second.idempotency_key}) == 3
    sql = replace(legacy, sql_query_id=str(uuid4()), sql_query_user_id=str(uuid4()), sql_query_scope_sha256="c" * 64)
    assert replace(sql, retrieval_context_sha256="a" * 64).idempotency_key != sql.idempotency_key
    assert replace(first).idempotency_key == first.idempotency_key


class Database:
    @asynccontextmanager
    async def session(self):
        yield object()


def active_service(monkeypatch, *, behavior_runtime=None, sql=False):
    active = active_bundle()
    repository = SimpleNamespace(get_active_bundle=AsyncMock(return_value=active))
    monkeypatch.setattr(active_module, "PostgresProductionBundleRepository", lambda _: repository)
    request = HotNewsRunRequest(active.tenant_id, START, END, active.bundle_version,
        sql_query_id=str(uuid4()) if sql else None, sql_query_user_id=str(uuid4()) if sql else None,
        sql_query_scope_sha256="c" * 64 if sql else None,
        retrieval_context_sha256="a" * 64 if behavior_runtime else None)
    async def result_for(request):
        await asyncio.sleep(0)
        return HotNewsRunResult(request, request.idempotency_key, 0, (), (), (), ())
    factory = Mock(side_effect=lambda **_: SimpleNamespace(run=result_for))
    monkeypatch.setattr(active_module, "HotNewsOrchestrationService", factory)
    metric_source = SimpleNamespace(get_tool_trace=AsyncMock(return_value={}))
    dependencies = HotNewsRunDependencies(metric_source, object(), object(),
        HotNewsOrchestrationPolicy(production_bundle_version=active.bundle_version))
    dependency_factory = AsyncMock(return_value=dependencies)
    service = ActiveProductionBundleHotNewsService(database=Database(),
        runtime_registry=SimpleNamespace(ensure_supported=Mock(), create=Mock()), behavior_data_source=object(),
        baseline_provider=object(), content_repository=object(), knowledge_search=object(),
        policy_template=dependencies.policy, behavior_runtime=behavior_runtime,
        run_dependencies_factory=dependency_factory if sql else None)
    return service, request, factory, repository, dependency_factory


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", [False, True])
async def test_active_bundle_applies_bound_context_to_default_and_sql_enrichment(monkeypatch, sql):
    active = active_bundle()
    ctx = context(tenant_id=active.tenant_id)
    runtime = SimpleNamespace(context_for_window=Mock(return_value=ctx))
    service, request, factory, _, dependencies = active_service(monkeypatch, behavior_runtime=runtime, sql=sql)
    result = await service.run(request)
    assert factory.call_args.kwargs["enrichment_service"].behavior_context is ctx
    runtime.context_for_window.assert_called_once_with(tenant_id=request.tenant_id, window_start=START, window_end=END)
    assert result.idempotency_key == request.idempotency_key
    assert dependencies.await_count == int(sql)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "changed", "disabled", "foreign", "unavailable"])
async def test_context_binding_failure_stops_before_sql_or_model(monkeypatch, failure):
    active = active_bundle()
    ctx = context(tenant_id=active.tenant_id)
    runtime = SimpleNamespace(context_for_window=Mock(return_value=ctx))
    service, request, factory, repository, dependencies = active_service(monkeypatch,
        behavior_runtime=runtime if failure != "disabled" else None, sql=True)
    if failure == "missing":
        request = replace(request, retrieval_context_sha256=None)
    elif failure in {"changed", "disabled"}:
        request = replace(request, retrieval_context_sha256="b" * 64)
    elif failure == "foreign":
        runtime.context_for_window.return_value = replace(ctx, tenant_id="different-tenant")
    else:
        runtime.context_for_window.side_effect = ValueError("private package issue")
    with pytest.raises(HotNewsDataQualityError, match="behavior retrieval"):
        await service.run(request)
    repository.get_active_bundle.assert_not_awaited()
    dependencies.assert_not_awaited()
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_windows_get_separate_frozen_contexts(monkeypatch):
    active = active_bundle()
    first = context(tenant_id=active.tenant_id)
    second = replace(first, identity_sha256="b" * 64, profile_as_of=END.isoformat())
    runtime = SimpleNamespace(context_for_window=Mock(side_effect=lambda **kwargs:
        first if kwargs["window_start"] == START else second))
    service, request, factory, _, _ = active_service(monkeypatch, behavior_runtime=runtime)
    later = replace(request, window_start=END, window_end=END + timedelta(hours=1), retrieval_context_sha256="b" * 64)
    await asyncio.gather(service.run(request), service.run(later))
    contexts = [call.kwargs["enrichment_service"].behavior_context for call in factory.call_args_list]
    assert contexts == [first, second]
    assert service._enrichment_service.behavior_context is None


@pytest.mark.asyncio
async def test_scheduler_refuses_enable_or_unpause_until_window_context_can_be_frozen(monkeypatch):
    import app.hot_news_scheduler as scheduler
    from app.services.hot_news_schedule import HotNewsScheduleConfigError
    settings = SimpleNamespace(behavior_tags_enabled=True, hot_news_schedule_definitions_json="configured")
    monkeypatch.setattr(scheduler, "get_settings", lambda: settings)
    monkeypatch.setattr(scheduler, "parse_schedule_definitions", lambda _: [object()])
    connect = AsyncMock()
    monkeypatch.setattr(scheduler.Client, "connect", connect)
    with pytest.raises(HotNewsScheduleConfigError, match="Schedule"):
        await scheduler.cmd_ensure()
    with pytest.raises(HotNewsScheduleConfigError, match="Schedule"):
        await scheduler.cmd_state("unpause", "demo")
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_query_freezes_context_before_sql_claim_and_workflow(sql_harness, monkeypatch):
    import examples.local_simulation as shared
    from app.api.dependencies import DataLoopPrincipal, HotNewsPermission
    from app.schemas.sql_assistant import SqlAssistantPreviewRequest
    from app.sql_assistant.warehouse import WINDOW_START
    from app.workflows.contracts import HotNewsActivityOutcome
    from examples.hot_news_e2e_support import E2E_TENANT_ID
    from tests.test_native_hot_news_runtime import native_spec

    spec = native_spec()
    bundle = SimpleNamespace(spec=spec, bundle_version="existing-bundle")
    monkeypatch.setattr(shared, "PostgresProductionBundleRepository", lambda _: SimpleNamespace(
        get_active_bundle=AsyncMock(return_value=bundle)))
    tool = sql_harness.service
    async def claim(**kwargs):
        return {"query_id": kwargs["query_id"]}
    tool.hot_news_bindings = SimpleNamespace(get_or_claim=AsyncMock(side_effect=claim))
    completed = HotNewsActivityOutcome(str(uuid4()), "unused", "completed", 1, 1, 1, 1)
    start = AsyncMock(return_value=SimpleNamespace(result=AsyncMock(return_value=completed)))
    frozen = context(tenant_id=E2E_TENANT_ID)
    behavior = SimpleNamespace(context_for_window=Mock(return_value=frozen))
    state = SimpleNamespace(settings=SimpleNamespace(behavior_tags_enabled=True,
        hot_news_runtime_manifest_json=json.dumps([spec.model_dump(mode="json")]),
        model_runtime_config_path="deploy/model-runtime.local.yml",
        sql_assistant_scenarios_path=str(sql_harness.scenarios_path),
        temporal_hot_news_task_queue="behavior-test"),
        behavior_tags_runtime=behavior, sql_assistant=tool, database=Database(),
        temporal=SimpleNamespace(start_workflow=start))
    body = SqlAssistantPreviewRequest(question="查询点击量最高的前5条新闻", scenario_id="news-ranking",
        window_start=WINDOW_START, window_end=WINDOW_START + timedelta(hours=1))
    principal = DataLoopPrincipal(tenant_id=UUID(E2E_TENANT_ID), user_id=uuid4(),
        permissions=frozenset({HotNewsPermission.ADMIN.value}))
    result = await shared.execute_local_hot_news_query(state=state, principal=principal, body=body)
    sent = start.call_args.args[1]
    assert sent.retrieval_context_sha256 == frozen.identity_sha256
    assert result.workflow_id == sent.idempotency_key
    assert sent.idempotency_key != replace(sent, retrieval_context_sha256=None).idempotency_key
    assert tool.hot_news_bindings.get_or_claim.call_args.kwargs["run_key"] == sent.idempotency_key
    behavior.context_for_window.assert_called_once_with(tenant_id=E2E_TENANT_ID,
        window_start=body.window_start, window_end=body.window_end)
