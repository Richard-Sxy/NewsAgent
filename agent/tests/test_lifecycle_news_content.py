"""Hotspot enrichment must not reintroduce stale source/cache content."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.analytics.entities import ContentType
from app.analytics.hot_news_enrichment import HotNewsEnrichmentService
from app.analytics.lifecycle_news_content import LifecycleNewsContentRepository, guard_run_dependencies
from app.analytics.news_content import InMemoryNewsContentRepository
from app.services.active_bundle_hot_news import HotNewsRunDependencies
from tests.test_tier_lifecycle_runtime import runtime, document, index_document, NOW, TENANT


def ranking(news_id="source"):
    return [SimpleNamespace(current=SimpleNamespace(news_id=news_id, content_type=ContentType.ARTICLE))]


def canonical_repository(runtime):
    return LifecycleNewsContentRepository(database=runtime.database)


@pytest.mark.asyncio
async def test_enrichment_uses_only_active_canonical_source_and_candidate_content(runtime):
    await index_document(runtime, replace(document("source", version=2), title="有效源标题"))
    await index_document(runtime, replace(document("related", version=2), title="有效关联标题"))
    service = HotNewsEnrichmentService(canonical_repository(runtime), runtime.query)
    enriched = await service.enrich(ranking(), tenant_id=TENANT, candidate_limit=5)
    assert enriched[0].content.title == "有效源标题"
    assert enriched[0].content.body == document().text
    assert len(enriched[0].related_news) == 1
    candidate = enriched[0].related_news[0].news
    assert candidate.news_id == "related" and candidate.title == "有效关联标题"
    active = await runtime.ledger.get_job(
        tenant_id=TENANT,
        job_id=(await runtime.ledger.get_news(tenant_id=TENANT, news_id="related"))["active_job_id"],
    )
    assert candidate.collection_id in active["chunk_ids"]
    assert candidate.text in {record["excerpt"] for record in active["records"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["pending", "retracted"])
async def test_pending_correction_and_withdrawn_sources_do_not_fall_back_to_cache(runtime, operation):
    await index_document(runtime, document("source"))
    if operation == "pending":
        await runtime.service.accept(tenant_id=TENANT, document=document("source", version=2), now=NOW)
    else:
        await runtime.service.retract(tenant_id=TENANT, news_id="source", source_revision=2)
    repository = canonical_repository(runtime)
    assert await repository.batch_get_by_news_ids(tenant_id=TENANT, news_ids=("source",)) == {}
    enriched = await HotNewsEnrichmentService(repository, runtime.query).enrich(ranking(), tenant_id=TENANT)
    assert enriched[0].content is None and enriched[0].related_news == ()


@pytest.mark.asyncio
async def test_source_withdrawn_while_recall_is_inflight_is_removed_by_final_gate(runtime):
    await index_document(runtime, document("source"))
    await index_document(runtime, document("related"))

    class SearchThenWithdrawSource:
        index = runtime.guarded

        async def batch_search_related_news(self, queries, *, tenant_id):
            result = await runtime.query.batch_search_related_news(queries, tenant_id=tenant_id)
            await runtime.service.retract(tenant_id=tenant_id, news_id="source", source_revision=2)
            return result

    service = HotNewsEnrichmentService(canonical_repository(runtime), SearchThenWithdrawSource())
    enriched = await service.enrich(ranking(), tenant_id=TENANT)
    assert enriched[0].content is None and enriched[0].related_news == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["correct", "withdraw"])
async def test_candidate_mutated_between_recall_and_hydration_cannot_mix_versions(runtime, operation):
    await index_document(runtime, document("source"))
    await index_document(runtime, replace(document("related"), title="旧关联标题"))

    class SearchThenMutateCandidate:
        index = runtime.guarded

        async def batch_search_related_news(self, queries, *, tenant_id):
            result = await runtime.query.batch_search_related_news(queries, tenant_id=tenant_id)
            if operation == "correct":
                await index_document(runtime, replace(document("related", version=2), title="新关联标题"))
            else:
                await runtime.service.retract(tenant_id=tenant_id, news_id="related", source_revision=2)
            return result

    service = HotNewsEnrichmentService(canonical_repository(runtime), SearchThenMutateCandidate())
    enriched = await service.enrich(ranking(), tenant_id=TENANT)
    assert enriched[0].content is not None
    assert enriched[0].related_news == ()


@pytest.mark.asyncio
async def test_canonical_content_reads_are_tenant_scoped(runtime):
    await index_document(runtime, replace(document("source"), title="本租户标题"))
    await index_document(runtime, replace(document("source", version=2), title="其他租户标题"), tenant_id="other")
    rows = await canonical_repository(runtime).batch_get_by_news_ids(tenant_id=TENANT, news_ids=("source",))
    assert rows["source"].title == "本租户标题"


def test_lifecycle_search_cannot_be_paired_with_legacy_cache(runtime):
    with pytest.raises(ValueError, match="canonical lifecycle content"):
        HotNewsEnrichmentService(InMemoryNewsContentRepository([]), runtime.query)


@pytest.mark.asyncio
async def test_run_scoped_dependency_factory_cannot_replace_canonical_content_with_cache():
    old_content, canonical_content = object(), object()
    dependencies = HotNewsRunDependencies(
        metric_source=object(), baseline_provider=object(), content_repository=old_content, policy=object(),
    )
    request = object()
    factory = AsyncMock(return_value=dependencies)
    guarded = guard_run_dependencies(factory, content_repository=canonical_content)
    result = await guarded(request)
    factory.assert_awaited_once_with(request)
    assert result.content_repository is canonical_content
    assert result.metric_source is dependencies.metric_source
    assert result.baseline_provider is dependencies.baseline_provider
    assert result.policy is dependencies.policy
    assert dependencies.content_repository is old_content


@pytest.mark.asyncio
async def test_hotspot_worker_enforces_lifecycle_even_when_dependency_factory_supplies_legacy_search(monkeypatch):
    import app.hot_news_worker as worker_module
    settings = SimpleNamespace(
        model_runtime_config_path=str(Path(__file__).resolve().parents[1] / "deploy/model-runtime.local.yml"),
        environment="e2e", knowledge_lifecycle_config_path="enabled-lifecycle.yml",
        knowledge_search_config_path=None, temporal_address="unused:7233",
        temporal_namespace="fixture", temporal_hot_news_task_queue="fixture-hotspot",
    )
    database = SimpleNamespace(close=AsyncMock())
    ports = SimpleNamespace(embedding=object(), close=AsyncMock())
    preflight = AsyncMock()
    index = SimpleNamespace(lifecycle_runtime=SimpleNamespace(validate=preflight))
    query, old_search = object(), object()
    runtime = SimpleNamespace(activities=SimpleNamespace(run_hot_news_window=object()), close=AsyncMock())
    create_runtime = Mock(return_value=runtime)
    create_index = Mock(return_value=index)
    monkeypatch.setattr(worker_module, "Database", Mock(return_value=database))
    monkeypatch.setattr(worker_module, "build_model_runtime_ports", Mock(return_value=ports))
    monkeypatch.setattr(worker_module, "build_native_knowledge_index", create_index)
    monkeypatch.setattr(worker_module, "build_native_knowledge_search", Mock(return_value=query))
    monkeypatch.setattr(worker_module, "create_hot_news_worker_runtime", create_runtime)
    monkeypatch.setattr(worker_module, "close_knowledge_search_index", AsyncMock())
    monkeypatch.setattr(worker_module, "Client", SimpleNamespace(connect=AsyncMock(return_value=object())))
    monkeypatch.setattr(worker_module, "Worker", Mock(return_value=SimpleNamespace(run=AsyncMock())))
    old_content = object()
    dependencies = HotNewsRunDependencies(object(), object(), old_content, object())
    await worker_module.run_hot_news_worker(
        behavior_data_source=object(), baseline_provider=object(), content_repository=old_content,
        policy=object(), knowledge_search=old_search, settings=settings,
        inference_port=object(), prompt_registry=object(),
        run_dependencies_factory=AsyncMock(return_value=dependencies),
    )
    create_index.assert_called_once()
    preflight.assert_awaited_once()
    args = create_runtime.call_args.kwargs
    assert args["knowledge_search"] is query
    assert isinstance(args["content_repository"], LifecycleNewsContentRepository)
    per_run = await args["run_dependencies_factory"](object())
    assert per_run.content_repository is args["content_repository"]
