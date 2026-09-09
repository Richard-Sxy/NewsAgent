"""API-facing Temporal client for bounded Data Loop runs."""

from __future__ import annotations

from hashlib import sha256

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from app.config import Settings
from app.workflows.data_loop import HotNewsDataLoopWorkflow
from app.workflows.data_loop_contracts import (
    DataLoopHumanDecision,
    DataLoopRunRequest,
    DataLoopWorkflowSnapshot,
)


class DataLoopDecisionNotAllowedError(ValueError):
    pass


class DataLoopWorkflowAccessError(LookupError):
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

    async def start(self, request: DataLoopRunRequest) -> str:
        request.validate()
        workflow_id = self.workflow_id(request)
        await self._client.start_workflow(
            HotNewsDataLoopWorkflow.run,
            request,
            id=workflow_id,
            task_queue=self._task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
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
    ) -> None:
        decision.validate()
        handle = self._client.get_workflow_handle(workflow_id)
        snapshot = await handle.query(
            HotNewsDataLoopWorkflow.snapshot,
            result_type=DataLoopWorkflowSnapshot,
        )
        if snapshot.tenant_id != tenant_id:
            raise DataLoopWorkflowAccessError("Data Loop workflow was not found")
        if not snapshot.waiting_for_approval or snapshot.gate_passed is not True:
            raise DataLoopDecisionNotAllowedError(
                "Data Loop workflow is not waiting at a passed evaluation gate"
            )
        await handle.signal(
            HotNewsDataLoopWorkflow.submit_promotion_decision,
            decision,
        )
