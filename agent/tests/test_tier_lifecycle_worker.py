"""Dispatch recovery and Temporal sandbox contracts, without a Temporal server."""

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from app.knowledge_lifecycle_worker import LifecycleActivities, dispatch_once
from app.workflows.knowledge_lifecycle import KnowledgeIndexWorkflow, KnowledgeReconcileWorkflow


BUDGETS = dict(max_attempts=3, retry_initial_interval_seconds=1.0,
               retry_max_interval_seconds=5.0, activity_timeout_seconds=30.0,
               workflow_timeout_seconds=120.0)


def service_with_jobs(*jobs):
    return SimpleNamespace(
        config=SimpleNamespace(scan_batch_size=10, task_queue="isolated-tier-queue", **BUDGETS),
        ledger=SimpleNamespace(list_pending=AsyncMock(return_value=list(jobs)),
                               record_error=AsyncMock()),
    )


def job(identity="abc123", status="pending"):
    return dict(tenant_id="tenant-private", job_id=identity, status=status,
                payload={"body": "article text must not enter workflow history"})


@pytest.mark.asyncio
async def test_lost_dispatch_response_reuses_same_workflow_id_and_keeps_pending_ledger():
    value = job()
    service = service_with_jobs(value)
    describe = AsyncMock(return_value=SimpleNamespace(status=SimpleNamespace(name="RUNNING")))
    client = SimpleNamespace(
        start_workflow=AsyncMock(side_effect=[
            ConnectionError("response lost after Temporal accepted"),
            WorkflowAlreadyStartedError("knowledge-index-abc123", "KnowledgeIndexWorkflow"),
        ]),
        get_workflow_handle=lambda identity: SimpleNamespace(describe=describe),
    )
    with pytest.raises(ConnectionError):
        await dispatch_once(client=client, service=service)
    assert value["status"] == "pending"
    service.ledger.record_error.assert_not_awaited()
    assert await dispatch_once(client=client, service=service) == "abc123"
    calls = client.start_workflow.await_args_list
    assert [call.kwargs["id"] for call in calls] == ["knowledge-index-abc123"] * 2
    assert calls[1].kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
    assert calls[1].kwargs["execution_timeout"] == timedelta(seconds=120)
    assert calls[1].args[1] == {**BUDGETS, "tenant_id": "tenant-private", "job_id": "abc123"}
    describe.assert_awaited_once()
    service.ledger.record_error.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"])
async def test_closed_failed_workflow_is_recorded_as_terminal_for_explicit_retry(terminal):
    service = service_with_jobs(job())
    client = SimpleNamespace(
        start_workflow=AsyncMock(side_effect=WorkflowAlreadyStartedError(
            "knowledge-index-abc123", "KnowledgeIndexWorkflow")),
        get_workflow_handle=lambda identity: SimpleNamespace(describe=AsyncMock(
            return_value=SimpleNamespace(status=SimpleNamespace(name=terminal)))),
    )
    await dispatch_once(client=client, service=service)
    service.ledger.record_error.assert_awaited_once_with(
        tenant_id="tenant-private", job_id="abc123", error="Workflow" + terminal, retryable=False)


@pytest.mark.asyncio
async def test_dispatch_advances_over_completed_cleanup_jobs_without_reopening_history():
    service = service_with_jobs(job("first", "complete"), job("second", "retry"), job("third", "complete"))
    client = SimpleNamespace(start_workflow=AsyncMock())
    assert await dispatch_once(client=client, service=service, after_job_id="cursor") == "third"
    service.ledger.list_pending.assert_awaited_once_with(limit=10, after_job_id="cursor")
    assert client.start_workflow.await_count == 1
    assert client.start_workflow.await_args.kwargs["id"] == "knowledge-index-second"
    service.ledger.list_pending.return_value = []
    assert await dispatch_once(client=client, service=service, after_job_id="third") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow_class", [KnowledgeIndexWorkflow, KnowledgeReconcileWorkflow])
async def test_workflows_prepare_in_real_temporal_sandbox(workflow_class):
    definition = workflow._Definition.must_from_class(workflow_class)
    SandboxedWorkflowRunner().prepare_workflow(definition)


@pytest.mark.asyncio
async def test_index_workflow_has_finite_activity_and_retry_budgets(monkeypatch):
    execute = AsyncMock(return_value={"status": "complete"})
    monkeypatch.setattr("app.workflows.knowledge_lifecycle.workflow.execute_activity", execute)
    result = await KnowledgeIndexWorkflow().run({**BUDGETS, "tenant_id": "tenant", "job_id": "job"})
    assert result == {"status": "complete"}
    call = execute.await_args
    assert call.args == ("process_knowledge_index_job", {"tenant_id": "tenant", "job_id": "job"})
    assert call.kwargs["start_to_close_timeout"] == timedelta(seconds=30)
    assert call.kwargs["schedule_to_close_timeout"] == timedelta(seconds=120)
    assert call.kwargs["retry_policy"].maximum_attempts == 3


@pytest.mark.asyncio
async def test_reconcile_workflow_resumes_pagination_including_cleanup_pages(monkeypatch):
    # Existing histories without the patch marker retain the old event sequence.
    monkeypatch.setattr("app.workflows.knowledge_lifecycle.workflow.patched", lambda _name: False)
    execute = AsyncMock(side_effect=[("tenant", "next"), None])
    monkeypatch.setattr("app.workflows.knowledge_lifecycle.workflow.execute_activity", execute)
    result = await KnowledgeReconcileWorkflow().run({**BUDGETS, "after_key": ["tenant", "previous"]})
    assert result == {"status": "complete"}
    assert [call.args for call in execute.await_args_list] == [
        ("scan_knowledge_lifecycle_page", {"after_key": ["tenant", "previous"]}),
        ("scan_knowledge_lifecycle_page", {"after_key": ("tenant", "next")}),
    ]


@pytest.mark.asyncio
async def test_activity_failure_is_redacted_and_exhaustion_is_non_retryable():
    service = SimpleNamespace(
        process_job=AsyncMock(side_effect=RuntimeError("sdk URL with credential must remain private")),
        ledger=SimpleNamespace(get_job=AsyncMock(return_value={"status": "failed"})),
    )
    with pytest.raises(ApplicationError) as error:
        await LifecycleActivities(service).process({"tenant_id": "tenant", "job_id": "job"})
    assert error.value.non_retryable
    assert "RuntimeError" in str(error.value)
    assert "credential" not in str(error.value)


@pytest.mark.asyncio
async def test_scan_activity_converts_persisted_cursor_and_reaches_service():
    service = SimpleNamespace(scan_page=AsyncMock(return_value=None))
    assert await LifecycleActivities(service).scan({"after_key": ["tenant", "withdrawn-news"]}) is None
    service.scan_page.assert_awaited_once_with(after_key=("tenant", "withdrawn-news"))


@pytest.mark.asyncio
async def test_real_temporal_dispatch_index_migrate_and_withdraw_in_isolated_postgres_schema():
    """Opt-in deployed Temporal/PG contract; the model/index are local test Ports.

    Both explicit environment variables are required. This creates only a random
    PostgreSQL schema, task queue and workflow IDs, and removes its schema after
    the run. It measures correctness, never enterprise ANN/model performance.
    """
    address = os.environ.get("NEWSAGENT_TIER_LIFECYCLE_TEST_TEMPORAL_ADDRESS")
    url = os.environ.get("NEWSAGENT_TIER_LIFECYCLE_TEST_DATABASE_URL")
    if not address or not url:
        pytest.skip("set explicit Temporal address and isolated PostgreSQL test URL")

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from temporalio.client import Client
    from temporalio.worker import Worker
    from app.modules.knowledge.document import KnowledgeDocument
    from app.modules.knowledge.tier_lifecycle import KnowledgeLifecycleService
    from app.modules.knowledge.tier_lifecycle_config import load_knowledge_lifecycle_config
    from app.modules.knowledge.tier_lifecycle_index import GuardedTieredIndex
    from app.modules.knowledge.tier_lifecycle_local_index import LocalLifecycleVectorIndex
    from app.modules.knowledge.tier_lifecycle_store import TierLifecycleStore, _metadata
    from app.model_runtime.core import EmbeddingRequest
    from app.model_runtime.local_embedding import LocalHashEmbedding
    from app.retrieval.tiered_vector import VectorTier

    suffix = uuid4().hex
    schema, queue = "tier_temporal_test_" + suffix, "tier-temporal-test-" + suffix
    admin = create_async_engine(url)
    engine, client = None, None
    started_ids, schema_created = [], False
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        schema_created = True
        engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        class TestDatabase:
            @asynccontextmanager
            async def session(self):
                async with sessions.begin() as session:
                    yield session

        database = TestDatabase()
        database.engine = engine
        async with engine.begin() as connection:
            await connection.run_sync(_metadata.create_all)
        cfg = load_knowledge_lifecycle_config(
            Path(__file__).parents[1] / "deploy/knowledge-lifecycle.local.example.yml"
        ).model_copy(update={"task_queue": queue, "activity_timeout_seconds": 30.0,
                             "workflow_timeout_seconds": 90.0})
        ledger = TierLifecycleStore(database=database)
        backend = LocalLifecycleVectorIndex(database=database, max_search_rows=1000, environment="e2e")
        index = GuardedTieredIndex(backend, ledger, max_candidates=cfg.query_max_candidates)
        embedding = LocalHashEmbedding(model_routes=(cfg.embedding_version,),
                                       dimensions=cfg.expected_embedding_dimensions)
        service = KnowledgeLifecycleService(ledger=ledger, index=index, embedding=embedding, config=cfg)
        activities = LifecycleActivities(service)
        client = await Client.connect(address, namespace=os.environ.get(
            "NEWSAGENT_TIER_LIFECYCLE_TEST_TEMPORAL_NAMESPACE", "default"))

        async def dispatch_and_wait(job):
            identity = f"knowledge-index-{job['job_id']}"
            started_ids.append(identity)
            await dispatch_once(client=client, service=service)
            return await asyncio.wait_for(client.get_workflow_handle(identity).result(), timeout=100)

        now = datetime.now(UTC)
        accepted = await service.accept(tenant_id="isolated-tenant", now=now, document=KnowledgeDocument(
            document_id="isolated-document", title="分层新闻测试", text="持久任务验证新增迁移和撤稿。",
            media_type="article", text_source="isolated-test",
            metadata={"news_id": "isolated-news", "content_version": "1",
                      "publish_time": (now - timedelta(seconds=10)).isoformat()},
        ))
        assert (await ledger.get_news(tenant_id="isolated-tenant", news_id="isolated-news"))["status"] == "pending"
        async with Worker(client, task_queue=queue,
                          workflows=[KnowledgeIndexWorkflow, KnowledgeReconcileWorkflow],
                          activities=[activities.process, activities.scan], max_concurrent_activities=1):
            assert (await dispatch_and_wait(accepted))["status"] == "complete"
            state = await ledger.get_news(tenant_id="isolated-tenant", news_id="isolated-news")
            assert state["status"] == "active" and state["active_tier"] == "hot"
            vectors = await embedding.embed(EmbeddingRequest(tenant_id="isolated-tenant",
                trace_id="isolated-query", model_route=cfg.embedding_version, texts=("分层新闻",)))

            async def search(tier):
                return await index.search(tier=tier, tenant_id="isolated-tenant", vector=vectors.vectors[0],
                    embedding_version=cfg.embedding_version, exclude_news_ids=frozenset(), limit=5)

            assert [hit.news_id for hit in await search(VectorTier.HOT)] == ["isolated-news"]
            migrated = await ledger.request_migration(tenant_id="isolated-tenant", news_id="isolated-news",
                target_tier="warm", expected_generation=state["generation"])
            assert (await dispatch_and_wait(migrated))["status"] == "complete"
            assert not await search(VectorTier.HOT)
            assert [hit.news_id for hit in await search(VectorTier.WARM)] == ["isolated-news"]
            withdrawal = await service.retract(tenant_id="isolated-tenant", news_id="isolated-news",
                                               source_revision=2)
            assert not await search(VectorTier.WARM)
            assert (await dispatch_and_wait(withdrawal))["status"] == "complete"
            assert (await ledger.get_news(tenant_id="isolated-tenant", news_id="isolated-news"))["status"] == "retracted"
            assert not await search(VectorTier.WARM)
    finally:
        if client is not None:
            for identity in started_ids:
                with suppress(Exception):
                    await client.get_workflow_handle(identity).terminate(reason="isolated lifecycle test cleanup")
        if engine is not None:
            await engine.dispose()
        if schema_created:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
