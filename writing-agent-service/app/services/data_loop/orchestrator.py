"""API-facing Temporal client for bounded Data Loop runs."""

from __future__ import annotations

import json
from datetime import timezone
from hashlib import sha256
from typing import Any, Callable

from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.config import Settings
from app.workflows.data_loop import (
    HotNewsDataLoopActivationRecoveryWorkflow,
    HotNewsDataLoopWorkflow,
)
from app.workflows.data_loop_contracts import (
    DataLoopActivationRecoveryContext,
    DataLoopActivationRecoveryRequest,
    DataLoopDecisionUpdate,
    DataLoopDecisionUpdateResult,
    DataLoopHumanDecision,
    DataLoopRunRequest,
    DataLoopWorkflowSnapshot,
)


DATA_LOOP_REQUEST_FINGERPRINT_MEMO_KEY = "data_loop_request_fingerprint"


class DataLoopDecisionNotAllowedError(ValueError):
    pass


class DataLoopWorkflowAccessError(LookupError):
    pass


class DataLoopStartConflictError(ValueError):
    pass


class DataLoopActivationRecoveryNotAllowedError(ValueError):
    pass


class DataLoopOrchestrator:
    """The only API-layer boundary allowed to start or signal Data Loop workflows."""

    def __init__(self, client: Client, settings: Settings) -> None:
        self._client = client
        self._task_queue = settings.temporal_data_loop_task_queue

    @staticmethod
    def workflow_id(request: DataLoopRunRequest) -> str:
        digest = sha256(
            f"{request.tenant_id}\x1f{request.idempotency_key}".encode("utf-8")
        ).hexdigest()
        return f"hot-news-data-loop-{digest}"

    @staticmethod
    def request_fingerprint(request: DataLoopRunRequest) -> str:
        """Hash every stable start field so an idempotency key cannot alias input."""

        request.validate()
        return DataLoopOrchestrator._fingerprint(
            {
                "tenant_id": request.tenant_id,
                "window_start": request.window_start.astimezone(
                    timezone.utc
                ).isoformat(),
                "window_end": request.window_end.astimezone(
                    timezone.utc
                ).isoformat(),
                "dataset_name": request.dataset_name,
                "dataset_version": request.dataset_version,
                "golden_dataset_id": request.golden_dataset_id,
                "high_risk_regression_dataset_id": (
                    request.high_risk_regression_dataset_id
                ),
                "candidate_id": request.candidate_id,
                "previous_experiment_candidate_id": (
                    request.previous_experiment_candidate_id
                ),
                "evaluation_policy_version": request.evaluation_policy_version,
                "idempotency_key": request.idempotency_key,
            }
        )

    async def start(self, request: DataLoopRunRequest) -> str:
        request.validate()
        workflow_id = self.workflow_id(request)
        await self._start_idempotent(
            workflow=HotNewsDataLoopWorkflow.run,
            request=request,
            workflow_id=workflow_id,
            request_fingerprint=self.request_fingerprint(request),
        )
        return workflow_id

    async def get_snapshot(
        self,
        workflow_id: str,
        *,
        tenant_id: str,
    ) -> DataLoopWorkflowSnapshot:
        snapshot = await self._client.get_workflow_handle(workflow_id).query(
            HotNewsDataLoopWorkflow.snapshot,
            result_type=DataLoopWorkflowSnapshot,
        )
        if snapshot.tenant_id != tenant_id:
            raise DataLoopWorkflowAccessError("Data Loop workflow was not found")
        return snapshot

    async def submit_decision(
        self,
        workflow_id: str,
        decision: DataLoopHumanDecision,
        *,
        tenant_id: str,
    ) -> DataLoopDecisionUpdateResult:
        decision.validate()
        handle = self._client.get_workflow_handle(workflow_id)
        try:
            return await handle.execute_update(
                HotNewsDataLoopWorkflow.submit_promotion_decision,
                DataLoopDecisionUpdate(
                    tenant_id=tenant_id,
                    decision=decision,
                ),
                # Bind Temporal's transport-level deduplication to the full
                # command. A reused business key with changed content gets a
                # different Update ID and must still pass the Workflow's
                # atomic validator.
                id=self.decision_update_id(tenant_id, decision),
                result_type=DataLoopDecisionUpdateResult,
            )
        except WorkflowUpdateFailedError as exc:
            message = str(exc.cause).strip() or "promotion decision was rejected"
            if "Data Loop workflow was not found" in message:
                raise DataLoopWorkflowAccessError(
                    "Data Loop workflow was not found"
                ) from exc
            raise DataLoopDecisionNotAllowedError(message) from exc

    @staticmethod
    def decision_update_id(
        tenant_id: str,
        decision: DataLoopHumanDecision,
    ) -> str:
        decision.validate()
        digest = DataLoopOrchestrator._fingerprint(
            {
                "tenant_id": tenant_id,
                "action": decision.action,
                "actor_id": decision.actor_id,
                "reason": decision.reason,
                "idempotency_key": decision.idempotency_key,
            }
        )
        return f"data-loop-promotion-decision-{digest}"

    async def recover_activation(
        self,
        source_workflow_id: str,
        *,
        tenant_id: str,
        requested_by: str,
        idempotency_key: str,
    ) -> str:
        """Start one bounded replay of the source approval's activation step."""

        if not requested_by.strip() or not idempotency_key.strip():
            raise ValueError("requested_by and idempotency_key cannot be empty")
        source = self._client.get_workflow_handle(source_workflow_id)
        context = await source.query(
            HotNewsDataLoopWorkflow.activation_recovery_context,
            result_type=DataLoopActivationRecoveryContext,
        )
        if context.tenant_id != tenant_id:
            raise DataLoopWorkflowAccessError("Data Loop workflow was not found")
        if (
            context.phase not in {"record_promotion_decision", "activate_bundle"}
            or context.gate_passed is not True
            or context.evaluation_run_id is None
            or context.decision is None
            or context.decision.action != "approve"
        ):
            raise DataLoopActivationRecoveryNotAllowedError(
                "Data Loop workflow has no approved activation to recover"
            )

        request = DataLoopActivationRecoveryRequest(
            tenant_id=tenant_id,
            source_workflow_id=source_workflow_id,
            candidate_id=context.candidate_id,
            evaluation_run_id=context.evaluation_run_id,
            approval_decision=context.decision,
            requested_by=requested_by,
            idempotency_key=idempotency_key,
        )
        request.validate()
        recovery_workflow_id = self.activation_recovery_workflow_id(request)
        await self._start_idempotent(
            workflow=HotNewsDataLoopActivationRecoveryWorkflow.run,
            request=request,
            workflow_id=recovery_workflow_id,
            request_fingerprint=self.activation_recovery_fingerprint(request),
        )
        return recovery_workflow_id

    @staticmethod
    def activation_recovery_workflow_id(
        request: DataLoopActivationRecoveryRequest,
    ) -> str:
        digest = sha256(
            (
                f"{request.tenant_id}\x1f{request.source_workflow_id}"
                f"\x1f{request.idempotency_key}"
            ).encode("utf-8")
        ).hexdigest()
        return f"hot-news-data-loop-activation-recovery-{digest}"

    @staticmethod
    def activation_recovery_fingerprint(
        request: DataLoopActivationRecoveryRequest,
    ) -> str:
        request.validate()
        return DataLoopOrchestrator._fingerprint(
            {
                "tenant_id": request.tenant_id,
                "source_workflow_id": request.source_workflow_id,
                "candidate_id": request.candidate_id,
                "evaluation_run_id": request.evaluation_run_id,
                "approval_decision": {
                    "action": request.approval_decision.action,
                    "actor_id": request.approval_decision.actor_id,
                    "reason": request.approval_decision.reason,
                    "idempotency_key": request.approval_decision.idempotency_key,
                },
                "requested_by": request.requested_by,
                "idempotency_key": request.idempotency_key,
            }
        )

    async def _start_idempotent(
        self,
        *,
        workflow: Callable[..., Any],
        request: object,
        workflow_id: str,
        request_fingerprint: str,
    ) -> None:
        try:
            handle = await self._client.start_workflow(
                workflow,
                request,
                id=workflow_id,
                task_queue=self._task_queue,
                memo={
                    DATA_LOOP_REQUEST_FINGERPRINT_MEMO_KEY: request_fingerprint,
                },
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError as exc:
            # REJECT_DUPLICATE intentionally protects closed executions. A
            # same-content HTTP replay still succeeds after memo verification.
            handle = self._client.get_workflow_handle(
                workflow_id,
                run_id=exc.run_id,
            )

        description = await handle.describe()
        stored_fingerprint = await description.memo_value(
            DATA_LOOP_REQUEST_FINGERPRINT_MEMO_KEY,
            None,
        )
        if stored_fingerprint != request_fingerprint:
            raise DataLoopStartConflictError(
                "Data Loop idempotency key is already bound to different content"
            )

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        content = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return sha256(content).hexdigest()
