import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from temporalio.exceptions import ApplicationError

from app.activities.data_loop import DataLoopActivities
from app.workflows.data_loop import (
    DATA_LOOP_ACTIVITY_HEARTBEAT_TIMEOUT,
    DATA_LOOP_ACTIVATION_RECOVERY_WORKFLOW_NAME,
    DATA_LOOP_APPROVAL_TIMEOUT,
    DATA_LOOP_DATASET_ANALYSIS_PATCH,
    DATA_LOOP_WORKFLOW_NAME,
    HotNewsDataLoopActivationRecoveryWorkflow,
    HotNewsDataLoopWorkflow,
)
from app.workflows.data_loop_contracts import (
    DataLoopActivationRecoveryRequest,
    DataLoopDecisionUpdate,
    DataLoopHumanDecision,
    DataLoopRunRequest,
    DataLoopStepCommand,
    DataLoopStepOutcome,
    DataLoopWorkflowSnapshot,
)


@pytest.fixture(autouse=True)
def legacy_history_patch(monkeypatch):
    """Pure Workflow tests default to a pre-patch history."""
    patched = lambda _patch_id: False
    monkeypatch.setattr("app.workflows.data_loop.workflow.patched", patched)


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
        idempotency_key="data-loop-2026-09-08-v1",
    )


def outcome(step_key: str, **values) -> DataLoopStepOutcome:
    return DataLoopStepOutcome(
        step_key=step_key,
        status="completed",
        values=values,
    )


def test_temporal_definitions_are_stable() -> None:
    assert (
        HotNewsDataLoopWorkflow.__temporal_workflow_definition.name
        == DATA_LOOP_WORKFLOW_NAME
    )
    assert (
        HotNewsDataLoopActivationRecoveryWorkflow.__temporal_workflow_definition.name
        == DATA_LOOP_ACTIVATION_RECOVERY_WORKFLOW_NAME
    )
    assert (
        "submit_promotion_decision"
        in HotNewsDataLoopWorkflow.__temporal_workflow_definition.updates
    )
    assert (
        DataLoopActivities.run_data_loop_step.__temporal_activity_definition.name
        == "run_data_loop_step"
    )
    assert DATA_LOOP_APPROVAL_TIMEOUT == timedelta(hours=48)


@pytest.mark.asyncio
async def test_activity_delegates_and_classifies_non_retryable_error() -> None:
    command = DataLoopStepCommand(
        tenant_id="tenant-1",
        run_idempotency_key="run-1",
        step_type="freeze_dataset",
        step_key="freeze-v1",
    )
    handler = AsyncMock()
    handler.execute.side_effect = ValueError("invalid dataset")

    with pytest.raises(ApplicationError) as captured:
        await DataLoopActivities(handler).run_data_loop_step(command)

    assert captured.value.non_retryable is True
    assert captured.value.type == "ValueError"


@pytest.mark.asyncio
async def test_gate_failure_never_waits_for_or_activates_candidate() -> None:
    workflow = HotNewsDataLoopWorkflow()
    workflow._execute = AsyncMock(
        side_effect=[
            outcome("freeze-dataset-v1", dataset_id="dataset-1"),
            outcome("attribute-errors-v1", attribution_count=2),
            outcome(
                "evaluate-candidate-v1",
                evaluation_run_id="evaluation-1",
                gate_passed=False,
                reason="critical regression",
            ),
        ]
    )

    result = await workflow.run(request())

    assert result.status == "evaluation_failed"
    assert result.production_bundle_id is None
    assert workflow.snapshot().waiting_for_approval is False
    assert workflow._execute.await_count == 3


@pytest.mark.asyncio
async def test_pre_patch_history_keeps_original_sequence_and_payloads() -> None:
    workflow = HotNewsDataLoopWorkflow()
    workflow._execute = AsyncMock(side_effect=[
        outcome("freeze-dataset-v1", dataset_id="dataset-1"),
        outcome("attribute-errors-v1", artifact_uri="s3://bucket/attribution.json",
                artifact_sha256="a" * 64),
        outcome("evaluate-candidate-v1", evaluation_run_id="evaluation-1",
                gate_passed=False),
    ])
    command = request()
    await workflow.run(command)

    calls = workflow._execute.await_args_list
    assert [call.kwargs["step_type"] for call in calls] == [
        "freeze_dataset", "attribute_errors", "evaluate_candidate",
    ]
    assert calls[1].kwargs["inputs"] == {"dataset_id": "dataset-1"}
    assert calls[2].kwargs["inputs"] == {
        "dataset_id": "dataset-1",
        "golden_dataset_id": command.golden_dataset_id,
        "high_risk_regression_dataset_id": command.high_risk_regression_dataset_id,
        "candidate_id": command.candidate_id,
        "previous_experiment_candidate_id": None,
        "evaluation_policy_version": command.evaluation_policy_version,
        "attribution_status": "completed",
        "attribution_artifact_uri": "s3://bucket/attribution.json",
        "attribution_artifact_sha256": "a" * 64,
    }
    snapshot = workflow.snapshot()
    assert snapshot.dataset_analysis_status is None
    assert snapshot.dataset_analysis_summary == {}


@pytest.mark.asyncio
async def test_patched_analysis_joins_approval_chain_with_small_refs(monkeypatch) -> None:
    patch_ids = []

    def patched(patch_id):
        patch_ids.append(patch_id)
        return True

    monkeypatch.setattr("app.workflows.data_loop.workflow.patched", patched)
    summary = {"source_run_count": 1, "analyzed_run_count": 1, "case_count": 2,
               "covered_case_count": 2, "unavailable_case_count": 0}
    workflow = HotNewsDataLoopWorkflow()
    workflow._execute = AsyncMock(side_effect=[
        outcome("freeze-dataset-v1", dataset_id="dataset-1"),
        outcome("analyze-dataset-v1", analysis_status="completed",
                artifact_uri="s3://bucket/dataset-analysis.json",
                artifact_sha256="b" * 64, summary=summary,
                report={"untrusted_full_payload": "must stay outside Workflow state"}),
        outcome("attribute-errors-v1"),
        outcome("evaluate-candidate-v1", evaluation_run_id="evaluation-1",
                gate_passed=True),
        outcome("record-promotion-decision-v1", decision_id="decision-1"),
        outcome("activate-bundle-v1", production_bundle_id="bundle-2"),
    ])
    workflow._decision = DataLoopHumanDecision(
        action="approve", actor_id="operator-1", reason="passed human review",
        idempotency_key="decision-1",
    )
    workflow._wait_for_decision = AsyncMock(return_value=workflow._decision)

    result = await workflow.run(request())

    assert patch_ids == [DATA_LOOP_DATASET_ANALYSIS_PATCH]
    assert result.status == "activated"
    calls = workflow._execute.await_args_list
    assert [call.kwargs["step_type"] for call in calls] == [
        "freeze_dataset", "analyze_dataset", "attribute_errors",
        "evaluate_candidate", "record_promotion_decision", "activate_bundle",
    ]
    assert calls[1].kwargs["step_key"] == "analyze-dataset-v1"
    assert calls[1].kwargs["inputs"] == {"dataset_id": "dataset-1"}
    inputs = calls[3].kwargs["inputs"]
    assert inputs["dataset_analysis_status"] == "completed"
    assert inputs["dataset_analysis_artifact_uri"] == "s3://bucket/dataset-analysis.json"
    assert inputs["dataset_analysis_artifact_sha256"] == "b" * 64
    assert "summary" not in inputs and "report" not in inputs
    snapshot = workflow.snapshot()
    assert snapshot.dataset_analysis_summary == summary
    assert snapshot.dataset_analysis_artifact_sha256 == "b" * 64
    assert "report" not in asdict(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("analysis_status", ["degraded", "disabled", "exception"])
@pytest.mark.parametrize("gate_passed", [False, True])
async def test_optional_analysis_never_bypasses_gate_or_approval(
    monkeypatch, analysis_status, gate_passed,
) -> None:
    monkeypatch.setattr("app.workflows.data_loop.workflow.patched", lambda _: True)
    disabled = analysis_status == "disabled"
    artifact_uri = "s3://bucket/disabled-analysis.json" if disabled else None
    artifact_sha256 = "c" * 64 if disabled else None
    summary = {
        "case_count": 2, "source_run_count": 1, "analyzed_run_count": 0,
        "covered_case_count": 0, "unavailable_case_count": 2,
    } if analysis_status != "exception" else {}
    analysis_result = (
        RuntimeError("analysis service unavailable")
        if analysis_status == "exception" else
        outcome("analyze-dataset-v1", analysis_status=analysis_status,
                artifact_uri=artifact_uri, artifact_sha256=artifact_sha256, summary=summary)
    )
    workflow = HotNewsDataLoopWorkflow()
    outcomes = [
        outcome("freeze-dataset-v1", dataset_id="dataset-1"),
        analysis_result,
        outcome("attribute-errors-v1"),
        outcome("evaluate-candidate-v1", evaluation_run_id="evaluation-1",
                gate_passed=gate_passed),
    ]
    if gate_passed:
        outcomes.append(outcome("record-promotion-decision-v1", decision_id="decision-1"))
    workflow._execute = AsyncMock(side_effect=outcomes)
    workflow._wait_for_decision = AsyncMock(return_value=None)

    result = await workflow.run(request())

    assert result.status == ("approval_expired" if gate_passed else "evaluation_failed")
    assert result.production_bundle_id is None
    steps = [call.kwargs["step_type"] for call in workflow._execute.await_args_list]
    assert "activate_bundle" not in steps
    if gate_passed:
        workflow._wait_for_decision.assert_awaited_once()
    else:
        workflow._wait_for_decision.assert_not_awaited()
    expected_status = "degraded" if analysis_status == "exception" else analysis_status
    assert workflow.snapshot().dataset_analysis_status == expected_status
    assert workflow.snapshot().dataset_analysis_summary == summary
    inputs = workflow._execute.await_args_list[3].kwargs["inputs"]
    assert inputs["dataset_analysis_status"] == expected_status
    assert inputs["dataset_analysis_artifact_uri"] == artifact_uri
    assert inputs["dataset_analysis_artifact_sha256"] == artifact_sha256
    if gate_passed:
        decision = workflow._execute.await_args_list[-1].kwargs["inputs"]
        assert decision["action"] == "reject" and decision["actor_id"] == "system"


@pytest.mark.asyncio
async def test_dataset_analysis_cancellation_propagates(monkeypatch) -> None:
    monkeypatch.setattr("app.workflows.data_loop.workflow.patched", lambda _: True)
    workflow = HotNewsDataLoopWorkflow()
    workflow._execute = AsyncMock(side_effect=[
        outcome("freeze-dataset-v1", dataset_id="dataset-1"),
        asyncio.CancelledError(),
    ])

    with pytest.raises(asyncio.CancelledError):
        await workflow.run(request())

    assert workflow._execute.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [
    {"analysis_status": "approved", "summary": {}},
    {"analysis_status": "disabled", "artifact_uri": None,
     "artifact_sha256": None, "summary": {"reason": "disabled"}},
    {"analysis_status": "completed", "artifact_uri": "s3://bucket/report.json",
     "artifact_sha256": "not-a-content-hash", "summary": {}},
    {"analysis_status": "completed", "artifact_uri": "s3://bucket/report.json",
     "artifact_sha256": "b" * 64, "summary": {"full_report": "x" * 4097}},
    {"analysis_status": "completed", "artifact_uri": "s3://bucket/report.json",
     "artifact_sha256": "b" * 64, "summary": {
         "case_count": True, "source_run_count": 1, "analyzed_run_count": 1,
         "covered_case_count": 1, "unavailable_case_count": 0,
     }},
    {"analysis_status": "completed", "artifact_uri": "s3://bucket/report.json",
     "artifact_sha256": "b" * 64, "summary": {
         "case_count": 1, "source_run_count": 1, "analyzed_run_count": 1,
         "covered_case_count": 1, "unavailable_case_count": -1,
     }},
    {"analysis_status": "completed", "artifact_uri": "s3://bucket/report.json",
     "artifact_sha256": "b" * 64, "summary": {
         "case_count": 1, "source_run_count": 1, "analyzed_run_count": 1,
         "covered_case_count": 1, "unavailable_case_count": 1,
     }},
    {"analysis_status": "completed", "artifact_uri": "s3://bucket/report.json",
     "artifact_sha256": "b" * 64, "summary": {
         "case_count": 1, "source_run_count": 1, "analyzed_run_count": 2,
         "covered_case_count": 1, "unavailable_case_count": 0,
     }},
    {"analysis_status": "completed", "artifact_uri": "s3://bucket/report.json",
     "artifact_sha256": "b" * 64, "summary": {
         "case_count": 1, "source_run_count": 2, "analyzed_run_count": 1,
         "covered_case_count": 1, "unavailable_case_count": 0,
     }},
    {"analysis_status": "completed", "artifact_uri": "s3://bucket/report.json",
     "artifact_sha256": "b" * 64, "summary": {
         "case_count": 2, "source_run_count": 1, "analyzed_run_count": 1,
         "covered_case_count": 1, "unavailable_case_count": 1,
     }},
    {"analysis_status": "completed", "artifact_uri": "s3://bucket/report.json",
     "artifact_sha256": "b" * 64, "summary": {
         "case_count": 1, "source_run_count": 1, "analyzed_run_count": 0,
         "covered_case_count": 1, "unavailable_case_count": 0,
     }},
    {"analysis_status": "disabled", "artifact_uri": "s3://bucket/report.json",
     "artifact_sha256": "b" * 64, "summary": {
         "case_count": 1, "source_run_count": 1, "analyzed_run_count": 0,
         "covered_case_count": 1, "unavailable_case_count": 0,
     }},
    {"analysis_status": "degraded", "artifact_uri": "s3://bucket/report.json",
     "artifact_sha256": "b" * 64, "summary": {
         "case_count": 1, "source_run_count": 1, "analyzed_run_count": 1,
         "covered_case_count": 1, "unavailable_case_count": 0,
     }},
])
async def test_invalid_analysis_receipt_degrades_without_large_payload(
    monkeypatch, values,
) -> None:
    monkeypatch.setattr("app.workflows.data_loop.workflow.patched", lambda _: True)
    workflow = HotNewsDataLoopWorkflow()
    workflow._execute = AsyncMock(side_effect=[
        outcome("freeze-dataset-v1", dataset_id="dataset-1"),
        outcome("analyze-dataset-v1", **values),
        outcome("attribute-errors-v1"),
        outcome("evaluate-candidate-v1", evaluation_run_id="evaluation-1", gate_passed=False),
    ])

    result = await workflow.run(request())

    assert result.status == "evaluation_failed"
    snapshot = workflow.snapshot()
    assert snapshot.dataset_analysis_status == "degraded"
    assert snapshot.dataset_analysis_summary == {}
    assert snapshot.dataset_analysis_artifact_uri is None


@pytest.mark.asyncio
async def test_degraded_analysis_preserves_valid_partial_report(monkeypatch) -> None:
    monkeypatch.setattr("app.workflows.data_loop.workflow.patched", lambda _: True)
    summary = {
        "case_count": 2, "source_run_count": 2, "analyzed_run_count": 1,
        "covered_case_count": 1, "unavailable_case_count": 1,
    }
    workflow = HotNewsDataLoopWorkflow()
    workflow._execute = AsyncMock(side_effect=[
        outcome("freeze-dataset-v1", dataset_id="dataset-1"),
        outcome("analyze-dataset-v1", analysis_status="degraded",
                artifact_uri="s3://bucket/partial-analysis.json",
                artifact_sha256="d" * 64, summary=summary),
        outcome("attribute-errors-v1"),
        outcome("evaluate-candidate-v1", evaluation_run_id="evaluation-1", gate_passed=False),
    ])

    result = await workflow.run(request())

    assert result.status == "evaluation_failed"
    snapshot = workflow.snapshot()
    assert snapshot.dataset_analysis_status == "degraded"
    assert snapshot.dataset_analysis_artifact_uri == "s3://bucket/partial-analysis.json"
    assert snapshot.dataset_analysis_summary == summary
    assert workflow._execute.await_args_list[-1].kwargs["inputs"][
        "dataset_analysis_artifact_sha256"
    ] == "d" * 64


def test_legacy_snapshot_has_optional_analysis_defaults() -> None:
    snapshot = DataLoopWorkflowSnapshot(
        tenant_id="tenant-1", phase="waiting_approval", dataset_id="dataset-1",
        evaluation_run_id="evaluation-1", gate_passed=True, waiting_for_approval=True,
    )
    assert snapshot.dataset_analysis_status is None
    assert snapshot.dataset_analysis_artifact_uri is None
    assert snapshot.dataset_analysis_artifact_sha256 is None
    assert snapshot.dataset_analysis_summary == {}


@pytest.mark.asyncio
async def test_gate_result_must_be_a_real_boolean() -> None:
    workflow = HotNewsDataLoopWorkflow()
    workflow._execute = AsyncMock(
        side_effect=[
            outcome("freeze-dataset-v1", dataset_id="dataset-1"),
            outcome("attribute-errors-v1", attribution_count=1),
            outcome(
                "evaluate-candidate-v1",
                evaluation_run_id="evaluation-1",
                gate_passed="false",
            ),
        ]
    )

    with pytest.raises(ValueError, match="boolean"):
        await workflow.run(request())


@pytest.mark.asyncio
async def test_human_approval_is_recorded_before_activation() -> None:
    workflow = HotNewsDataLoopWorkflow()
    workflow._execute = AsyncMock(
        side_effect=[
            outcome("freeze-dataset-v1", dataset_id="dataset-1"),
            outcome("attribute-errors-v1", attribution_count=2),
            outcome(
                "evaluate-candidate-v1",
                evaluation_run_id="evaluation-1",
                gate_passed=True,
            ),
            outcome("record-promotion-decision-v1", decision_id="decision-1"),
            outcome("activate-bundle-v1", production_bundle_id="bundle-2"),
        ]
    )
    workflow._decision = DataLoopHumanDecision(
        action="approve",
        actor_id="operator-1",
        reason="all deterministic gates passed",
        idempotency_key="approval-request-1",
    )
    workflow._wait_for_decision = AsyncMock(return_value=workflow._decision)

    result = await workflow.run(request())

    assert result.status == "activated"
    assert result.production_bundle_id == "bundle-2"
    calls = workflow._execute.await_args_list
    assert calls[3].kwargs["step_type"] == "record_promotion_decision"
    assert calls[4].kwargs["step_type"] == "activate_bundle"


@pytest.mark.asyncio
async def test_human_rejection_never_activates_candidate() -> None:
    workflow = HotNewsDataLoopWorkflow()
    workflow._execute = AsyncMock(
        side_effect=[
            outcome("freeze-dataset-v1", dataset_id="dataset-1"),
            outcome("attribute-errors-v1"),
            outcome(
                "evaluate-candidate-v1",
                evaluation_run_id="evaluation-1",
                gate_passed=True,
            ),
            outcome("record-promotion-decision-v1", decision_id="decision-1"),
        ]
    )
    workflow._decision = DataLoopHumanDecision(
        action="reject",
        actor_id="operator-1",
        reason="editorial risk",
        idempotency_key="rejection-request-1",
    )
    workflow._wait_for_decision = AsyncMock(return_value=workflow._decision)

    result = await workflow.run(request())

    assert result.status == "rejected"
    assert result.production_bundle_id is None
    assert workflow._execute.await_count == 4


@pytest.mark.asyncio
async def test_approval_timeout_records_system_rejection_and_never_activates() -> None:
    workflow = HotNewsDataLoopWorkflow()
    workflow._execute = AsyncMock(
        side_effect=[
            outcome("freeze-dataset-v1", dataset_id="dataset-1"),
            outcome("attribute-errors-v1"),
            outcome(
                "evaluate-candidate-v1",
                evaluation_run_id="evaluation-1",
                gate_passed=True,
            ),
            outcome("record-promotion-decision-v1", decision_id="decision-1"),
        ]
    )
    workflow._wait_for_decision = AsyncMock(return_value=None)

    result = await workflow.run(request())

    assert result.status == "approval_expired"
    assert result.production_bundle_id is None
    assert workflow.snapshot().phase == "approval_expired"
    decision_call = workflow._execute.await_args_list[-1]
    assert decision_call.kwargs["step_type"] == "record_promotion_decision"
    assert decision_call.kwargs["inputs"]["action"] == "reject"
    assert decision_call.kwargs["inputs"]["actor_id"] == "system"
    assert workflow._execute.await_count == 4


def test_decision_update_is_atomic_idempotent_and_rejects_a_second_decision() -> None:
    workflow = HotNewsDataLoopWorkflow()
    workflow._tenant_id = "tenant-1"
    workflow._phase = "waiting_approval"
    workflow._gate_passed = True
    decision = DataLoopHumanDecision(
        action="approve",
        actor_id="operator-1",
        reason="passed review",
        idempotency_key="approval-request-1",
    )
    command = DataLoopDecisionUpdate(tenant_id="tenant-1", decision=decision)

    first = workflow.submit_promotion_decision(command)
    replay = workflow.submit_promotion_decision(command)

    assert first.created is True
    assert replay.created is False
    with pytest.raises(ValueError, match="different content"):
        workflow.submit_promotion_decision(
            DataLoopDecisionUpdate(
                tenant_id="tenant-1",
                decision=DataLoopHumanDecision(
                    action="approve",
                    actor_id="operator-1",
                    reason="changed reason",
                    idempotency_key="approval-request-1",
                ),
            )
        )
    with pytest.raises(ValueError, match="already been submitted"):
        workflow.submit_promotion_decision(
            DataLoopDecisionUpdate(
                tenant_id="tenant-1",
                decision=DataLoopHumanDecision(
                    action="reject",
                    actor_id="operator-2",
                    reason="second decision",
                    idempotency_key="approval-request-2",
                ),
            )
        )


def test_decision_update_checks_tenant_and_gate_inside_workflow() -> None:
    workflow = HotNewsDataLoopWorkflow()
    workflow._tenant_id = "tenant-1"
    workflow._phase = "waiting_approval"
    workflow._gate_passed = True
    decision = DataLoopHumanDecision(
        action="approve",
        actor_id="operator-1",
        reason="passed review",
        idempotency_key="approval-request-1",
    )

    with pytest.raises(ValueError, match="not found"):
        workflow.submit_promotion_decision(
            DataLoopDecisionUpdate(tenant_id="tenant-2", decision=decision)
        )
    workflow._phase = "activate_bundle"
    with pytest.raises(ValueError, match="not waiting"):
        workflow.submit_promotion_decision(
            DataLoopDecisionUpdate(tenant_id="tenant-1", decision=decision)
        )


@pytest.mark.asyncio
async def test_activation_recovery_reuses_original_activation_ledger_key(
    monkeypatch,
) -> None:
    execute_activity = AsyncMock(
        side_effect=[
            outcome(
                "record-promotion-decision-v1",
                decision_id="decision-1",
            ),
            outcome(
                "activate-bundle-v1",
                production_bundle_id="bundle-2",
            ),
        ]
    )
    monkeypatch.setattr(
        "app.workflows.data_loop.workflow.execute_activity",
        execute_activity,
    )
    decision = DataLoopHumanDecision(
        action="approve",
        actor_id="operator-1",
        reason="passed review",
        idempotency_key="approval-request-1",
    )
    recovery = DataLoopActivationRecoveryRequest(
        tenant_id="tenant-1",
        source_workflow_id="source-workflow-1",
        candidate_id="candidate-1",
        evaluation_run_id="evaluation-1",
        approval_decision=decision,
        requested_by="operator-2",
        idempotency_key="recovery-request-1",
    )

    result = await HotNewsDataLoopActivationRecoveryWorkflow().run(recovery)

    assert result.production_bundle_id == "bundle-2"
    assert execute_activity.await_count == 2
    approval_command = execute_activity.await_args_list[0].args[1]
    activation_command = execute_activity.await_args_list[1].args[1]
    assert approval_command.step_type == "record_promotion_decision"
    assert approval_command.inputs["action"] == "approve"
    assert approval_command.inputs["evaluation_run_id"] == "evaluation-1"
    assert approval_command.inputs["actor_id"] == "operator-1"
    assert approval_command.inputs["idempotency_key"] == "approval-request-1"
    assert activation_command.inputs["evaluation_run_id"] == "evaluation-1"
    assert activation_command.inputs["actor_id"] == "operator-1"
    assert (
        activation_command.inputs["idempotency_key"]
        == "approval-request-1:activate"
    )
    assert execute_activity.await_args.kwargs["heartbeat_timeout"] == (
        DATA_LOOP_ACTIVITY_HEARTBEAT_TIMEOUT
    )


@pytest.mark.asyncio
async def test_activation_recovery_never_activates_if_approval_replay_fails(
    monkeypatch,
) -> None:
    execute_activity = AsyncMock(
        side_effect=RuntimeError("approval ledger is unavailable")
    )
    monkeypatch.setattr(
        "app.workflows.data_loop.workflow.execute_activity",
        execute_activity,
    )
    recovery = DataLoopActivationRecoveryRequest(
        tenant_id="tenant-1",
        source_workflow_id="source-workflow-1",
        candidate_id="candidate-1",
        evaluation_run_id="evaluation-1",
        approval_decision=DataLoopHumanDecision(
            action="approve",
            actor_id="operator-1",
            reason="passed review",
            idempotency_key="approval-request-1",
        ),
        requested_by="operator-2",
        idempotency_key="recovery-request-1",
    )

    with pytest.raises(RuntimeError, match="approval ledger"):
        await HotNewsDataLoopActivationRecoveryWorkflow().run(recovery)

    assert execute_activity.await_count == 1
    command = execute_activity.await_args.args[1]
    assert command.step_type == "record_promotion_decision"
