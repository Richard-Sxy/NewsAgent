"""Admission is optional and changes only conversation async launch ports."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from temporalio.service import RPCError, RPCStatusCode

import app.worker as worker_module
from app.config import Settings
from app.conversation.admission_contracts import AdmissionConflictError, request_admission_id
from app.conversation.admission_workflows import ADMISSION_WORKFLOWS
from app.conversation.writing import ConversationWritingConflict, ConversationWritingReader
from app.workflows.data_loop_contracts import DataLoopWorkflowSnapshot
from app.services.production_bundle import ProductionBundleRuleViolation
from tests.test_conversation_writing_commands import runtime, start
from tests.test_conversation_tools_writing import Jobs, Database
from tests.test_conversation_dataloop_hook import harness, TENANT


def queue_result(workflow_id, *, rejected=False):
    return {"workflow_id": workflow_id, "workflow_accepted": False, "admission_accepted": not rejected,
        "evaluation_policy_version": "hot-news-gate-v1",
        "admission": {"state": "rejected" if rejected else "queued", "queue_position": None if rejected else 1,
            "active_count": 2, "budget": 2, "bootstrap_complete": True,
            "request_workflow_id": "conversation-admission-request-" + "a" * 64,
            "error_code": "admission_queue_full" if rejected else None}}


def test_feature_defaults_do_not_change_existing_deployments():
    assert Settings.model_fields["conversation_async_admission_enabled"].default is False
    assert Settings.model_fields["conversation_async_admission_budget"].default == 2
    assert Settings.model_fields["conversation_async_admission_queue_limit"].default == 100


@pytest.mark.asyncio
async def test_writing_start_returns_durable_queue_ack_without_claiming_workflow_start():
    commands, database, orchestrator, _ = runtime()
    commands.admission = SimpleNamespace(start_writing=AsyncMock(side_effect=lambda row: queue_result(row.temporal_workflow_id)))
    bound = AsyncMock()
    result = await start(commands, on_bound=bound)
    assert result["workflow_accepted"] is False and result["admission_accepted"] is True
    assert result["admission"]["queue_position"] == 1 and result["job"]["admission"] == result["admission"]
    assert result["receipt"]["status"] == "landed" and len(database.rows) == 1
    assert bound.await_count == 2 and bound.await_args.args[0] == result
    orchestrator.start_job.assert_not_awaited()
    commands.admission.start_writing.assert_awaited_once()


@pytest.mark.asyncio
async def test_writing_queue_full_retains_bound_job_but_does_not_succeed_or_retry():
    commands, database, orchestrator, _ = runtime()
    commands.admission = SimpleNamespace(start_writing=AsyncMock(side_effect=lambda row: queue_result(row.temporal_workflow_id, rejected=True)))
    bound = AsyncMock()
    with pytest.raises(ConversationWritingConflict, match="admission_queue_full"):
        await start(commands, on_bound=bound)
    assert len(database.rows) == 1 and bound.await_args.args[0]["admission"]["state"] == "rejected"
    assert bound.await_args.args[0]["admission_accepted"] is False
    assert commands.admission.start_writing.await_count == 1
    orchestrator.start_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_writing_status_and_list_attach_admission_without_changing_job_snapshot():
    jobs = Jobs()
    queue = queue_result(jobs.row.temporal_workflow_id)
    admission = SimpleNamespace(status=AsyncMock(return_value=queue))
    reader = ConversationWritingReader(database=Database(), jobs=jobs, admission=admission)
    result = await reader.job_status(tenant_id=str(jobs.row.tenant_id), job_id=str(jobs.row.id))
    assert result["admission"] == queue["admission"] and result["job"]["admission"] == queue["admission"]
    assert result["job"]["version"] == "7" and result["workflow_accepted"] is False
    listed = await reader.list_jobs(tenant_id=str(jobs.row.tenant_id), limit=5)
    assert listed["items"][0]["admission"] == queue["admission"]
    assert admission.status.await_args.kwargs == {
        "tenant_id": str(jobs.row.tenant_id), "business_workflow_id": jobs.row.temporal_workflow_id}


@pytest.mark.asyncio
async def test_page_created_job_missing_admission_keeps_original_read_projection():
    jobs = Jobs()
    reader = ConversationWritingReader(database=Database(), jobs=jobs,
        admission=SimpleNamespace(status=AsyncMock(return_value={"not_found": True})))
    result = await reader.job_status(tenant_id=str(jobs.row.tenant_id), job_id=str(jobs.row.id))
    assert "admission" not in result and "admission" not in result["job"]


@pytest.mark.asyncio
async def test_admission_outage_degrades_status_without_losing_authoritative_job():
    jobs = Jobs()
    reader = ConversationWritingReader(database=Database(), jobs=jobs,
        admission=SimpleNamespace(status=AsyncMock(side_effect=ConnectionError("Temporal unavailable"))))
    result = await reader.job_status(tenant_id=str(jobs.row.tenant_id), job_id=str(jobs.row.id))
    assert result["job"]["id"] == str(jobs.row.id)
    assert result["admission"]["state"] == "unknown" and result["admission"]["active_count"] is None
    assert result["admission"]["bootstrap_complete"] is None
    assert result["admission"]["request_workflow_id"] == request_admission_id(str(jobs.row.tenant_id), jobs.row.temporal_workflow_id)
    assert result["workflow_accepted"] is None and result["admission_accepted"] is None


@pytest.mark.asyncio
async def test_evaluation_queued_ack_is_unpersisted_business_receipt_and_status_skips_business_query(monkeypatch):
    hooks, identity, _, _, context, _, _, _, _, client, _ = harness(monkeypatch)
    hooks.admission = SimpleNamespace(
        start_evaluation=AsyncMock(side_effect=lambda request: queue_result(hooks.orchestrator.workflow_id(request))),
        status=AsyncMock())
    proposal = {"candidate_id": context["candidate_id"]}
    prepared = await hooks.prepare_evaluation(tenant_id=TENANT, proposal=proposal)
    bound = AsyncMock()
    result = await hooks.start_evaluation(**identity, proposal=proposal,
        prepared_context=prepared["prepared_context"], on_bound=bound)
    assert result["workflow_accepted"] is False and result["admission_accepted"] is True
    assert result["run"]["phase"] == "queued" and result["receipt"]["status"] == "not_landed"
    assert result["receipt"]["entity_id"] is None and result["receipt"]["deep_link"] is None
    assert result["evaluation_policy_version"] == "hot-news-gate-v1"
    assert bound.await_count == 2 and bound.await_args.args[0] == result
    client.start_workflow.assert_not_awaited()
    hooks.admission.status.return_value = queue_result(result["workflow_id"])
    queried = await hooks.status(tenant_id=TENANT, proposal={"workflow_id": result["workflow_id"]})
    assert queried["run"]["phase"] == "queued" and queried["receipt"]["status"] == "not_landed"
    assert queried["evaluation_policy_version"] == "hot-news-gate-v1"
    client.start_workflow.assert_not_awaited()


@pytest.mark.asyncio
async def test_evaluation_queue_full_never_claims_success_or_business_entity(monkeypatch):
    hooks, identity, _, _, context, _, _, _, _, client, _ = harness(monkeypatch)
    hooks.admission = SimpleNamespace(start_evaluation=AsyncMock(side_effect=lambda request:
        queue_result(hooks.orchestrator.workflow_id(request), rejected=True)))
    proposal = {"candidate_id": context["candidate_id"]}
    prepared = await hooks.prepare_evaluation(tenant_id=TENANT, proposal=proposal)
    bound = AsyncMock()
    with pytest.raises(ProductionBundleRuleViolation, match="admission_queue_full"):
        await hooks.start_evaluation(**identity, proposal=proposal,
            prepared_context=prepared["prepared_context"], on_bound=bound)
    assert bound.await_args.args[0]["receipt"]["status"] == "not_landed"
    assert bound.await_args.args[0]["admission"]["state"] == "rejected"
    client.start_workflow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("business_error", [RPCStatusCode.NOT_FOUND, RPCStatusCode.UNAVAILABLE])
async def test_evaluation_unreadable_actor_returns_unknown_gray_receipt_only_after_owned_descriptor(monkeypatch, business_error):
    hooks, *_ = harness(monkeypatch)
    workflow_id = "hot-news-data-loop-" + "b" * 64
    hooks.admission = SimpleNamespace(status=AsyncMock(side_effect=RPCError("unreadable actor", RPCStatusCode.UNAVAILABLE, b"")),
        verify_request=AsyncMock(return_value={"not_found": False}))
    hooks.orchestrator = SimpleNamespace(get_snapshot=AsyncMock(side_effect=RPCError("business unavailable", business_error, b"")))
    result = await hooks.status(tenant_id=TENANT, proposal={"workflow_id": workflow_id})
    assert result["admission"]["state"] == "unknown" and result["admission"]["bootstrap_complete"] is None
    assert result["admission"]["request_workflow_id"] == request_admission_id(TENANT, workflow_id)
    assert result["admission"]["active_count"] is None and result["admission"]["budget"] is None
    assert result["workflow_accepted"] is None and result["admission_accepted"] is None
    assert result["run"]["phase"] == "unknown"
    assert result["receipt"]["status"] == "not_landed" and result["receipt"]["entity_id"] is None
    assert result["receipt"]["deep_link"] is None
    hooks.admission.verify_request.assert_awaited_once_with(tenant_id=TENANT, business_workflow_id=workflow_id)


@pytest.mark.asyncio
async def test_real_owned_business_snapshot_remains_landed_during_admission_query_outage(monkeypatch):
    hooks, *_ = harness(monkeypatch)
    workflow_id = "hot-news-data-loop-" + "b" * 64
    hooks.admission = SimpleNamespace(status=AsyncMock(side_effect=ConnectionError("unavailable")),
        verify_request=AsyncMock(side_effect=RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")))
    hooks.orchestrator = SimpleNamespace(get_snapshot=AsyncMock(return_value=DataLoopWorkflowSnapshot(
        TENANT, "evaluation_failed", None, str(uuid4()), False, False)))
    result = await hooks.status(tenant_id=TENANT, proposal={"workflow_id": workflow_id})
    assert result["workflow_accepted"] is True and result["receipt"]["status"] == "landed"
    assert result["receipt"]["entity_id"] == workflow_id
    assert result["admission"]["state"] == "unknown"


@pytest.mark.parametrize("verification,business,expected", [
    ("owned", "missing", 200), ("owned", "unavailable", 200),
    ("missing", "missing", 404), ("unavailable", "missing", 503),
    ("unavailable", "unavailable", 503), ("mismatch", "missing", 404),
    ("owned", "foreign", 404),
])
def test_evaluation_receipt_http_preserves_ownership_404_and_safe_outage_mapping(monkeypatch, verification, business, expected):
    from tests.test_conversation_capability_integration import integrated_tools, receipt_client
    hooks, *_ = harness(monkeypatch)
    workflow_id = "hot-news-data-loop-" + "c" * 64
    verified = AsyncMock(return_value={"not_found": verification == "missing"})
    if verification == "unavailable":
        verified.side_effect = RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")
    elif verification == "mismatch":
        verified.side_effect = AdmissionConflictError("admission_tenant_mismatch")
    hooks.admission = SimpleNamespace(status=AsyncMock(side_effect=RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")),
        verify_request=verified)
    business_port = AsyncMock(side_effect=RPCError("unavailable", RPCStatusCode.NOT_FOUND if business == "missing" else RPCStatusCode.UNAVAILABLE, b""))
    if business == "foreign":
        business_port.side_effect = None
        business_port.return_value = DataLoopWorkflowSnapshot(str(uuid4()), "running", None, None, None, False)
    hooks.orchestrator = SimpleNamespace(get_snapshot=business_port)
    tools = integrated_tools(dataloop=hooks)
    client, headers = receipt_client(tools)
    with client:
        response = client.get(f"/api/v1/conversations/receipts/evaluation-runs/{workflow_id}",
                              headers={**headers, "X-Data-Loop-Roles": "data-loop:read"})
    assert response.status_code == expected
    if expected == 200:
        assert response.json()["receipt"]["status"] == "not_landed"
        assert response.json()["admission"]["request_workflow_id"] == request_admission_id(TENANT, workflow_id)
    if verification == "mismatch":
        business_port.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_worker_registration_changes_only_when_feature_explicitly_enabled(monkeypatch, enabled):
    client = SimpleNamespace()
    monkeypatch.setattr(worker_module.Client, "connect", AsyncMock(return_value=client))
    observed = []
    def worker(*args, **kwargs):
        observed.append(kwargs)
        return SimpleNamespace(run=AsyncMock())
    monkeypatch.setattr(worker_module, "Worker", worker)
    settings = SimpleNamespace(temporal_address="unused", temporal_namespace="test",
        temporal_task_queue="writing", conversation_async_admission_enabled=enabled)
    extras = SimpleNamespace(registered=["admission-test-activity"])
    await worker_module.run_worker(handler=AsyncMock(), state_handler=AsyncMock(), settings=settings,
                                   admission_activities=extras)
    assert len(observed[0]["workflows"]) == (3 if enabled else 1)
    assert len(observed[0]["activities"]) == (3 if enabled else 2)
    if enabled:
        assert observed[0]["workflows"][1:] == ADMISSION_WORKFLOWS
