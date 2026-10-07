from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

import pytest
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

from app.domain.job_scenario import JobScenario
from app.services.orchestrator import (
    OrchestratorService,
    WorkflowExecutionNotFoundError,
)
from app.workflows.contracts import HumanDecision, WorkflowSnapshot


def service_and_client():
    client = Mock()
    client.start_workflow = AsyncMock()
    client.get_workflow_handle = Mock()
    settings = SimpleNamespace(temporal_task_queue="news-writing")
    return OrchestratorService(client, settings), client


@pytest.mark.asyncio
async def test_start_workflow_uses_stable_idempotency_policies() -> None:
    service, client = service_and_client()
    job = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        topic="主题",
        scenario=JobScenario.RESEARCH_PACKAGE,
        requirements={"style": "news"},
        temporal_workflow_id="news-writing:tenant:job",
    )

    await service.start_job(job)

    arguments = client.start_workflow.await_args
    assert arguments.kwargs["id"] == job.temporal_workflow_id
    assert arguments.kwargs["id_conflict_policy"] == WorkflowIDConflictPolicy.USE_EXISTING
    assert arguments.kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
    assert arguments.args[1].topic == "主题"
    assert arguments.args[1].scenario == "research_package"


@pytest.mark.asyncio
async def test_query_and_signal_use_job_workflow_handle() -> None:
    service, client = service_and_client()
    handle = Mock()
    handle.query = AsyncMock(
        return_value=WorkflowSnapshot("research", 1, 0, "research", None)
    )
    handle.signal = AsyncMock()
    client.get_workflow_handle.return_value = handle

    snapshot = await service.get_progress("workflow-1")
    decision = HumanDecision(gate="research", action="approve")
    await service.submit_human_decision("workflow-1", decision)

    assert snapshot.waiting_gate == "research"
    client.get_workflow_handle.assert_called_with("workflow-1")
    assert handle.signal.await_args.args[1] == decision


@pytest.mark.asyncio
async def test_missing_workflow_rpc_error_is_classified() -> None:
    service, client = service_and_client()
    handle = Mock()
    handle.query = AsyncMock(
        side_effect=RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b"")
    )
    client.get_workflow_handle.return_value = handle

    with pytest.raises(WorkflowExecutionNotFoundError):
        await service.get_progress("missing-workflow")
