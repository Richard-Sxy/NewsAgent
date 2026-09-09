from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.data_loop.orchestrator import (
    DataLoopDecisionNotAllowedError,
    DataLoopOrchestrator,
)
from app.workflows.data_loop_contracts import (
    DataLoopHumanDecision,
    DataLoopRunRequest,
    DataLoopWorkflowSnapshot,
)


def request() -> DataLoopRunRequest:
    start = datetime(2026, 9, 8, tzinfo=timezone.utc)
    return DataLoopRunRequest(
        tenant_id="tenant-1",
        window_start=start,
        window_end=start + timedelta(days=1),
        dataset_name="daily-feedback",
        dataset_version="2026-09-08.v1",
        golden_dataset_id="11111111-1111-4111-8111-111111111111",
        high_risk_regression_dataset_id=(
            "22222222-2222-4222-8222-222222222222"
        ),
        candidate_id="candidate-1",
        previous_experiment_candidate_id=None,
        evaluation_policy_version="gate-v1",
        idempotency_key="data-loop-request-1",
    )


def orchestrator(client) -> DataLoopOrchestrator:
    return DataLoopOrchestrator(
        client,
        SimpleNamespace(temporal_data_loop_task_queue="data-loop-queue"),
    )


@pytest.mark.asyncio
async def test_start_uses_stable_non_sensitive_workflow_id() -> None:
    client = SimpleNamespace(start_workflow=AsyncMock())
    service = orchestrator(client)

    first = await service.start(request())
    second = service.workflow_id(request())

    assert first == second
    assert first.startswith("hot-news-data-loop-")
    assert "tenant-1" not in first
    assert client.start_workflow.await_args.kwargs["task_queue"] == "data-loop-queue"


@pytest.mark.asyncio
async def test_decision_requires_waiting_passed_gate() -> None:
    handle = SimpleNamespace(
        query=AsyncMock(
            return_value=DataLoopWorkflowSnapshot(
                tenant_id="tenant-1",
                phase="evaluation_failed",
                dataset_id="dataset-1",
                evaluation_run_id="evaluation-1",
                gate_passed=False,
                waiting_for_approval=False,
            )
        ),
        signal=AsyncMock(),
    )
    client = SimpleNamespace(get_workflow_handle=lambda _: handle)
    decision = DataLoopHumanDecision(
        action="approve",
        actor_id="operator-1",
        reason="looks good",
        idempotency_key="approval-request-1",
    )

    with pytest.raises(DataLoopDecisionNotAllowedError):
        await orchestrator(client).submit_decision(
            "workflow-1", decision, tenant_id="tenant-1"
        )

    handle.signal.assert_not_awaited()


@pytest.mark.asyncio
async def test_approved_gate_accepts_operator_signal() -> None:
    handle = SimpleNamespace(
        query=AsyncMock(
            return_value=DataLoopWorkflowSnapshot(
                tenant_id="tenant-1",
                phase="waiting_approval",
                dataset_id="dataset-1",
                evaluation_run_id="evaluation-1",
                gate_passed=True,
                waiting_for_approval=True,
            )
        ),
        signal=AsyncMock(),
    )
    client = SimpleNamespace(get_workflow_handle=lambda _: handle)
    decision = DataLoopHumanDecision(
        action="approve",
        actor_id="operator-1",
        reason="passed review",
        idempotency_key="approval-request-1",
    )

    await orchestrator(client).submit_decision(
        "workflow-1", decision, tenant_id="tenant-1"
    )

    handle.signal.assert_awaited_once()
