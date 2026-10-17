"""Frozen dispatch contracts and fault isolation for long-running tier maintenance."""

import asyncio
from contextlib import suppress
from datetime import timedelta
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import event
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

from app.knowledge_lifecycle_worker import LifecycleActivities, dispatch_loop, dispatch_once
from app.workflows.knowledge_lifecycle import KnowledgeReconcileWorkflow
from tests.test_tier_lifecycle_runtime import (
    NOW, TENANT, document, index_document, runtime,  # noqa: F401
)


@pytest.mark.asyncio
async def test_dispatch_uses_saved_queue_and_budgets_after_worker_config_changes(runtime):
    job = await runtime.service.accept(tenant_id=TENANT, document=document(), now=NOW)
    runtime.service.config = runtime.config.model_copy(update={
        "task_queue": "new-worker-queue", "max_attempts": 1,
        "activity_timeout_seconds": 1.0, "workflow_timeout_seconds": 2.0,
    })
    client = SimpleNamespace(start_workflow=AsyncMock())
    await dispatch_once(client=client, service=runtime.service)
    call = client.start_workflow.await_args
    assert call.kwargs["task_queue"] == job["config"]["task_queue"]
    assert call.kwargs["execution_timeout"] == timedelta(seconds=job["config"]["workflow_timeout_seconds"])
    for key in ("max_attempts", "retry_initial_interval_seconds", "retry_max_interval_seconds",
                "activity_timeout_seconds", "workflow_timeout_seconds"):
        assert call.args[1][key] == job["config"][key]
    assert "body" not in call.args[1] and "config" not in call.args[1]


@pytest.mark.asyncio
async def test_invalid_job_policy_is_failed_without_blocking_later_dispatch(runtime):
    valid = await runtime.service.accept(tenant_id=TENANT, document=document(), now=NOW)
    bad = {**valid, "job_id": "bad-job", "config": {"schema_version": "invalid"}}
    ledger = SimpleNamespace(list_pending=AsyncMock(return_value=[bad, valid]), record_error=AsyncMock())
    client = SimpleNamespace(start_workflow=AsyncMock())
    await dispatch_once(client=client, service=SimpleNamespace(config=runtime.config, ledger=ledger))
    ledger.record_error.assert_awaited_once_with(
        tenant_id=TENANT, job_id="bad-job", error="DispatchValidationError", retryable=False)
    assert client.start_workflow.await_count == 1
    assert client.start_workflow.await_args.kwargs["id"] == f"knowledge-index-{valid['job_id']}"


@pytest.mark.asyncio
async def test_dispatch_metadata_does_not_fetch_article_vectors_or_chunk_manifest(runtime):
    job = await runtime.service.accept(tenant_id=TENANT, document=document(), now=NOW)
    await runtime.service._records(job)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(runtime.database.engine, "before_cursor_execute", capture)
    try:
        rows = await runtime.ledger.list_pending()
    finally:
        event.remove(runtime.database.engine, "before_cursor_execute", capture)
    assert [row["job_id"] for row in rows] == [job["job_id"]]
    assert all(name not in rows[0] for name in ("records", "payload", "chunk_ids"))
    assert all(f"tier_lifecycle_jobs.{name}" not in statements[0]
               for name in ("records", "payload", "chunk_ids"))
    assert rows[0]["config"] == job["config"]


@pytest.mark.asyncio
async def test_job_uses_frozen_vector_batch_size_after_worker_restart(runtime):
    job = await runtime.service.accept(tenant_id=TENANT,
        document=document(text="较长正文用于检查冻结批次" * 3), now=NOW)
    saved = await runtime.service._records(job)
    calls = []
    original = runtime.raw_index.upsert

    async def capture(**kwargs):
        calls.append(len(kwargs["records"]))
        return await original(**kwargs)

    runtime.raw_index.upsert = capture
    runtime.service.config = runtime.config.model_copy(update={"vector_batch_size": 1})
    await runtime.service.process_job(tenant_id=TENANT, job_id=job["job_id"])
    batch_size = job["config"]["vector_batch_size"]
    assert calls == [min(batch_size, len(saved) - start) for start in range(0, len(saved), batch_size)]
    assert max(calls) > runtime.service.config.vector_batch_size


@pytest.mark.asyncio
async def test_job_uses_frozen_max_attempts_instead_of_current_worker_limit(runtime):
    job = await runtime.service.accept(tenant_id=TENANT, document=document(), now=NOW)
    runtime.service.config = runtime.config.model_copy(update={"max_attempts": 1})
    runtime.raw_index.upsert = AsyncMock(side_effect=RuntimeError("private SDK message"))
    for attempt in range(1, job["config"]["max_attempts"] + 1):
        with pytest.raises(RuntimeError):
            await runtime.service.process_job(tenant_id=TENANT, job_id=job["job_id"])
        current = await runtime.ledger.get_job(tenant_id=TENANT, job_id=job["job_id"])
        assert current["attempts"] == attempt
        assert current["status"] == ("retry" if attempt < job["config"]["max_attempts"] else "failed")
        assert current["last_error"] == "RuntimeError"


@pytest.mark.asyncio
async def test_one_cleanup_failure_is_durable_and_does_not_starve_later_migration(runtime, caplog):
    old = await index_document(runtime, document("a"))
    replacement = await runtime.service.accept(tenant_id=TENANT,
        document=document("a", version=2), now=NOW)
    await index_document(runtime, document("b"))
    original_delete = runtime.raw_index.delete
    blocked_ids = set(old["chunk_ids"])

    async def fail_one_projection(**kwargs):
        if blocked_ids.intersection(kwargs["chunk_ids"]):
            raise RuntimeError("private SDK URL and token")
        return await original_delete(**kwargs)

    runtime.raw_index.delete = fail_one_projection
    future = NOW + timedelta(seconds=runtime.config.hot_max_age_seconds + 1)
    cursor = await runtime.service.scan_page(now=future)
    assert cursor == (TENANT, "b")
    b = await runtime.ledger.get_news(tenant_id=TENANT, news_id="b")
    assert b["desired_tier"] == "warm" and b["active_tier"] == "hot"
    failed_cleanup = await runtime.ledger.get_job(tenant_id=TENANT, job_id=old["job_id"])
    assert failed_cleanup["status"] == "complete"  # no terminal-job resurrection
    assert failed_cleanup["cleanup_dirty"] is True
    assert failed_cleanup["last_error"] == "CleanupRuntimeError"
    current = await runtime.ledger.get_job(tenant_id=TENANT, job_id=replacement["job_id"])
    assert current["last_error"] == "ScanRuntimeError"
    assert "private SDK" not in caplog.text
    assert old["payload"]["body"] not in caplog.text

    runtime.raw_index.delete = original_delete
    await runtime.service.scan_page(now=future)
    recovered = await runtime.ledger.get_job(tenant_id=TENANT, job_id=old["job_id"])
    assert recovered["cleanup_dirty"] is False and recovered["last_error"] is None
    await runtime.service.process_job(tenant_id=TENANT, job_id=replacement["job_id"])
    assert (await runtime.ledger.get_job(tenant_id=TENANT,
        job_id=replacement["job_id"]))["last_error"] is None


@pytest.mark.asyncio
async def test_invalid_news_policy_is_visible_without_blocking_later_rows(runtime):
    first = await index_document(runtime, document("a"))
    await index_document(runtime, document("b"))
    rows = await runtime.ledger.list_news()
    rows[0]["config"] = {"schema_version": "invalid"}
    runtime.service.ledger.list_news = AsyncMock(return_value=rows)
    future = NOW + timedelta(seconds=runtime.config.hot_max_age_seconds + 1)
    assert await runtime.service.scan_page(now=future) == (TENANT, "b")
    assert (await runtime.ledger.get_job(tenant_id=TENANT,
        job_id=first["job_id"]))["last_error"] == "ScanValidationError"
    assert (await runtime.ledger.get_news(tenant_id=TENANT, news_id="b"))["desired_tier"] == "warm"


@pytest.mark.asyncio
async def test_scan_database_failures_are_retried_with_redacted_temporal_error():
    service = SimpleNamespace(scan_page=AsyncMock(side_effect=RuntimeError("private database URI")))
    with pytest.raises(ApplicationError) as exc:
        await LifecycleActivities(service).scan({})
    assert str(exc.value) == "RuntimeError"
    assert not exc.value.non_retryable


@pytest.mark.asyncio
async def test_failed_fault_persistence_is_a_page_failure_not_a_silent_success(runtime):
    await index_document(runtime, document("a"))
    runtime.service.reconcile = AsyncMock(side_effect=RuntimeError("first failure"))
    runtime.service.ledger.record_maintenance_error = AsyncMock(side_effect=ConnectionError("db down"))
    with pytest.raises(ConnectionError):
        await runtime.service.scan_page(now=NOW)


@pytest.mark.asyncio
async def test_distinct_deployment_queues_do_not_compete_for_one_scan_workflow(runtime, monkeypatch):
    started = {}

    async def start(_workflow, _budgets, *, id, task_queue, **_kwargs):
        if id in started:
            raise WorkflowAlreadyStartedError(id, "KnowledgeReconcileWorkflow")
        started[id] = task_queue

    client = SimpleNamespace(start_workflow=AsyncMock(side_effect=start))
    # End each perpetual dispatcher after its first successful scheduling pass.
    monkeypatch.setattr("app.knowledge_lifecycle_worker.asyncio.sleep",
                        AsyncMock(side_effect=asyncio.CancelledError))
    for queue in ("deployment-a", "deployment-b", "deployment-a"):
        service = SimpleNamespace(config=runtime.config.model_copy(update={"task_queue": queue}),
            ledger=SimpleNamespace(list_pending=AsyncMock(return_value=[])))
        with pytest.raises(asyncio.CancelledError):
            await dispatch_loop(client=client, service=service)
    assert set(started.values()) == {"deployment-a", "deployment-b"}
    calls = client.start_workflow.await_args_list
    assert calls[0].kwargs["id"] == calls[2].kwargs["id"]
    assert calls[0].kwargs["id"] != calls[1].kwargs["id"]
    assert "knowledge-lifecycle-reconcile" not in started
    # Each continued run has its own finite budget; no whole-scan execution cap.
    assert all("run_timeout" in call.kwargs and "execution_timeout" not in call.kwargs for call in calls)


@pytest.mark.asyncio
async def test_new_scan_checkpoints_every_page_so_run_budget_is_not_shared_across_pages(runtime, monkeypatch):
    class ContinueAsNew(Exception):
        def __init__(self, value):
            self.value = value

    def continue_as_new(value):
        raise ContinueAsNew(value)

    execute = AsyncMock(side_effect=[(TENANT, "page-a"), (TENANT, "page-b"), None])
    monkeypatch.setattr("app.workflows.knowledge_lifecycle.workflow.patched", lambda _name: True)
    monkeypatch.setattr("app.workflows.knowledge_lifecycle.workflow.execute_activity", execute)
    monkeypatch.setattr("app.workflows.knowledge_lifecycle.workflow.continue_as_new", continue_as_new)
    value = {name: getattr(runtime.config, name) for name in (
        "max_attempts", "retry_initial_interval_seconds", "retry_max_interval_seconds",
        "activity_timeout_seconds", "workflow_timeout_seconds")}
    budgets = dict(value)
    for page in range(2):
        with pytest.raises(ContinueAsNew) as continuation:
            await KnowledgeReconcileWorkflow().run(value)
        assert execute.await_count == page + 1  # no second page in the same run
        value = continuation.value.value
        assert {key: value[key] for key in budgets} == budgets
    assert await KnowledgeReconcileWorkflow().run(value) == {"status": "complete"}
    assert [call.args[1] for call in execute.await_args_list] == [
        {"after_key": None}, {"after_key": (TENANT, "page-a")}, {"after_key": (TENANT, "page-b")},
    ]
    assert all(call.kwargs["schedule_to_close_timeout"] ==
               timedelta(seconds=budgets["workflow_timeout_seconds"])
               for call in execute.await_args_list)


@pytest.mark.asyncio
async def test_real_temporal_scan_continues_past_one_run_timeout_without_losing_cursor():
    """Opt-in isolated queue; four successful pages exceed one run's budget."""
    address = os.environ.get("NEWSAGENT_TIER_LIFECYCLE_TEST_TEMPORAL_ADDRESS")
    if not address:
        pytest.skip("set explicit Temporal test address to exercise continue-as-new")
    from temporalio import activity
    from temporalio.client import Client
    from temporalio.worker import Worker

    suffix = uuid4().hex
    queue, workflow_id = "tier-scan-test-" + suffix, "tier-scan-workflow-test-" + suffix
    observed = []

    @activity.defn(name="scan_knowledge_lifecycle_page")
    async def scan(value):
        observed.append(value.get("after_key"))
        await asyncio.sleep(0.8)
        return ["synthetic-tenant", f"page-{len(observed)}"] if len(observed) < 4 else None

    client = await Client.connect(address, namespace=os.environ.get(
        "NEWSAGENT_TIER_LIFECYCLE_TEST_TEMPORAL_NAMESPACE", "default"))
    handle = None
    try:
        async with Worker(client, task_queue=queue, workflows=[KnowledgeReconcileWorkflow], activities=[scan]):
            handle = await client.start_workflow(KnowledgeReconcileWorkflow.run, {
                "max_attempts": 1, "retry_initial_interval_seconds": 0.1,
                "retry_max_interval_seconds": 0.1, "activity_timeout_seconds": 1.5,
                "workflow_timeout_seconds": 2.0,
            }, id=workflow_id, task_queue=queue, run_timeout=timedelta(seconds=2))
            assert await asyncio.wait_for(handle.result(), timeout=30) == {"status": "complete"}
        assert observed == [None, ["synthetic-tenant", "page-1"],
                            ["synthetic-tenant", "page-2"], ["synthetic-tenant", "page-3"]]
    finally:
        if handle is not None:
            with suppress(Exception):
                await handle.terminate(reason="isolated lifecycle scan test cleanup")
