"""Per-news visitor aggregates reach the real model port without changing hot ranks."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.modules.hot_news.analysis.hot_news_enrichment import HotNewsEnrichmentService
from app.analytics.news_content import InMemoryNewsContentRepository, NewsContent
from app.modules.audience.taxonomy import TopicCatalog
from app.domain.errors import HotNewsDataQualityError
from app.modules.data_loop.bundle_runtime import NativeProductionBundleRuntimeRegistry
from app.modules.hot_news.analysis.runner import local_hot_news_prompts
from app.model_runtime.local_inference import LocalHotNewsInference
from app.modules.reader_profiles.contracts import (
    PROFILE_POLICY_VERSION, ReaderInterestSummary, ReaderProfileBatch, ReaderProfileProviderError,
    ReaderProfileSummary, ReaderTopic, reader_profile_query_fingerprint,
)
from app.modules.hot_news.analysis.hot_news_analysis import HotNewsAnalysisInputBuilder, HotNewsAnalysisService, HotNewsAnalysisValidator
from app.modules.hot_news.application.hot_news_orchestration import (
    EmptyHotNewsBaselineProvider, HotNewsOrchestrationPolicy, HotNewsOrchestrationService, HotNewsRunRequest,
)
from app.modules.hot_news.application.hot_news_query import HotNewsQueryService
from app.modules.hot_news.adapters.hot_news_run_store import PostgresHotNewsRunStore
from app.modules.data_loop.push_strategy import PushStrategyInputBuilder
from simulation.hot_news_demo import build_ranking, load_scenario
from tests.test_behavior_hot_news_integration import active_service
from tests.test_hot_news_orchestration import START, END
from tests.test_native_hot_news_runtime import native_spec
from tests.test_push_strategy import analysis_input as push_analysis_input, context as push_context, report as push_report
from tests.test_sql_assistant_service import harness as sql_harness


TENANT = "00000000-0000-0000-0000-000000000123"
PROMPT = "native-e2e-prompt-v3-reader-profiles"


def interest(topic_id, *, short=False):
    count = 4 if short else 6
    return ReaderInterestSummary(
        lookback_hours=24 if short else 720, profiled_reader_count=8, unknown_reader_count=2,
        cold_start_reader_count=1, baseline_profiled_count=10,
        topics=(ReaderTopic(topic_id=topic_id, reader_count=count, share_all=str(count / 10),
            share_profiled=str(count / 8), baseline_reader_count=5 if short else 6, baseline_profiled_count=10,
            baseline_share="0.5" if short else "0.6", lift=None if short else "1.25", low_sample=short),),
    )


def summary(news_id, *, cutoff=START, content_sha256=None):
    topic = "science" if news_id.endswith("a") else "finance"
    return ReaderProfileSummary(news_id=news_id, source_mode="synthetic", taxonomy_version="reader-topics-v1",
        profile_policy_version=PROFILE_POLICY_VERSION, content_sha256=content_sha256, content_topic_ids=(topic,),
        reader_count=10, long_term=interest(topic), short_term=interest("sports", short=True),
        profile_cutoff=cutoff, trigger_article_excluded=True, count_method="exact_synthetic_distinct", status="complete",
        limitations=("合成访问样例；多标签人数不可相加。",))


class Aggregates:
    identity_sha256 = "a" * 64
    source_release_sha256 = "b" * 64
    timeout_seconds = 3
    run_timeout_seconds = 5
    allowed_analysis_prompt_versions = frozenset({PROMPT})

    def __init__(self):
        self.calls = []

    def context_identity_for_window(self, **kwargs):
        identity = (str(kwargs["tenant_id"]), kwargs["window_start"].isoformat(), kwargs["window_end"].isoformat())
        return sha256("|".join(identity).encode()).hexdigest()

    async def fetch(self, query):
        self.calls.append(query)
        await asyncio.sleep(0)
        return ReaderProfileBatch(tenant_id=query.tenant_id, window_start=query.window_start, window_end=query.window_end,
            source_release_sha256=self.source_release_sha256, query_fingerprint=reader_profile_query_fingerprint(query),
            snapshot_id="snapshot-" + reader_profile_query_fingerprint(query)[:16], source_revision=1,
            taxonomy_version="reader-topics-v1", profile_policy_version=PROFILE_POLICY_VERSION,
            generated_at=query.window_end + timedelta(minutes=1), watermark=query.window_end, is_complete=True,
            source_mode="synthetic", items=tuple(summary(news_id, cutoff=query.window_start) for news_id in query.news_ids))


class RecordingInference(LocalHotNewsInference):
    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return await super().complete(request)


def chain(reader_service=None):
    rankings = build_ranking(load_scenario(), limit=2)
    snapshots = [replace(row.current, news_id=news_id, window_start=START, window_end=END)
        for row, news_id in zip(rankings, ("news-a", "news-b"), strict=True)]
    contents = [NewsContent(row.news_id, "科学与财经研究", "可信测试摘要", row.content_type, START,
        "https://example.com/" + row.news_id, body="可信测试正文") for row in snapshots]
    search = SimpleNamespace(batch_search_related_news=AsyncMock(side_effect=lambda queries, **_:
        {query.query_id: [] for query in queries}))
    enrichment = HotNewsEnrichmentService(InMemoryNewsContentRepository(contents), search,
        reader_profile_service=reader_service, reader_profile_window=(START, END))
    inference = RecordingInference()
    spec = native_spec(analysis_prompt_version=PROMPT)
    registry = NativeProductionBundleRuntimeRegistry(inference, local_hot_news_prompts(), (spec,))
    service = HotNewsOrchestrationService(
        metric_source=SimpleNamespace(fetch_snapshots=AsyncMock(return_value=snapshots)),
        baseline_provider=EmptyHotNewsBaselineProvider(), enrichment_service=enrichment,
        analysis_service=HotNewsAnalysisService(input_builder=HotNewsAnalysisInputBuilder(policy_version=PROMPT),
            runner=registry.create(spec, tenant_id=TENANT), validator=HotNewsAnalysisValidator()),
        policy=HotNewsOrchestrationPolicy(production_bundle_version="reader-bundle", ranking_limit=2),
    )
    request = HotNewsRunRequest(TENANT, START, END, "reader-bundle", reader_profile_context_sha256=(
        reader_service.context_identity_for_window(tenant_id=TENANT, window_start=START, window_end=END)
        if reader_service else None))
    return service, request, inference, search


@pytest.mark.asyncio
async def test_per_news_profiles_reach_model_port_and_saved_view_without_changing_scores():
    aggregates = Aggregates()
    service, request, inference, _ = chain(aggregates)
    plain, old_request, _, _ = chain()
    old = await plain.run(old_request)
    result = await service.run(request)
    assert result.ranked_news == old.ranked_news
    assert result.metric_snapshots == old.metric_snapshots
    assert len(aggregates.calls) == 1
    assert aggregates.calls[0].news_ids == ("news-a", "news-b")
    payloads = {json.loads(req.messages[1].content)["news_id"]: json.loads(req.messages[1].content)
        for req in inference.requests}
    assert payloads["news-a"]["reader_profile_summary"]["long_term"]["topics"][0]["topic_id"] == "science"
    assert payloads["news-b"]["reader_profile_summary"]["long_term"]["topics"][0]["topic_id"] == "finance"
    reports = {row.news_id: row.analysis.value for row in result.analyzed_news}
    assert "science" in " ".join(row.action for row in reports["news-a"].operation_suggestions)
    assert "finance" in " ".join(row.action for row in reports["news-b"].operation_suggestions)
    assert all(any("合成" in limitation for limitation in report.limitations) for report in reports.values())
    assert all(any("覆盖" in limitation for limitation in report.limitations) for report in reports.values())
    assert all(any("lift" in limitation for limitation in report.limitations) for report in reports.values())
    assert all(req.prompt_version == PROMPT for req in inference.requests)

    saved = PostgresHotNewsRunStore._serialize_result(result)
    view = HotNewsQueryService._parse_ranked_news(SimpleNamespace(payload_schema_version="2.0", result_payload=saved))
    assert {row.news_id: row.reader_profile_summary for row in view} == {
        row.news_id: row.analysis_input.reader_profile_summary for row in result.analyzed_news}
    assert all(row.reader_profile_trace["source_release_sha256"] == "b" * 64 for row in view)
    assert all(row.reader_profile_trace["source_revision"] == 1 for row in view)
    assert "synthetic_user_id" not in json.dumps(saved)
    assert "user_id" not in json.dumps(payloads)
    assert old.analyzed_news[0].reader_profile_summary is None


@pytest.mark.asyncio
async def test_historical_snapshots_still_parse_without_reader_fields():
    service, request, _, _ = chain()
    saved = PostgresHotNewsRunStore._serialize_result(await service.run(request))
    for entry in saved["analyzed_news"]:
        entry.pop("reader_profile_summary", None)
        entry.pop("reader_profile_trace", None)
        entry["analysis_input"].pop("reader_profile_summary", None)
    view = HotNewsQueryService._parse_ranked_news(SimpleNamespace(payload_schema_version="2.0", result_payload=saved))
    assert all(row.reader_profile_summary is None and row.reader_profile_trace is None for row in view)


@pytest.mark.asyncio
async def test_reader_trace_freezes_only_this_news_topic_names_from_bound_catalog():
    aggregates = Aggregates()
    aggregates.catalog = TopicCatalog.model_validate({"taxonomy_version": "reader-topics-v1", "status": "draft", "topics": [
        {"topic_id": topic_id, "name": name, "provenance": "draft_extension"}
        for topic_id, name in (("science", "科学"), ("finance", "财经"), ("sports", "体育"), ("other", "其他"))]})
    service, request, _, _ = chain(aggregates)
    saved = PostgresHotNewsRunStore._serialize_result(await service.run(request))
    # The API uses this frozen map even if the current directory is later renamed.
    aggregates.catalog = None
    view = HotNewsQueryService._parse_ranked_news(SimpleNamespace(payload_schema_version="2.0", result_payload=saved))
    traces = {row.news_id: row.reader_profile_trace for row in view}
    assert traces["news-a"]["topic_names"] == {"science": "科学", "sports": "体育"}
    assert traces["news-b"]["topic_names"] == {"finance": "财经", "sports": "体育"}
    assert all(trace["taxonomy_version"] == "reader-topics-v1" for trace in traces.values())


@pytest.mark.parametrize("digest", ["", "a" * 63, "A" * 64, "g" * 64, 1, False])
def test_request_rejects_invalid_reader_context(digest):
    with pytest.raises(ValueError, match="reader profile context"):
        HotNewsRunRequest(TENANT, START, END, "bundle", reader_profile_context_sha256=digest).validate()


def test_reader_context_separates_run_keys_with_legacy_and_behavior_namespaces_preserved():
    legacy = HotNewsRunRequest(TENANT, START, END, "bundle")
    old_identity = "\x1f".join((TENANT, START.isoformat(), END.isoformat(), "bundle"))
    assert legacy.idempotency_key == "hot-news-" + sha256(old_identity.encode()).hexdigest()
    reader = replace(legacy, reader_profile_context_sha256="a" * 64)
    changed = replace(reader, reader_profile_context_sha256="b" * 64)
    behavior = replace(legacy, retrieval_context_sha256="a" * 64)
    assert len({row.idempotency_key for row in (legacy, reader, changed, behavior,
        replace(reader, retrieval_context_sha256="a" * 64))}) == 5
    sql = replace(legacy, sql_query_id=str(uuid4()), sql_query_user_id=str(uuid4()), sql_query_scope_sha256="c" * 64)
    assert sql.idempotency_key != replace(sql, reader_profile_context_sha256="a" * 64).idempotency_key


def ranked_many(count):
    first = build_ranking(load_scenario(), limit=1)[0]
    return [replace(first, rank=index + 1, current=replace(first.current, news_id=f"news-{index}",
        window_start=START, window_end=END)) for index in range(count)]


def batch_enricher(aggregates):
    search = SimpleNamespace(batch_search_related_news=AsyncMock(return_value={}))
    return HotNewsEnrichmentService(InMemoryNewsContentRepository([]), search,
        reader_profile_service=aggregates, reader_profile_window=(START, END)), search


@pytest.mark.asyncio
async def test_top100_fetches_five_bounded_batches_without_truncation_or_per_article_calls():
    aggregates = Aggregates()
    enrichment, _ = batch_enricher(aggregates)
    result = await enrichment.enrich(ranked_many(100), tenant_id=TENANT)
    assert len(result) == 100
    assert len(aggregates.calls) == 5
    assert all(len(query.news_ids) == 20 for query in aggregates.calls)
    assert {row.reader_profile_summary.news_id for row in result} == {f"news-{index}" for index in range(100)}


@pytest.mark.asyncio
async def test_reader_batch_global_deadline_cancels_pending_callers():
    class Slow(Aggregates):
        run_timeout_seconds = .02
        cancelled = 0

        async def fetch(self, query):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled += 1
    aggregates = Slow()
    enrichment, search = batch_enricher(aggregates)
    with pytest.raises(ReaderProfileProviderError) as error:
        await enrichment.enrich(ranked_many(21), tenant_id=TENANT)
    assert error.value.code == "timeout"
    assert aggregates.cancelled == 2
    search.batch_search_related_news.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["tenant", "window", "release", "fingerprint", "news", "trigger"])
async def test_reader_batch_rejects_scope_mismatch_before_model_or_retrieval(change):
    class Wrong(Aggregates):
        async def fetch(self, query):
            batch = await super().fetch(query)
            updates = {
                "tenant": {"tenant_id": uuid4()}, "window": {"window_end": END + timedelta(hours=1)},
                "release": {"source_release_sha256": "c" * 64}, "fingerprint": {"query_fingerprint": "d" * 64},
                "news": {"items": (summary("unrequested"),)},
                "trigger": {"items": tuple(item.model_copy(update={"trigger_article_excluded": False}) for item in batch.items)},
            }
            return batch.model_copy(update=updates[change])
    enrichment, search = batch_enricher(Wrong())
    with pytest.raises(HotNewsDataQualityError, match="reader profile response binding"):
        await enrichment.enrich(ranked_many(2), tenant_id=TENANT)
    search.batch_search_related_news.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_release_different_batch_revisions_fail_entire_run_before_any_model_call():
    class ChangedRevision(Aggregates):
        async def fetch(self, query):
            batch = await super().fetch(query)
            return batch.model_copy(update={"source_revision": 1 if "news-0" in query.news_ids else 2})
    aggregates = ChangedRevision()
    service, request, inference, search = chain(aggregates)
    ranked = ranked_many(21)
    service.metric_source.fetch_snapshots.return_value = [row.current for row in ranked]
    service.policy = replace(service.policy, ranking_limit=21)
    service.enrichment_service.content_repository = InMemoryNewsContentRepository([
        NewsContent(row.current.news_id, "可靠测试正文", "摘要", row.current.content_type, START,
            "https://example.com/" + row.current.news_id, body="正文") for row in ranked])
    with pytest.raises(HotNewsDataQualityError, match="reader profile response binding"):
        await service.run(request)
    assert len(aggregates.calls) == 2
    assert inference.requests == []
    search.batch_search_related_news.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_source_body_cannot_reuse_old_reader_content_binding():
    class Changed(Aggregates):
        async def fetch(self, query):
            batch = await super().fetch(query)
            return batch.model_copy(update={"items": tuple(item.model_copy(update={"content_sha256": "c" * 64})
                for item in batch.items)})
    service, request, inference, search = chain(Changed())
    with pytest.raises(HotNewsDataQualityError, match="content binding"):
        await service.run(request)
    assert not inference.requests
    search.batch_search_related_news.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", [False, True])
async def test_active_bundle_passes_frozen_reader_service_and_window_to_both_paths(monkeypatch, sql):
    service, request, factory, _, _ = active_service(monkeypatch, sql=sql)
    aggregates = Aggregates()
    aggregates.allowed_analysis_prompt_versions = frozenset()
    service._reader_profile_service = aggregates
    request = replace(request, reader_profile_context_sha256=aggregates.context_identity_for_window(
        tenant_id=request.tenant_id, window_start=START, window_end=END))
    await service.run(request)
    enrichment = factory.call_args.kwargs["enrichment_service"]
    assert enrichment.reader_profile_service is aggregates
    assert enrichment.reader_profile_window == (START, END)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "changed", "disabled", "prompt"])
async def test_active_reader_binding_failures_stop_before_sql_or_model(monkeypatch, failure):
    service, request, factory, repository, dependencies = active_service(monkeypatch, sql=True)
    aggregates = Aggregates()
    service._reader_profile_service = aggregates if failure != "disabled" else None
    digest = aggregates.context_identity_for_window(tenant_id=request.tenant_id, window_start=START, window_end=END)
    request = replace(request, reader_profile_context_sha256=None if failure == "missing" else
        "f" * 64 if failure == "changed" else digest)
    with pytest.raises(HotNewsDataQualityError, match="reader profile"):
        await service.run(request)
    assert repository.get_active_bundle.await_count == int(failure == "prompt")
    dependencies.assert_not_awaited()
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_runs_keep_reader_windows_isolated(monkeypatch):
    service, request, factory, _, _ = active_service(monkeypatch)
    aggregates = Aggregates()
    aggregates.allowed_analysis_prompt_versions = frozenset()
    service._reader_profile_service = aggregates
    later = replace(request, window_start=END, window_end=END + timedelta(hours=1))
    requests = [replace(item, reader_profile_context_sha256=aggregates.context_identity_for_window(
        tenant_id=item.tenant_id, window_start=item.window_start, window_end=item.window_end)) for item in (request, later)]
    await asyncio.gather(*(service.run(item) for item in requests))
    windows = [call.kwargs["enrichment_service"].reader_profile_window for call in factory.call_args_list]
    assert windows == [(START, END), (END, END + timedelta(hours=1))]
    assert service._enrichment_service.reader_profile_service is None


def test_push_input_copies_aggregate_evidence_without_expanding_policy_or_removing_manual_gate():
    original = push_analysis_input()
    profile = summary(original.news_id, cutoff=original.window_start)
    trusted = original.model_copy(update={"reader_profile_summary": profile})
    policy = push_context()
    built = PushStrategyInputBuilder().build(event_id="event-1", analysis_input=trusted, analysis_report=push_report(),
        policy_context=policy, suggested_deep_link="https://news.qq.com/rain/a/news-1", strategy_version="push-v1")
    assert built.reader_profile_summary == profile
    assert built.reader_profile_summary is not profile
    assert built.audience_options.interest_tags == tuple(policy.allowed_interest_tags)
    assert "user_id" not in built.model_dump_json()


def test_reader_prompt_is_explicit_and_legacy_assets_are_unchanged():
    prompts = local_hot_news_prompts()
    old = prompts.resolve(scene="hot_news_analysis", version="native-e2e-prompt-v2")
    new = prompts.resolve(scene="hot_news_analysis", version=PROMPT)
    assert "reader_profile_summary" not in old.system_prompt
    assert "reader_profile_summary" in new.system_prompt
    assert "因果" in new.system_prompt and "预期CTR" in new.system_prompt and "合成样例" in new.system_prompt


@pytest.mark.asyncio
async def test_manual_query_freezes_reader_digest_before_claiming_sql_and_starting_workflow(sql_harness, monkeypatch):
    import simulation.local_simulation as shared
    from app.api.dependencies import DataLoopPrincipal, HotNewsPermission
    from app.schemas.sql_assistant import SqlAssistantPreviewRequest
    from app.sql_assistant.warehouse import WINDOW_START
    from app.workflows.contracts import HotNewsActivityOutcome
    from simulation.hot_news_e2e_support import E2E_TENANT_ID

    spec = native_spec(analysis_prompt_version=PROMPT)
    monkeypatch.setattr(shared, "PostgresProductionBundleRepository", lambda _: SimpleNamespace(
        get_active_bundle=AsyncMock(return_value=SimpleNamespace(spec=spec, bundle_version="reader-bundle"))))
    tool = sql_harness.service
    tool.hot_news_bindings = SimpleNamespace(get_or_claim=AsyncMock(side_effect=lambda **kwargs: {"query_id": kwargs["query_id"]}))
    completed = HotNewsActivityOutcome(str(uuid4()), "unused", "completed", 1, 1, 1, 1)
    start = AsyncMock(return_value=SimpleNamespace(result=AsyncMock(return_value=completed)))
    aggregates = Aggregates()
    from tests.test_behavior_hot_news_integration import Database
    from uuid import UUID
    state = SimpleNamespace(settings=SimpleNamespace(reader_profile_enabled=True,
        hot_news_runtime_manifest_json=json.dumps([spec.model_dump(mode="json")]),
        model_runtime_config_path="deploy/model-runtime.reader-profiles.local.yml",
        sql_assistant_scenarios_path=str(sql_harness.scenarios_path), temporal_hot_news_task_queue="reader-test"),
        reader_profile_service=aggregates, sql_assistant=tool, database=Database(),
        temporal=SimpleNamespace(start_workflow=start))
    body = SqlAssistantPreviewRequest(question="查询点击量最高的前5条新闻", scenario_id="news-ranking",
        window_start=WINDOW_START, window_end=WINDOW_START + timedelta(hours=1))
    principal = DataLoopPrincipal(tenant_id=UUID(E2E_TENANT_ID), user_id=uuid4(),
        permissions=frozenset({HotNewsPermission.ADMIN.value}))
    result = await shared.execute_local_hot_news_query(state=state, principal=principal, body=body)
    sent = start.call_args.args[1]
    assert sent.reader_profile_context_sha256 == aggregates.context_identity_for_window(tenant_id=E2E_TENANT_ID,
        window_start=body.window_start, window_end=body.window_end)
    assert sent.idempotency_key != replace(sent, reader_profile_context_sha256=None).idempotency_key
    assert result.workflow_id == tool.hot_news_bindings.get_or_claim.call_args.kwargs["run_key"] == sent.idempotency_key


@pytest.mark.asyncio
async def test_worker_runtime_closes_reader_service_even_if_knowledge_close_fails():
    from app.bootstrap.hot_news import NativeHotNewsRuntime
    knowledge = SimpleNamespace(close=AsyncMock(side_effect=OSError("test close failure")))
    aggregates = SimpleNamespace(close=AsyncMock())
    with pytest.raises(OSError, match="test close failure"):
        await NativeHotNewsRuntime(knowledge, aggregates).close()
    aggregates.close.assert_awaited_once()
