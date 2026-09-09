"""Operator API for bounded Data Loop execution and promotion approval."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from temporalio.service import RPCError

from app.api.dependencies import (
    get_data_loop_orchestrator,
    get_tenant_id,
    get_user_id,
)
from app.schemas.data_loop_api import (
    DataLoopDecisionResponse,
    DataLoopSnapshotResponse,
    StartDataLoopRequest,
    StartDataLoopResponse,
    SubmitDataLoopDecisionRequest,
)
from app.services.data_loop.orchestrator import (
    DataLoopDecisionNotAllowedError,
    DataLoopOrchestrator,
    DataLoopWorkflowAccessError,
)
from app.workflows.data_loop_contracts import (
    DataLoopHumanDecision,
    DataLoopRunRequest,
)


router = APIRouter(prefix="/api/v1/data-loop", tags=["hot-news-data-loop"])


@router.post(
    "/runs",
    response_model=StartDataLoopResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_data_loop(
    request: StartDataLoopRequest,
    tenant_id: UUID = Depends(get_tenant_id),
    orchestrator: DataLoopOrchestrator = Depends(get_data_loop_orchestrator),
) -> StartDataLoopResponse:
    command = DataLoopRunRequest(
        tenant_id=str(tenant_id),
        window_start=request.window_start,
        window_end=request.window_end,
        dataset_name=request.dataset_name,
        dataset_version=request.dataset_version,
        golden_dataset_id=str(request.golden_dataset_id),
        high_risk_regression_dataset_id=str(
            request.high_risk_regression_dataset_id
        ),
        candidate_id=str(request.candidate_id),
        previous_experiment_candidate_id=(
            str(request.previous_experiment_candidate_id)
            if request.previous_experiment_candidate_id is not None
            else None
        ),
        evaluation_policy_version=request.evaluation_policy_version,
        idempotency_key=request.idempotency_key,
    )
    try:
        workflow_id = await orchestrator.start(command)
    except RPCError as exc:
        raise HTTPException(
            status_code=503,
            detail="Temporal is unavailable; retry with the same idempotency_key",
        ) from exc
    return StartDataLoopResponse(workflow_id=workflow_id)


@router.get("/runs/{workflow_id}", response_model=DataLoopSnapshotResponse)
async def get_data_loop_run(
    workflow_id: str,
    tenant_id: UUID = Depends(get_tenant_id),
    orchestrator: DataLoopOrchestrator = Depends(get_data_loop_orchestrator),
) -> DataLoopSnapshotResponse:
    try:
        snapshot = await orchestrator.get_snapshot(
            workflow_id,
            tenant_id=str(tenant_id),
        )
    except DataLoopWorkflowAccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RPCError as exc:
        raise HTTPException(status_code=503, detail="Temporal is unavailable") from exc
    return DataLoopSnapshotResponse(
        workflow_id=workflow_id,
        phase=snapshot.phase,
        dataset_id=snapshot.dataset_id,
        evaluation_run_id=snapshot.evaluation_run_id,
        gate_passed=snapshot.gate_passed,
        waiting_for_approval=snapshot.waiting_for_approval,
    )


@router.post(
    "/runs/{workflow_id}/decision",
    response_model=DataLoopDecisionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_data_loop_decision(
    workflow_id: str,
    request: SubmitDataLoopDecisionRequest,
    tenant_id: UUID = Depends(get_tenant_id),
    user_id: UUID = Depends(get_user_id),
    orchestrator: DataLoopOrchestrator = Depends(get_data_loop_orchestrator),
) -> DataLoopDecisionResponse:
    try:
        await orchestrator.submit_decision(
            workflow_id,
            DataLoopHumanDecision(
                action=request.action,
                actor_id=str(user_id),
                reason=request.reason,
                idempotency_key=request.idempotency_key,
            ),
            tenant_id=str(tenant_id),
        )
    except DataLoopWorkflowAccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DataLoopDecisionNotAllowedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RPCError as exc:
        raise HTTPException(status_code=503, detail="Temporal is unavailable") from exc
    return DataLoopDecisionResponse(workflow_id=workflow_id)
