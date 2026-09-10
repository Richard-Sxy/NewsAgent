"""Operator API for bounded Data Loop execution and promotion approval."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.service import RPCError

from app.api.dependencies import (
    DataLoopPermission,
    DataLoopPrincipal,
    get_analysis_feedback_collector,
    get_data_loop_orchestrator,
    get_evaluation_dataset_artifact_store,
    get_hot_news_decision_service,
    get_production_bundle_service,
    get_publication_outcome_feedback_service,
    get_data_loop_runtime_registry,
    get_session,
    require_data_loop_permission,
)
from app.domain.errors import ArtifactStoreError
from app.repositories.evaluation_dataset import (
    EvaluationDatasetConflictError,
    EvaluationDatasetRepositoryError,
    PostgresEvaluationDatasetRepository,
)
from app.repositories.production_bundle import (
    PostgresProductionBundleRepository,
)
from app.schemas.analysis_feedback import (
    ApproveAnalysisFeedbackLabelCommand,
    FeedbackStatus,
    RecordPublicationOutcomeCommand,
    SubmitAnalysisFeedbackLabelCommand,
)
from app.schemas.data_loop_api import (
    ApproveFeedbackLabelRequest,
    BootstrapProductionBundleRequest,
    CandidateResponse,
    DataLoopDecisionResponse,
    DataLoopSnapshotResponse,
    FeedbackCaseListResponse,
    FeedbackLabelListResponse,
    FeedbackLabelResponse,
    FreezeDatasetRequest,
    FreezeDatasetResponse,
    OperatorDecisionResponse,
    ProductionBundleResponse,
    ProposeCandidateRequest,
    PublicationOutcomeResponse,
    RecoverDataLoopActivationRequest,
    RecoverDataLoopActivationResponse,
    RecordOperatorDecisionRequest,
    RollbackProductionBundleRequest,
    RollbackProductionBundleResponse,
    StartDataLoopRequest,
    StartDataLoopResponse,
    SubmitFeedbackLabelRequest,
    SubmitDataLoopDecisionRequest,
)
from app.schemas.evaluation_dataset import FreezeEvaluationDatasetCommand
from app.schemas.hot_news_decision import RecordHotNewsDecisionCommand
from app.schemas.production_bundle import (
    ProposeConfigurationCandidateCommand,
    RollbackProductionBundleCommand,
)
from app.services.data_loop.dataset_freezer import (
    EvaluationDatasetArtifactStore,
    EvaluationDatasetFreezer,
    EvaluationDatasetNotReadyError,
)
from app.services.data_loop.feedback_collector import (
    AnalysisFeedbackCollector,
    AnalysisFeedbackConflictError,
    AnalysisFeedbackPersistenceError,
    AnalysisFeedbackTargetNotFoundError,
)
from app.services.data_loop.orchestrator import (
    DataLoopActivationRecoveryNotAllowedError,
    DataLoopDecisionNotAllowedError,
    DataLoopOrchestrator,
    DataLoopStartConflictError,
    DataLoopWorkflowAccessError,
)
from app.services.data_loop.publication_outcome import (
    PublicationOutcomeFeedbackService,
)
from app.services.hot_news_decision import (
    HotNewsDecisionConflictError,
    HotNewsDecisionPersistenceError,
    HotNewsDecisionService,
    HotNewsDecisionTargetNotFoundError,
)
from app.services.production_bundle import (
    ProductionBundleApplicationService,
    ProductionBundleConflictError,
    ProductionBundleNotFoundError,
    ProductionBundlePersistenceError,
    ProductionBundleRuleViolation,
)
from app.services.production_bundle_runtime import (
    ProductionBundleRuntimeRegistry,
    UnsupportedProductionBundleRuntimeError,
)
from app.workflows.data_loop_contracts import (
    DataLoopHumanDecision,
    DataLoopRunRequest,
)


router = APIRouter(prefix="/api/v1/data-loop", tags=["hot-news-data-loop"])

ReadPrincipal = Annotated[
    DataLoopPrincipal,
    Depends(require_data_loop_permission(DataLoopPermission.READ)),
]
FeedbackWritePrincipal = Annotated[
    DataLoopPrincipal,
    Depends(require_data_loop_permission(DataLoopPermission.FEEDBACK_WRITE)),
]
LabelSubmitPrincipal = Annotated[
    DataLoopPrincipal,
    Depends(require_data_loop_permission(DataLoopPermission.LABEL_SUBMIT)),
]
LabelApprovePrincipal = Annotated[
    DataLoopPrincipal,
    Depends(require_data_loop_permission(DataLoopPermission.LABEL_APPROVE)),
]
DatasetManagePrincipal = Annotated[
    DataLoopPrincipal,
    Depends(require_data_loop_permission(DataLoopPermission.DATASET_MANAGE)),
]
CandidateManagePrincipal = Annotated[
    DataLoopPrincipal,
    Depends(require_data_loop_permission(DataLoopPermission.CANDIDATE_MANAGE)),
]
RunPrincipal = Annotated[
    DataLoopPrincipal,
    Depends(require_data_loop_permission(DataLoopPermission.RUN)),
]
ReleaseApprovePrincipal = Annotated[
    DataLoopPrincipal,
    Depends(require_data_loop_permission(DataLoopPermission.RELEASE_APPROVE)),
]
RollbackPrincipal = Annotated[
    DataLoopPrincipal,
    Depends(require_data_loop_permission(DataLoopPermission.ROLLBACK)),
]


@router.post(
    "/runs",
    response_model=StartDataLoopResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_data_loop(
    request: StartDataLoopRequest,
    principal: RunPrincipal,
    orchestrator: DataLoopOrchestrator = Depends(get_data_loop_orchestrator),
) -> StartDataLoopResponse:
    tenant_id = principal.tenant_id
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
    except DataLoopStartConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RPCError as exc:
        raise HTTPException(
            status_code=503,
            detail="Temporal is unavailable; retry with the same idempotency_key",
        ) from exc
    return StartDataLoopResponse(workflow_id=workflow_id)


@router.get("/runs/{workflow_id}", response_model=DataLoopSnapshotResponse)
async def get_data_loop_run(
    workflow_id: str,
    principal: ReadPrincipal,
    orchestrator: DataLoopOrchestrator = Depends(get_data_loop_orchestrator),
) -> DataLoopSnapshotResponse:
    tenant_id = principal.tenant_id
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
    principal: ReleaseApprovePrincipal,
    orchestrator: DataLoopOrchestrator = Depends(get_data_loop_orchestrator),
) -> DataLoopDecisionResponse:
    tenant_id = principal.tenant_id
    user_id = principal.user_id
    try:
        receipt = await orchestrator.submit_decision(
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
    return DataLoopDecisionResponse(
        workflow_id=workflow_id,
        created=receipt.created,
    )


@router.post(
    "/runs/{workflow_id}/activation/recover",
    response_model=RecoverDataLoopActivationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def recover_data_loop_activation(
    workflow_id: str,
    request: RecoverDataLoopActivationRequest,
    principal: ReleaseApprovePrincipal,
    orchestrator: DataLoopOrchestrator = Depends(get_data_loop_orchestrator),
) -> RecoverDataLoopActivationResponse:
    """Retry only the activation already authorized by this source Workflow."""

    tenant_id = principal.tenant_id
    user_id = principal.user_id
    try:
        recovery_workflow_id = await orchestrator.recover_activation(
            workflow_id,
            tenant_id=str(tenant_id),
            requested_by=str(user_id),
            idempotency_key=request.idempotency_key,
        )
    except DataLoopWorkflowAccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        DataLoopActivationRecoveryNotAllowedError,
        DataLoopStartConflictError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RPCError as exc:
        raise HTTPException(status_code=503, detail="Temporal is unavailable") from exc
    return RecoverDataLoopActivationResponse(
        source_workflow_id=workflow_id,
        recovery_workflow_id=recovery_workflow_id,
    )


@router.post(
    "/operator-decisions",
    response_model=OperatorDecisionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_operator_decision(
    request: RecordOperatorDecisionRequest,
    principal: FeedbackWritePrincipal,
    service: HotNewsDecisionService = Depends(get_hot_news_decision_service),
) -> OperatorDecisionResponse:
    tenant_id = principal.tenant_id
    user_id = principal.user_id
    try:
        decision, created = await service.record_decision(
            tenant_id=str(tenant_id),
            operator_id=str(user_id),
            command=RecordHotNewsDecisionCommand(
                run_id=request.run_id,
                news_id=request.news_id,
                decision_type=request.decision_type,
                reason=request.reason,
                correction_payload=request.correction_payload,
                idempotency_key=request.idempotency_key,
                supersedes_decision_id=request.supersedes_decision_id,
            ),
            feedback_problem_type=request.feedback_problem_type,
            feedback_severity=request.feedback_severity,
        )
    except Exception as exc:
        _raise_data_loop_http_error(exc)
    return OperatorDecisionResponse(
        decision_id=decision.id,
        decision_type=decision.decision_type,
        created=created,
    )


@router.get("/feedback-cases", response_model=FeedbackCaseListResponse)
async def list_feedback_cases(
    principal: ReadPrincipal,
    statuses: Annotated[list[FeedbackStatus] | None, Query()] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    session: AsyncSession = Depends(get_session),
    collector: AnalysisFeedbackCollector = Depends(
        get_analysis_feedback_collector
    ),
) -> FeedbackCaseListResponse:
    tenant_id = principal.tenant_id
    try:
        cases = await collector.list_cases(
            session,
            tenant_id=str(tenant_id),
            statuses=statuses,
            offset=offset,
            limit=limit,
        )
    except Exception as exc:
        _raise_data_loop_http_error(exc)
    return FeedbackCaseListResponse(cases=tuple(cases))


@router.get(
    "/feedback-cases/{feedback_case_id}/labels",
    response_model=FeedbackLabelListResponse,
)
async def list_feedback_labels(
    feedback_case_id: UUID,
    principal: ReadPrincipal,
    session: AsyncSession = Depends(get_session),
    collector: AnalysisFeedbackCollector = Depends(
        get_analysis_feedback_collector
    ),
) -> FeedbackLabelListResponse:
    tenant_id = principal.tenant_id
    try:
        labels = await collector.list_labels(
            session,
            tenant_id=str(tenant_id),
            feedback_case_id=feedback_case_id,
        )
    except Exception as exc:
        _raise_data_loop_http_error(exc)
    return FeedbackLabelListResponse(labels=tuple(labels))


@router.post(
    "/feedback-cases/{feedback_case_id}/labels",
    response_model=FeedbackLabelResponse,
    status_code=status.HTTP_201_CREATED,
)
async def submit_feedback_label(
    feedback_case_id: UUID,
    request: SubmitFeedbackLabelRequest,
    principal: LabelSubmitPrincipal,
    session: AsyncSession = Depends(get_session),
    collector: AnalysisFeedbackCollector = Depends(
        get_analysis_feedback_collector
    ),
) -> FeedbackLabelResponse:
    tenant_id = principal.tenant_id
    user_id = principal.user_id
    now = datetime.now(UTC)
    try:
        outcome = await collector.submit_label(
            session,
            tenant_id=str(tenant_id),
            command=SubmitAnalysisFeedbackLabelCommand(
                feedback_case_id=feedback_case_id,
                labeled_by=str(user_id),
                labeled_at=now,
                **request.model_dump(),
            ),
            recorded_at=now,
        )
    except Exception as exc:
        _raise_data_loop_http_error(exc)
    return FeedbackLabelResponse(
        label=outcome.label,
        created=outcome.created,
    )


@router.post(
    "/feedback-cases/{feedback_case_id}/labels/{label_id}/approve",
    response_model=FeedbackLabelResponse,
)
async def approve_feedback_label(
    feedback_case_id: UUID,
    label_id: UUID,
    request: ApproveFeedbackLabelRequest,
    principal: LabelApprovePrincipal,
    session: AsyncSession = Depends(get_session),
    collector: AnalysisFeedbackCollector = Depends(
        get_analysis_feedback_collector
    ),
) -> FeedbackLabelResponse:
    tenant_id = principal.tenant_id
    user_id = principal.user_id
    try:
        outcome = await collector.approve_label(
            session,
            tenant_id=str(tenant_id),
            command=ApproveAnalysisFeedbackLabelCommand(
                feedback_case_id=feedback_case_id,
                label_id=label_id,
                expected_label_version=request.expected_label_version,
                approved_by=str(user_id),
                approved_at=datetime.now(UTC),
                idempotency_key=request.idempotency_key,
            ),
        )
    except Exception as exc:
        _raise_data_loop_http_error(exc)
    return FeedbackLabelResponse(
        label=outcome.label,
        created=outcome.created,
    )


@router.post(
    "/publication-outcomes",
    response_model=PublicationOutcomeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_publication_outcome(
    request: RecordPublicationOutcomeCommand,
    principal: FeedbackWritePrincipal,
    session: AsyncSession = Depends(get_session),
    service: PublicationOutcomeFeedbackService = Depends(
        get_publication_outcome_feedback_service
    ),
) -> PublicationOutcomeResponse:
    tenant_id = principal.tenant_id
    try:
        outcome = await service.record_and_collect(
            session,
            tenant_id=str(tenant_id),
            command=request,
            recorded_at=datetime.now(UTC),
        )
    except Exception as exc:
        _raise_data_loop_http_error(exc)
    return PublicationOutcomeResponse(
        outcome=outcome.outcome,
        created=outcome.outcome_created,
        feedback_case_id=(
            None
            if outcome.feedback_case is None
            else outcome.feedback_case.case.id
        ),
    )


@router.post(
    "/datasets/freeze",
    response_model=FreezeDatasetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def freeze_evaluation_dataset(
    request: FreezeDatasetRequest,
    principal: DatasetManagePrincipal,
    session: AsyncSession = Depends(get_session),
    artifact_store: EvaluationDatasetArtifactStore = Depends(
        get_evaluation_dataset_artifact_store
    ),
) -> FreezeDatasetResponse:
    tenant_id = principal.tenant_id
    user_id = principal.user_id
    try:
        outcome = await EvaluationDatasetFreezer(
            repository=PostgresEvaluationDatasetRepository(session),
            artifact_store=artifact_store,
        ).freeze(
            FreezeEvaluationDatasetCommand(
                tenant_id=str(tenant_id),
                dataset_name=request.dataset_name,
                dataset_version=request.dataset_version,
                dataset_layer=request.dataset_layer,
                description=request.description,
                feedback_case_ids=request.feedback_case_ids,
                source_cutoff_at=request.source_cutoff_at,
                frozen_by=str(user_id),
                idempotency_key=request.idempotency_key,
            ),
            end_prepare_transaction=session.rollback,
        )
    except Exception as exc:
        _raise_data_loop_http_error(exc)
    return FreezeDatasetResponse(
        dataset=outcome.dataset,
        created=outcome.created,
    )


@router.get(
    "/datasets/{dataset_id}",
    response_model=FreezeDatasetResponse,
)
async def get_evaluation_dataset(
    dataset_id: UUID,
    principal: ReadPrincipal,
    session: AsyncSession = Depends(get_session),
) -> FreezeDatasetResponse:
    tenant_id = principal.tenant_id
    try:
        dataset = await PostgresEvaluationDatasetRepository(session).get_by_id(
            tenant_id=str(tenant_id),
            dataset_id=dataset_id,
        )
        if dataset is None:
            raise EvaluationDatasetNotReadyError(
                "evaluation dataset not found"
            )
    except Exception as exc:
        _raise_data_loop_http_error(exc)
    return FreezeDatasetResponse(dataset=dataset, created=False)


@router.post(
    "/production-bundles/bootstrap",
    response_model=ProductionBundleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def bootstrap_production_bundle(
    request: BootstrapProductionBundleRequest,
    principal: CandidateManagePrincipal,
    session: AsyncSession = Depends(get_session),
    runtime_registry: ProductionBundleRuntimeRegistry = Depends(
        get_data_loop_runtime_registry
    ),
    service: ProductionBundleApplicationService = Depends(
        get_production_bundle_service
    ),
) -> ProductionBundleResponse:
    tenant_id = principal.tenant_id
    user_id = principal.user_id
    try:
        runtime_registry.ensure_supported(request.spec)
        outcome = await service.bootstrap_initial_bundle(
            session,
            tenant_id=str(tenant_id),
            bundle_version=request.bundle_version,
            spec=request.spec,
            actor_id=str(user_id),
            now=datetime.now(UTC),
        )
    except Exception as exc:
        _raise_data_loop_http_error(exc)
    return ProductionBundleResponse(
        bundle=outcome.bundle,
        created=outcome.created,
    )


@router.get(
    "/production-bundles/active",
    response_model=ProductionBundleResponse,
)
async def get_active_production_bundle(
    principal: ReadPrincipal,
    session: AsyncSession = Depends(get_session),
) -> ProductionBundleResponse:
    tenant_id = principal.tenant_id
    try:
        bundle = await PostgresProductionBundleRepository(
            session
        ).get_active_bundle(tenant_id=str(tenant_id))
        if bundle is None:
            raise ProductionBundleNotFoundError(
                "active production bundle is not available"
            )
    except Exception as exc:
        _raise_data_loop_http_error(exc)
    return ProductionBundleResponse(bundle=bundle, created=False)


@router.post(
    "/configuration-candidates",
    response_model=CandidateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def propose_configuration_candidate(
    request: ProposeCandidateRequest,
    principal: CandidateManagePrincipal,
    session: AsyncSession = Depends(get_session),
    service: ProductionBundleApplicationService = Depends(
        get_production_bundle_service
    ),
) -> CandidateResponse:
    tenant_id = principal.tenant_id
    user_id = principal.user_id
    try:
        outcome = await service.propose_candidate(
            session,
            command=ProposeConfigurationCandidateCommand(
                tenant_id=str(tenant_id),
                base_bundle_id=request.base_bundle_id,
                candidate_version=request.candidate_version,
                proposed_spec=request.proposed_spec,
                structured_diff=request.structured_diff,
                proposed_by=str(user_id),
                proposal_reason=request.proposal_reason,
                idempotency_key=request.idempotency_key,
            ),
            now=datetime.now(UTC),
        )
    except Exception as exc:
        _raise_data_loop_http_error(exc)
    return CandidateResponse(
        candidate=outcome.candidate,
        created=outcome.created,
    )


@router.get(
    "/configuration-candidates/{candidate_id}",
    response_model=CandidateResponse,
)
async def get_configuration_candidate(
    candidate_id: UUID,
    principal: ReadPrincipal,
    session: AsyncSession = Depends(get_session),
) -> CandidateResponse:
    tenant_id = principal.tenant_id
    try:
        candidate = await PostgresProductionBundleRepository(
            session
        ).get_candidate(
            tenant_id=str(tenant_id),
            candidate_id=candidate_id,
        )
        if candidate is None:
            raise ProductionBundleNotFoundError(
                "configuration candidate is not available"
            )
    except Exception as exc:
        _raise_data_loop_http_error(exc)
    return CandidateResponse(candidate=candidate, created=False)


@router.post(
    "/production-bundles/rollback",
    response_model=RollbackProductionBundleResponse,
)
async def rollback_production_bundle(
    request: RollbackProductionBundleRequest,
    principal: RollbackPrincipal,
    session: AsyncSession = Depends(get_session),
    runtime_registry: ProductionBundleRuntimeRegistry = Depends(
        get_data_loop_runtime_registry
    ),
    service: ProductionBundleApplicationService = Depends(
        get_production_bundle_service
    ),
) -> RollbackProductionBundleResponse:
    tenant_id = principal.tenant_id
    user_id = principal.user_id
    try:
        target = await PostgresProductionBundleRepository(
            session
        ).get_bundle(
            tenant_id=str(tenant_id),
            bundle_id=request.target_bundle_id,
        )
        if target is None:
            raise ProductionBundleNotFoundError(
                "rollback target production bundle is not available"
            )
        runtime_registry.ensure_supported(target.spec)
        outcome = await service.rollback_bundle(
            session,
            command=RollbackProductionBundleCommand(
                tenant_id=str(tenant_id),
                target_bundle_id=request.target_bundle_id,
                rolled_back_by=str(user_id),
                reason=request.reason,
                idempotency_key=request.idempotency_key,
            ),
            now=datetime.now(UTC),
        )
    except Exception as exc:
        _raise_data_loop_http_error(exc)
    return RollbackProductionBundleResponse(
        active_bundle=outcome.activated_bundle,
        decision=outcome.decision,
        created=outcome.created,
    )


def _raise_data_loop_http_error(exc: Exception) -> None:
    if isinstance(
        exc,
        (
            AnalysisFeedbackTargetNotFoundError,
            HotNewsDecisionTargetNotFoundError,
            ProductionBundleNotFoundError,
        ),
    ):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(
        exc,
        (
            AnalysisFeedbackConflictError,
            HotNewsDecisionConflictError,
            EvaluationDatasetConflictError,
            EvaluationDatasetNotReadyError,
            ProductionBundleConflictError,
            ProductionBundleRuleViolation,
            UnsupportedProductionBundleRuntimeError,
        ),
    ):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(
        exc,
        (
            AnalysisFeedbackPersistenceError,
            HotNewsDecisionPersistenceError,
            EvaluationDatasetRepositoryError,
            ProductionBundlePersistenceError,
            ArtifactStoreError,
        ),
    ):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Never return to a route with uninitialized response variables. Unknown
    # defects remain 500s and preserve their original traceback for operators.
    raise exc
