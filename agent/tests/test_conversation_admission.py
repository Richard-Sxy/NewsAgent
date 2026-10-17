import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

import app.conversation.admission as admission_module
import app.conversation.admission_workflows as workflow_module
from app.conversation.admission import (
    AdmissionActivities, AdmissionConflictError, AdmissionService, admission_result,
    evaluation_payload, writing_fingerprint,
)
from app.conversation.admission_contracts import (
    ADMISSION_BOOTSTRAP_LIMIT, ADMISSION_FINGERPRINT_MEMO, ADMISSION_TENANT_MEMO,
    AdmissionRequest, AdmissionTicket, TenantAdmissionState, request_admission_id, tenant_admission_id,
)
from app.conversation.admission_workflows import AdmissionRequestWorkflow, TenantAdmissionWorkflow, START_RETRY
from app.workflows.data_loop_contracts import DataLoopRunRequest
from tests.test_conversation_writing_commands import job_for
from app.schemas.job import CreateWritingJobRequest


TENANT = str(uuid4())


def job():
    row = job_for(uuid4(), uuid4(), CreateWritingJobRequest(topic="新闻选题", idempotency_key="test-writing-request"))
    row.tenant_id = uuid4()
    row.temporal_workflow_id = f"news-writing:{row.tenant_id}:{row.id}"
    return row


def request(index=1, *, tenant_id=TENANT, budget=2, queue_limit=100):
    row = job()
    row.tenant_id = tenant_id
    row.temporal_workflow_id = f"business-{index}"
    return AdmissionRequest(tenant_id, "writing", row.temporal_workflow_id,
        writing_fingerprint(row), {"job_id": str(row.id)}, budget, queue_limit)


def evaluation():
    now = datetime.now(timezone.utc)
    return DataLoopRunRequest(TENANT, now, now + timedelta(hours=1), "feedback", "1",
        str(uuid4()), str(uuid4()), str(uuid4()), None, "gate-v1", "evaluation-request-1")


def test_uninitialized_bootstrap_does_not_grant_or_fabricate_active_count():
    state = TenantAdmissionState(TENANT)
    snapshot = state.enqueue(request())
    assert snapshot["state"] == "queued" and snapshot["queue_position"] == 1
    assert snapshot["active_count"] is None and snapshot["bootstrap_complete"] is False
    assert state.grant() == []


def test_fifo_budget_queue_and_confirmed_terminal_release():
    state = TenantAdmissionState(TENANT, initialized=True)
    items = [request(index) for index in range(4)]
    for item in items:
        state.enqueue(item)
    assert [item.business_workflow_id for item in state.grant()] == ["business-0", "business-1"]
    assert state.snapshot(items[2].request_workflow_id)["queue_position"] == 1
    assert state.grant() == []
    state.complete("business-0")
    assert state.grant()[0].business_workflow_id == "business-2"
    assert state.snapshot()["active_count"] == 2


def test_duplicates_do_not_consume_queue_and_changed_payload_conflicts():
    state = TenantAdmissionState(TENANT, initialized=True)
    original = request()
    first = state.enqueue(original)
    assert state.enqueue(original) == first and len(state.pending) == 1
    with pytest.raises(ValueError, match="admission_request_conflict"):
        state.enqueue(replace(original, fingerprint="a" * 64))
    state.grant()
    assert state.enqueue(original)["state"] == "granted" and len(state.active) == 1


def test_queue_full_explicitly_rejects_and_does_not_enqueue():
    state = TenantAdmissionState(TENANT, queue_limit=1, initialized=True)
    state.enqueue(request(1, queue_limit=1))
    with pytest.raises(ValueError, match="admission_queue_full"):
        state.enqueue(request(2, queue_limit=1))
    assert len(state.pending) == 1


def test_tenant_and_frozen_policy_are_enforced():
    state = TenantAdmissionState(TENANT)
    with pytest.raises(ValueError, match="admission_tenant_mismatch"):
        state.enqueue(request(tenant_id=str(uuid4())))
    with pytest.raises(ValueError, match="admission_policy_conflict"):
        state.enqueue(request(budget=3))
    assert not state.pending


@pytest.mark.parametrize("field,value", [("budget", True), ("budget", 0), ("budget", 9),
    ("queue_limit", 0), ("queue_limit", 1001), ("policy_version", "unknown"),
    ("fingerprint", "invalid"), ("kind", "publish"), ("tenant_id", " "),
    ("payload", {"job_id": str(uuid4()), "sql": "untrusted"})])
def test_protocol_refuses_unbounded_or_unsupported_requests(field, value):
    with pytest.raises((ValueError, TypeError)):
        replace(request(), **{field: value}).validate()


@pytest.mark.asyncio
async def test_continue_as_new_state_is_serializable_and_preserves_active_queue():
    state = TenantAdmissionState(TENANT, initialized=True)
    items = [request(index) for index in range(4)]
    for item in items:
        state.enqueue(item)
    state.grant()
    converter = DataConverter.default
    encoded = await converter.encode([state])
    restored = (await converter.decode(encoded, [TenantAdmissionState]))[0]
    assert restored == state and restored.grant() == []
    restored.complete("business-0")
    assert restored.grant()[0].business_workflow_id == "business-2"


def test_imported_conversation_jobs_count_above_budget_and_replay_binds_without_double_slot():
    state = TenantAdmissionState(TENANT, initialized=True,
        active=[AdmissionTicket("", f"business-{index}", "bootstrap") for index in range(3)])
    replay = request(1)
    assert state.enqueue(replay)["state"] == "granted"
    assert len(state.active) == 3
    state.enqueue(request(4))
    assert not state.grant()
    state.complete("business-0")
    assert not state.grant()
    state.complete("business-2")
    assert state.grant()[0].business_workflow_id == "business-4"


def description(*, tenant_id=TENANT, fingerprint=None, status=WorkflowExecutionStatus.RUNNING):
    values = {ADMISSION_TENANT_MEMO: tenant_id, ADMISSION_FINGERPRINT_MEMO: fingerprint}
    return SimpleNamespace(status=status, memo_value=AsyncMock(side_effect=lambda key, default: values.get(key, default)))


@pytest.mark.asyncio
async def test_client_returns_real_queued_acceptance_without_business_wait():
    row = job()
    snapshot = {"state": "queued", "queue_position": 3, "active_count": 2, "budget": 2,
                "bootstrap_complete": True, "admission_accepted": True, "workflow_accepted": False}
    handle = SimpleNamespace(describe=AsyncMock(return_value=description(tenant_id=str(row.tenant_id),
        fingerprint=writing_fingerprint(row))), execute_update=AsyncMock(return_value=snapshot))
    client = SimpleNamespace(start_workflow=AsyncMock(return_value=handle))
    result = await AdmissionService(client=client).start_writing(row)
    assert result["workflow_id"] == row.temporal_workflow_id
    assert result["admission_accepted"] is True and result["workflow_accepted"] is False
    assert result["admission"]["state"] == "queued" and result["admission"]["queue_position"] == 3
    started = client.start_workflow.await_args
    assert started.args[0] == AdmissionRequestWorkflow.run
    assert started.kwargs["id"] == request_admission_id(str(row.tenant_id), row.temporal_workflow_id)
    assert started.args[1].payload == {"job_id": str(row.id)}


@pytest.mark.asyncio
async def test_evaluation_admission_preserves_business_workflow_id_and_bounded_payload():
    value = evaluation()
    from app.services.data_loop.orchestrator import DataLoopOrchestrator
    handle = SimpleNamespace(describe=AsyncMock(return_value=description(
        fingerprint=DataLoopOrchestrator.request_fingerprint(value))),
        execute_update=AsyncMock(return_value={"state": "queued", "admission_accepted": True}))
    client = SimpleNamespace(start_workflow=AsyncMock(return_value=handle))
    result = await AdmissionService(client=client).start_evaluation(value)
    assert result["workflow_id"] == DataLoopOrchestrator.workflow_id(value)
    assert client.start_workflow.await_args.args[1].payload == evaluation_payload(value)


@pytest.mark.asyncio
async def test_client_rejects_memo_tenant_or_fingerprint_conflicts():
    row = job()
    for metadata in (description(tenant_id="other", fingerprint=writing_fingerprint(row)),
                     description(tenant_id=str(row.tenant_id), fingerprint="b" * 64)):
        handle = SimpleNamespace(describe=AsyncMock(return_value=metadata), execute_update=AsyncMock())
        client = SimpleNamespace(start_workflow=AsyncMock(return_value=handle))
        with pytest.raises(AdmissionConflictError):
            await AdmissionService(client=client).start_writing(row)
        handle.execute_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_readonly_request_verification_describes_scoped_id_and_checks_memo_without_actor_query():
    value = request()
    handle = SimpleNamespace(describe=AsyncMock(return_value=description()), query=AsyncMock())
    client = SimpleNamespace(get_workflow_handle=lambda key: handle)
    port = AdmissionService(client=client)
    result = await port.verify_request(tenant_id=TENANT, business_workflow_id=value.business_workflow_id)
    assert result == {"request_workflow_id": value.request_workflow_id, "not_found": False}
    handle.query.assert_not_awaited()
    handle.describe.return_value = description(tenant_id="foreign")
    with pytest.raises(AdmissionConflictError, match="tenant_mismatch"):
        await port.verify_request(tenant_id=TENANT, business_workflow_id=value.business_workflow_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [RPCStatusCode.NOT_FOUND, RPCStatusCode.UNAVAILABLE])
async def test_readonly_request_verification_distinguishes_missing_and_temporal_unavailable(status):
    handle = SimpleNamespace(describe=AsyncMock(side_effect=RPCError("test-only", status, b"")), query=AsyncMock())
    port = AdmissionService(client=SimpleNamespace(get_workflow_handle=lambda key: handle))
    if status == RPCStatusCode.NOT_FOUND:
        assert await port.verify_request(tenant_id=TENANT, business_workflow_id="business") == {"not_found": True}
    else:
        with pytest.raises(RPCError):
            await port.verify_request(tenant_id=TENANT, business_workflow_id="business")
    handle.query.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_admission_replay_queries_snapshot_without_restart_or_update():
    row = job()
    handle = SimpleNamespace(describe=AsyncMock(return_value=description(tenant_id=str(row.tenant_id),
        fingerprint=writing_fingerprint(row), status=WorkflowExecutionStatus.COMPLETED)),
        execute_update=AsyncMock(), query=AsyncMock(return_value={"state": "terminal", "workflow_accepted": True,
                                                                "admission_accepted": True}))
    client = SimpleNamespace(start_workflow=AsyncMock(return_value=handle))
    result = await AdmissionService(client=client).start_writing(row)
    assert result["workflow_accepted"] is True and result["admission"]["state"] == "terminal"
    handle.execute_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_status_merges_real_queue_position_and_shared_active_count():
    original = request()
    request_handle = SimpleNamespace(describe=AsyncMock(return_value=description()),
        query=AsyncMock(return_value={"state": "queued", "queue_position": 9, "admission_accepted": True}))
    coordinator = SimpleNamespace(query=AsyncMock(return_value={"state": "queued", "queue_position": 2,
        "active_count": 2, "budget": 2, "bootstrap_complete": True}))
    client = SimpleNamespace(get_workflow_handle=lambda workflow_id: coordinator
        if workflow_id == tenant_admission_id(TENANT) else request_handle)
    result = await AdmissionService(client=client).status(tenant_id=TENANT,
                                                         business_workflow_id=original.business_workflow_id)
    assert result["admission"]["queue_position"] == 2 and result["admission"]["active_count"] == 2


def activities(*, database=None, client=None):
    settings = SimpleNamespace(temporal_task_queue="writing", temporal_data_loop_task_queue="evaluation")
    writing, evaluation_port = SimpleNamespace(start_job=AsyncMock()), SimpleNamespace(start=AsyncMock())
    return AdmissionActivities(client=client or SimpleNamespace(), database=database,
        settings=settings, writing=writing, evaluation=evaluation_port)


def database_with_rows(rows):
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    @asynccontextmanager
    async def session_scope():
        yield session
    return SimpleNamespace(session=session_scope), session


@pytest.mark.asyncio
async def test_bootstrap_only_imports_real_running_conversation_jobs_and_fails_closed():
    rows = [SimpleNamespace(temporal_workflow_id="one"), SimpleNamespace(temporal_workflow_id="two")]
    database, session = database_with_rows(rows)
    instance = activities(database=database)
    instance.describe_many = AsyncMock(return_value=[{"workflow_id": "one", "state": "running"},
                                                      {"workflow_id": "two", "state": "missing"}])
    assert await instance.bootstrap(TENANT) == ["one"]
    statement = session.execute.await_args.args[0]
    assert statement._limit_clause.value == ADMISSION_BOOTSTRAP_LIMIT + 1
    assert any(getattr(clause.right, "value", None) == "conversation-writing:%" for clause in statement._where_criteria)
    instance.describe_many.return_value = [{"workflow_id": "one", "state": "unknown"}]
    with pytest.raises(ApplicationError, match="admission_bootstrap_incomplete"):
        await instance.bootstrap(TENANT)


@pytest.mark.asyncio
async def test_bootstrap_over_limit_is_never_silently_truncated():
    database, _ = database_with_rows([SimpleNamespace()] * (ADMISSION_BOOTSTRAP_LIMIT + 1))
    instance = activities(database=database)
    instance.describe_many = AsyncMock()
    with pytest.raises(ApplicationError, match="admission_bootstrap_limit_exceeded"):
        await instance.bootstrap(TENANT)
    instance.describe_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_reads_authoritative_job_and_reuses_existing_orchestrator(monkeypatch):
    row = job()
    database, _ = database_with_rows([])
    instance = activities(database=database)
    instance._describe = AsyncMock(return_value={"state": "missing"})
    get = AsyncMock(return_value=row)
    monkeypatch.setattr(admission_module, "JobService", lambda: SimpleNamespace(get=get))
    value = AdmissionRequest(str(row.tenant_id), "writing", row.temporal_workflow_id,
        writing_fingerprint(row), {"job_id": str(row.id)})
    result = await instance.dispatch(value)
    assert result["state"] == "running"
    instance.writing.start_job.assert_awaited_once_with(row)
    assert str(get.await_args.kwargs["tenant_id"]) == str(row.tenant_id)
    with pytest.raises(ApplicationError, match="admission_request_conflict"):
        await instance.dispatch(replace(value, fingerprint="f" * 64))
    assert instance.writing.start_job.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "terminal"])
async def test_dispatch_retry_does_not_restart_existing_business_workflow(monkeypatch, state):
    row = job()
    database, _ = database_with_rows([])
    instance = activities(database=database)
    instance._describe = AsyncMock(return_value={"state": state})
    monkeypatch.setattr(admission_module, "JobService", lambda: SimpleNamespace(get=AsyncMock(return_value=row)))
    value = AdmissionRequest(str(row.tenant_id), "writing", row.temporal_workflow_id,
        writing_fingerprint(row), {"job_id": str(row.id)})
    assert (await instance.dispatch(value))["state"] == state
    instance.writing.start_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_evaluation_dispatch_reuses_existing_service_and_enforces_fingerprint():
    from app.services.data_loop.orchestrator import DataLoopOrchestrator
    value = evaluation()
    instance = activities()
    admission = AdmissionRequest(TENANT, "evaluation", DataLoopOrchestrator.workflow_id(value),
        DataLoopOrchestrator.request_fingerprint(value), evaluation_payload(value))
    await instance.dispatch(admission)
    instance.evaluation.start.assert_awaited_once_with(value)
    with pytest.raises(ApplicationError):
        await instance.dispatch(replace(admission, tenant_id=str(uuid4())))


@pytest.mark.asyncio
async def test_request_workflow_waits_for_durable_grant_then_only_dispatch_acceptance(monkeypatch):
    instance = AdmissionRequestWorkflow()
    value = request()
    calls = []
    async def execute(name, payload, **kwargs):
        calls.append((name, kwargs))
        if name.endswith("enqueue"):
            return {"state": "queued", "queue_position": 1, "active_count": 2, "budget": 2}
        instance.observed({"state": "terminal", "business_status": "completed"})
        return {"state": "running"}  # late accepted response cannot overwrite terminal observation
    async def wait(predicate, **kwargs):
        if not predicate():
            assert instance._snapshot["admission_accepted"] is True
            instance.granted({"state": "granted", "queue_position": None, "active_count": 2, "budget": 2})
        assert predicate()
    monkeypatch.setattr(workflow_module.workflow, "execute_activity", execute)
    monkeypatch.setattr(workflow_module.workflow, "wait_condition", wait)
    monkeypatch.setattr(workflow_module.workflow, "all_handlers_finished", lambda: True)
    result = await instance.run(value)
    assert result["state"] == "terminal" and result["workflow_accepted"] is True
    assert [name for name, _ in calls] == ["conversation_admission_enqueue", "conversation_admission_dispatch"]
    assert calls[1][1]["retry_policy"].maximum_attempts == START_RETRY.maximum_attempts == 2


def test_unknown_business_observation_cannot_release_or_fake_start():
    instance = AdmissionRequestWorkflow()
    instance.request = request()
    instance.observed({"state": "unknown"})
    assert instance._terminal is False and instance._snapshot["workflow_accepted"] is False
    with pytest.raises(ValueError, match="admission_tenant_mismatch"):
        instance.snapshot("other")


@pytest.mark.asyncio
async def test_failed_start_transitions_to_readonly_diagnosis_without_another_dispatch(monkeypatch):
    instance = AdmissionRequestWorkflow()
    value = request()
    calls = []
    async def execute(name, payload, **kwargs):
        calls.append(name)
        if name.endswith("enqueue"):
            return {"state": "granted", "active_count": 1, "budget": 2}
        raise ConnectionError("accepted response lost after the permitted Activity retry")
    async def wait(predicate, **kwargs):
        if not predicate():
            assert instance._snapshot["state"] == "unknown"
            assert instance._snapshot["workflow_accepted"] is False
            instance.observed({"state": "terminal", "business_status": "completed"})
        assert predicate()
    monkeypatch.setattr(workflow_module.workflow, "execute_activity", execute)
    monkeypatch.setattr(workflow_module.workflow, "wait_condition", wait)
    monkeypatch.setattr(workflow_module.workflow, "all_handlers_finished", lambda: True)
    result = await instance.run(value)
    assert calls == ["conversation_admission_enqueue", "conversation_admission_dispatch"]
    assert result["state"] == "terminal" and result["workflow_accepted"] is True


@pytest.mark.asyncio
async def test_coordinator_keeps_slots_for_missing_or_unknown_business_state(monkeypatch):
    class StopProbe(BaseException):
        pass
    state = TenantAdmissionState(TENANT, initialized=True)
    for index in range(3):
        state.enqueue(request(index))
    instance = TenantAdmissionWorkflow()
    async def execute(name, payload, **kwargs):
        return [{"workflow_id": "business-0", "state": "missing"},
                {"workflow_id": "business-1", "state": "unknown"}]
    async def wait(predicate, **kwargs):
        assert not predicate()
        raise StopProbe()
    monkeypatch.setattr(workflow_module.workflow, "execute_activity", execute)
    monkeypatch.setattr(workflow_module.workflow, "wait_condition", wait)
    instance._signal = AsyncMock()
    with pytest.raises(StopProbe):
        await instance.run(state)
    assert len(state.active) == 2 and state.pending[0].business_workflow_id == "business-2"


def test_workflow_and_activity_definitions_are_worker_registerable():
    assert TenantAdmissionWorkflow.__temporal_workflow_definition.name == "conversation-tenant-admission-v1"
    assert AdmissionRequestWorkflow.__temporal_workflow_definition.name == "conversation-admission-request-v1"
    assert "enqueue" in TenantAdmissionWorkflow.__temporal_workflow_definition.updates
    assert "accepted" in AdmissionRequestWorkflow.__temporal_workflow_definition.updates
    assert len(activities().registered) == 4
