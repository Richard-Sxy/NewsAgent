"""Temporal admission ports and thin Activities for conversation async hooks.

This budget covers conversation writing/evaluation launches. Page/API launches
keep their existing ports. Redis and expiring leases are not admission authority.
"""

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from uuid import UUID

from sqlalchemy import select
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus, WorkflowUpdateFailedError
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from app.modules.conversation.admission_contracts import (
    ADMISSION_BOOTSTRAP_LIMIT, ADMISSION_FINGERPRINT_MEMO, ADMISSION_TENANT_MEMO,
    AdmissionConflictError, AdmissionRequest, TenantAdmissionState,
    admission_fingerprint, request_admission_id, tenant_admission_id,
)
from app.modules.conversation.admission_workflows import AdmissionRequestWorkflow, TenantAdmissionWorkflow
from app.modules.writing.persistence.job import WritingJob
from app.modules.writing.job import JobService
from app.modules.writing.orchestrator import OrchestratorService
from app.modules.data_loop.orchestrator import DataLoopOrchestrator
from app.modules.data_loop.workflow_contracts import DataLoopRunRequest


TERMINAL_STATUSES = {
    WorkflowExecutionStatus.COMPLETED, WorkflowExecutionStatus.FAILED,
    WorkflowExecutionStatus.CANCELED, WorkflowExecutionStatus.TERMINATED, WorkflowExecutionStatus.TIMED_OUT,
}


def writing_fingerprint(job) -> str:
    return admission_fingerprint({"tenant_id": str(job.tenant_id), "job_id": str(job.id),
        "workflow_id": job.temporal_workflow_id, "topic": job.topic,
        "requirements": job.requirements, "scenario": getattr(job.scenario, "value", job.scenario)})


def evaluation_payload(request: DataLoopRunRequest) -> dict:
    request.validate()
    payload = asdict(request)
    for field in ("window_start", "window_end"):
        payload[field] = payload[field].astimezone(timezone.utc).isoformat()
    return payload


def evaluation_request(payload: dict) -> DataLoopRunRequest:
    values = dict(payload)
    for field in ("window_start", "window_end"):
        values[field] = datetime.fromisoformat(values[field])
    result = DataLoopRunRequest(**values)
    result.validate()
    return result


def admission_result(snapshot: dict, *, tenant_id: str, business_workflow_id: str) -> dict:
    admission = {key: snapshot.get(key) for key in (
        "state", "queue_position", "active_count", "budget", "bootstrap_complete", "error_code")}
    admission["request_workflow_id"] = request_admission_id(tenant_id, business_workflow_id)
    result = {"workflow_id": business_workflow_id,
        "workflow_accepted": bool(snapshot.get("workflow_accepted")),
        "admission_accepted": bool(snapshot.get("admission_accepted")), "admission": admission}
    if snapshot.get("evaluation_policy_version") is not None:
        result["evaluation_policy_version"] = snapshot["evaluation_policy_version"]
    return result


class AdmissionService:
    def __init__(self, *, client, settings=None, budget: int = 2, queue_limit: int = 100):
        self.client = client
        self.task_queue = getattr(settings, "temporal_task_queue", "news-writing")
        self.budget = getattr(settings, "conversation_async_admission_budget", budget)
        self.queue_limit = getattr(settings, "conversation_async_admission_queue_limit", queue_limit)

    async def start_writing(self, job) -> dict:
        request = AdmissionRequest(str(job.tenant_id), "writing", job.temporal_workflow_id,
            writing_fingerprint(job), {"job_id": str(job.id)}, self.budget, self.queue_limit)
        return await self._start(request)

    async def start_evaluation(self, request: DataLoopRunRequest) -> dict:
        admission = AdmissionRequest(request.tenant_id, "evaluation", DataLoopOrchestrator.workflow_id(request),
            DataLoopOrchestrator.request_fingerprint(request), evaluation_payload(request), self.budget, self.queue_limit)
        return await self._start(admission)

    async def _start(self, request: AdmissionRequest) -> dict:
        request.validate()
        async with asyncio.timeout(10):
            try:
                handle = await self.client.start_workflow(AdmissionRequestWorkflow.run, request,
                    id=request.request_workflow_id, task_queue=self.task_queue,
                    id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    memo={ADMISSION_FINGERPRINT_MEMO: request.fingerprint, ADMISSION_TENANT_MEMO: request.tenant_id})
            except WorkflowAlreadyStartedError as exc:
                handle = self.client.get_workflow_handle(request.request_workflow_id, run_id=exc.run_id)
            description = await handle.describe()
            await self._verify(description, request.tenant_id, request.fingerprint)
            if description.status == WorkflowExecutionStatus.RUNNING:
                snapshot = await handle.execute_update(AdmissionRequestWorkflow.accepted, request.tenant_id,
                    id="admission-accepted", result_type=dict, rpc_timeout=timedelta(seconds=10))
            else:
                snapshot = await handle.query(AdmissionRequestWorkflow.snapshot, request.tenant_id, result_type=dict)
            return admission_result(snapshot, tenant_id=request.tenant_id, business_workflow_id=request.business_workflow_id)

    @staticmethod
    async def _verify(description, tenant_id, fingerprint=None):
        if await description.memo_value(ADMISSION_TENANT_MEMO, None) != tenant_id:
            raise AdmissionConflictError("admission_tenant_mismatch")
        if fingerprint is not None and await description.memo_value(ADMISSION_FINGERPRINT_MEMO, None) != fingerprint:
            raise AdmissionConflictError("admission_request_conflict")

    async def status(self, *, tenant_id: str, business_workflow_id: str) -> dict:
        request_id = request_admission_id(tenant_id, business_workflow_id)
        handle = self.client.get_workflow_handle(request_id)
        try:
            description = await handle.describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return {"not_found": True}
            raise
        await self._verify(description, tenant_id)
        snapshot = await handle.query(AdmissionRequestWorkflow.snapshot, tenant_id, result_type=dict)
        coordinator = self.client.get_workflow_handle(tenant_admission_id(tenant_id))
        try:
            current = await coordinator.query(TenantAdmissionWorkflow.snapshot, request_id, result_type=dict)
            snapshot.update({key: current.get(key) for key in ("active_count", "budget", "bootstrap_complete")})
            if snapshot.get("state") == "queued" and current.get("state") in {"queued", "granted"}:
                snapshot.update(state=current["state"], queue_position=current["queue_position"])
        except RPCError:
            snapshot.update(active_count=None, error_code="admission_coordinator_unavailable")
        return admission_result(snapshot, tenant_id=tenant_id, business_workflow_id=business_workflow_id)

    async def verify_request(self, *, tenant_id: str, business_workflow_id: str) -> dict:
        """Describe and verify a durable request without requiring worker queries."""
        request_id = request_admission_id(tenant_id, business_workflow_id)
        try:
            description = await self.client.get_workflow_handle(request_id).describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return {"not_found": True}
            raise
        await self._verify(description, tenant_id)
        return {"request_workflow_id": request_id, "not_found": False}


class AdmissionActivities:
    def __init__(self, *, client, database, settings, writing=None, evaluation=None):
        self.client, self.database, self.settings = client, database, settings
        self.writing = writing or OrchestratorService(client, settings)
        self.evaluation = evaluation or DataLoopOrchestrator(client, settings)
        self.task_queue = settings.temporal_task_queue

    @property
    def registered(self):
        return [self.bootstrap, self.enqueue, self.describe_many, self.dispatch]

    async def _describe(self, workflow_id):
        try:
            description = await self.client.get_workflow_handle(workflow_id).describe()
            state = ("running" if description.status == WorkflowExecutionStatus.RUNNING
                     else "terminal" if description.status in TERMINAL_STATUSES else "unknown")
            return {"workflow_id": workflow_id, "state": state,
                    "business_status": description.status.name.lower()}
        except RPCError as exc:
            return {"workflow_id": workflow_id,
                    "state": "missing" if exc.status == RPCStatusCode.NOT_FOUND else "unknown"}
        except Exception:
            return {"workflow_id": workflow_id, "state": "unknown"}

    @activity.defn(name="conversation_admission_bootstrap")
    async def bootstrap(self, tenant_id: str) -> list[str]:
        async with self.database.session() as session:
            result = await session.execute(select(WritingJob).where(WritingJob.tenant_id == UUID(tenant_id),
                WritingJob.idempotency_key.like("conversation-writing:%")).limit(ADMISSION_BOOTSTRAP_LIMIT + 1))
            rows = list(result.scalars().all())
        if len(rows) > ADMISSION_BOOTSTRAP_LIMIT:
            raise ApplicationError("admission_bootstrap_limit_exceeded", non_retryable=True)
        observed = await self.describe_many([row.temporal_workflow_id for row in rows])
        if any(item["state"] == "unknown" for item in observed):
            raise ApplicationError("admission_bootstrap_incomplete", non_retryable=True)
        return [item["workflow_id"] for item in observed if item["state"] == "running"]

    @activity.defn(name="conversation_admission_enqueue")
    async def enqueue(self, request: AdmissionRequest) -> dict:
        request.validate()
        handle = await self.client.start_workflow(TenantAdmissionWorkflow.run,
            TenantAdmissionState(request.tenant_id, request.budget, request.queue_limit),
            id=request.coordinator_workflow_id, task_queue=self.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
        try:
            return await handle.execute_update(TenantAdmissionWorkflow.enqueue, request,
                id=request.request_workflow_id, result_type=dict, rpc_timeout=timedelta(seconds=10))
        except WorkflowUpdateFailedError as exc:
            reason = "admission_enqueue_rejected"
            message = str(exc.cause)
            for known in ("admission_queue_full", "admission_policy_conflict", "admission_request_conflict",
                          "admission_tenant_mismatch"):
                if known in message:
                    reason = known
                    break
            raise ApplicationError(reason, type=reason, non_retryable=True) from exc

    @activity.defn(name="conversation_admission_describe_many")
    async def describe_many(self, workflow_ids: list[str]) -> list[dict]:
        if len(workflow_ids) > ADMISSION_BOOTSTRAP_LIMIT:
            raise ApplicationError("admission_describe_limit_exceeded", non_retryable=True)
        semaphore = asyncio.Semaphore(8)
        async def one(workflow_id):
            async with semaphore:
                return await self._describe(workflow_id)
        return list(await asyncio.gather(*(one(item) for item in workflow_ids)))

    @activity.defn(name="conversation_admission_dispatch")
    async def dispatch(self, request: AdmissionRequest) -> dict:
        request.validate()
        if request.kind == "writing":
            async with self.database.session() as session:
                job = await JobService().get(session, tenant_id=UUID(request.tenant_id), job_id=UUID(request.payload["job_id"]))
                if job.temporal_workflow_id != request.business_workflow_id or writing_fingerprint(job) != request.fingerprint:
                    raise ApplicationError("admission_request_conflict", non_retryable=True)
            existing = await self._describe(request.business_workflow_id)
            if existing["state"] in {"running", "terminal"}:
                return existing
            if existing["state"] == "unknown":
                raise ApplicationError("admission_business_state_unknown")
            await self.writing.start_job(job)
        else:
            evaluation = evaluation_request(request.payload)
            if (evaluation.tenant_id != request.tenant_id
                    or DataLoopOrchestrator.workflow_id(evaluation) != request.business_workflow_id
                    or DataLoopOrchestrator.request_fingerprint(evaluation) != request.fingerprint):
                raise ApplicationError("admission_request_conflict", non_retryable=True)
            await self.evaluation.start(evaluation)
        return {"workflow_id": request.business_workflow_id, "state": "running"}
