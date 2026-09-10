from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.services.data_loop.orchestrator import (
    DATA_LOOP_REQUEST_FINGERPRINT_MEMO_KEY,
    DataLoopActivationRecoveryNotAllowedError,
    DataLoopDecisionNotAllowedError,
    DataLoopOrchestrator,
    DataLoopStartConflictError,
)
from app.workflows.data_loop_contracts import (
    DataLoopActivationRecoveryContext,
    DataLoopActivationRecoveryRequest,
    DataLoopDecisionUpdateResult,
    DataLoopHumanDecision,
    DataLoopRunRequest,
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


def described_handle(fingerprint: str):
    description = SimpleNamespace(
        memo_value=AsyncMock(return_value=fingerprint),
    )
    return SimpleNamespace(describe=AsyncMock(return_value=description))


@pytest.mark.asyncio
async def test_start_uses_stable_non_sensitive_workflow_id() -> None:
    expected_fingerprint = DataLoopOrchestrator.request_fingerprint(request())
    handle = described_handle(expected_fingerprint)
    client = SimpleNamespace(start_workflow=AsyncMock(return_value=handle))
    service = orchestrator(client)

    first = await service.start(request())
    second = service.workflow_id(request())

    assert first == second
    assert first.startswith("hot-news-data-loop-")
    assert "tenant-1" not in first
    assert client.start_workflow.await_args.kwargs["task_queue"] == "data-loop-queue"
    assert client.start_workflow.await_args.kwargs["memo"] == {
        DATA_LOOP_REQUEST_FINGERPRINT_MEMO_KEY: expected_fingerprint,
    }
    handle.describe.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_rejects_same_key_with_different_memo_fingerprint() -> None:
    client = SimpleNamespace(
        start_workflow=AsyncMock(return_value=described_handle("different"))
    )

    with pytest.raises(DataLoopStartConflictError, match="different content"):
        await orchestrator(client).start(request())


@pytest.mark.asyncio
async def test_closed_same_content_start_is_an_idempotent_replay() -> None:
    value = request()
    fingerprint = DataLoopOrchestrator.request_fingerprint(value)
    handle = described_handle(fingerprint)
    client = SimpleNamespace(
        start_workflow=AsyncMock(
            side_effect=WorkflowAlreadyStartedError(
                DataLoopOrchestrator.workflow_id(value),
                "hot-news-data-loop-v1",
                run_id="run-1",
            )
        ),
        get_workflow_handle=lambda *_args, **_kwargs: handle,
    )

    workflow_id = await orchestrator(client).start(value)

    assert workflow_id == DataLoopOrchestrator.workflow_id(value)
    assert handle.describe.await_count == 1


@pytest.mark.asyncio
async def test_closed_different_content_start_is_a_conflict() -> None:
    value = request()
    handle = described_handle("different")
    client = SimpleNamespace(
        start_workflow=AsyncMock(
            side_effect=WorkflowAlreadyStartedError(
                DataLoopOrchestrator.workflow_id(value),
                "hot-news-data-loop-v1",
                run_id="run-1",
            )
        ),
        get_workflow_handle=lambda *_args, **_kwargs: handle,
    )

    with pytest.raises(DataLoopStartConflictError, match="different content"):
        await orchestrator(client).start(value)


@pytest.mark.asyncio
async def test_decision_requires_waiting_passed_gate() -> None:
    handle = SimpleNamespace(
        execute_update=AsyncMock(
            side_effect=WorkflowUpdateFailedError(
                ValueError(
                    "Data Loop workflow is not waiting at a passed "
                    "evaluation gate"
                )
            )
        ),
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

    handle.execute_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_approved_gate_accepts_operator_signal() -> None:
    handle = SimpleNamespace(
        execute_update=AsyncMock(
            return_value=DataLoopDecisionUpdateResult(
                action="approve",
                idempotency_key="approval-request-1",
                created=True,
            )
        ),
    )
    client = SimpleNamespace(get_workflow_handle=lambda _: handle)
    decision = DataLoopHumanDecision(
        action="approve",
        actor_id="operator-1",
        reason="passed review",
        idempotency_key="approval-request-1",
    )

    result = await orchestrator(client).submit_decision(
        "workflow-1", decision, tenant_id="tenant-1"
    )

    assert result.created is True
    handle.execute_update.assert_awaited_once()
    command = handle.execute_update.await_args.args[1]
    assert command.tenant_id == "tenant-1"
    assert command.decision == decision
    assert handle.execute_update.await_args.kwargs["id"] == (
        DataLoopOrchestrator.decision_update_id("tenant-1", decision)
    )


@pytest.mark.asyncio
async def test_decision_update_rejection_maps_to_control_plane_conflict() -> None:
    handle = SimpleNamespace(
        execute_update=AsyncMock(
            side_effect=WorkflowUpdateFailedError(
                ValueError("a promotion decision has already been submitted")
            )
        )
    )
    client = SimpleNamespace(get_workflow_handle=lambda _: handle)
    decision = DataLoopHumanDecision(
        action="approve",
        actor_id="operator-1",
        reason="passed review",
        idempotency_key="approval-request-1",
    )

    with pytest.raises(DataLoopDecisionNotAllowedError, match="already"):
        await orchestrator(client).submit_decision(
            "workflow-1", decision, tenant_id="tenant-1"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_phase",
    ["record_promotion_decision", "activate_bundle"],
)
async def test_activation_recovery_uses_approved_source_context(
    source_phase: str,
) -> None:
    decision = DataLoopHumanDecision(
        action="approve",
        actor_id="operator-1",
        reason="passed review",
        idempotency_key="approval-request-1",
    )
    context = DataLoopActivationRecoveryContext(
        tenant_id="tenant-1",
        phase=source_phase,
        candidate_id="candidate-1",
        evaluation_run_id="evaluation-1",
        gate_passed=True,
        decision=decision,
    )
    source_handle = SimpleNamespace(query=AsyncMock(return_value=context))
    recovery_request = DataLoopActivationRecoveryRequest(
        tenant_id="tenant-1",
        source_workflow_id="workflow-1",
        candidate_id="candidate-1",
        evaluation_run_id="evaluation-1",
        approval_decision=decision,
        requested_by="operator-2",
        idempotency_key="recovery-request-1",
    )
    recovery_handle = described_handle(
        DataLoopOrchestrator.activation_recovery_fingerprint(recovery_request)
    )
    client = SimpleNamespace(
        get_workflow_handle=lambda _: source_handle,
        start_workflow=AsyncMock(return_value=recovery_handle),
    )

    workflow_id = await orchestrator(client).recover_activation(
        "workflow-1",
        tenant_id="tenant-1",
        requested_by="operator-2",
        idempotency_key="recovery-request-1",
    )

    assert workflow_id.startswith("hot-news-data-loop-activation-recovery-")
    started = client.start_workflow.await_args.args[1]
    assert started.approval_decision == decision
    assert started.evaluation_run_id == "evaluation-1"


@pytest.mark.asyncio
async def test_activation_recovery_rejects_source_without_approval() -> None:
    source_handle = SimpleNamespace(
        query=AsyncMock(
            return_value=DataLoopActivationRecoveryContext(
                tenant_id="tenant-1",
                phase="evaluation_failed",
                candidate_id="candidate-1",
                evaluation_run_id="evaluation-1",
                gate_passed=False,
                decision=None,
            )
        )
    )
    client = SimpleNamespace(get_workflow_handle=lambda _: source_handle)

    with pytest.raises(DataLoopActivationRecoveryNotAllowedError):
        await orchestrator(client).recover_activation(
            "workflow-1",
            tenant_id="tenant-1",
            requested_by="operator-2",
            idempotency_key="recovery-request-1",
        )


@pytest.mark.asyncio
async def test_activation_recovery_cannot_override_completed_activation() -> None:
    decision = DataLoopHumanDecision(
        action="approve",
        actor_id="operator-1",
        reason="passed review",
        idempotency_key="approval-request-1",
    )
    source_handle = SimpleNamespace(
        query=AsyncMock(
            return_value=DataLoopActivationRecoveryContext(
                tenant_id="tenant-1",
                phase="activated",
                candidate_id="candidate-1",
                evaluation_run_id="evaluation-1",
                gate_passed=True,
                decision=decision,
            )
        )
    )
    client = SimpleNamespace(
        get_workflow_handle=lambda _: source_handle,
        start_workflow=AsyncMock(),
    )

    with pytest.raises(DataLoopActivationRecoveryNotAllowedError):
        await orchestrator(client).recover_activation(
            "workflow-1",
            tenant_id="tenant-1",
            requested_by="operator-2",
            idempotency_key="recovery-request-1",
        )

    client.start_workflow.assert_not_awaited()
