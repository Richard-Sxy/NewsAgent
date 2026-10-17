"""Opt-in durable queue/restart acceptance using an isolated local Temporal queue."""

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.worker import Worker, Replayer

from app.conversation.admission import AdmissionActivities, AdmissionService
from app.conversation.admission_contracts import AdmissionRequest, request_admission_id, tenant_admission_id
from app.conversation.admission_workflows import ADMISSION_WORKFLOWS
from tests.test_conversation_admission import job


pytestmark = [pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("NEWS_AGENT_ADMISSION_TEMPORAL_TEST") != "1",
                       reason="Enable only against the isolated local Temporal dependency stack")]


@workflow.defn(name="conversation-admission-test-business-v1", sandboxed=False)
class ProbeBusinessWorkflow:
    def __init__(self):
        self.finished = False

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(lambda: self.finished)
        return "completed"

    @workflow.signal
    def finish(self):
        self.finished = True


class ProbeAdmissionActivities(AdmissionActivities):
    @activity.defn(name="conversation_admission_bootstrap")
    async def bootstrap(self, tenant_id: str) -> list[str]:
        return []

    @activity.defn(name="conversation_admission_dispatch")
    async def dispatch(self, request: AdmissionRequest) -> dict:
        await self.client.start_workflow(ProbeBusinessWorkflow.run,
            id=request.business_workflow_id, task_queue=self.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
        return {"workflow_id": request.business_workflow_id, "state": "running"}


async def until(service, row, desired, *, timeout=15):
    async with asyncio.timeout(timeout):
        while True:
            current = await service.status(tenant_id=str(row.tenant_id),
                                           business_workflow_id=row.temporal_workflow_id)
            if current.get("admission", {}).get("state") == desired:
                return current
            await asyncio.sleep(0.15)


@pytest.mark.asyncio
async def test_real_temporal_queue_survives_worker_restart_and_preserves_fifo():
    client = await Client.connect("temporal:7233", namespace="default")
    tenant = uuid4()
    task_queue = f"admission-test-{uuid4()}"
    settings = SimpleNamespace(temporal_task_queue=task_queue, temporal_data_loop_task_queue=task_queue)
    activities = ProbeAdmissionActivities(client=client, database=None, settings=settings,
        writing=SimpleNamespace(), evaluation=SimpleNamespace())
    service = AdmissionService(client=client, settings=settings)
    rows = [job() for _ in range(4)]
    for row in rows:
        row.tenant_id = tenant
        row.temporal_workflow_id = f"admission-test-business-{uuid4()}"
    def worker():
        return Worker(client, task_queue=task_queue,
            workflows=[*ADMISSION_WORKFLOWS, ProbeBusinessWorkflow], activities=activities.registered)
    identifiers = [tenant_admission_id(str(tenant)),
        *(request_admission_id(str(tenant), row.temporal_workflow_id) for row in rows),
        *(row.temporal_workflow_id for row in rows)]
    try:
        async with worker():
            await service.start_writing(rows[0])
            await service.start_writing(rows[1])
            await until(service, rows[0], "running")
            await until(service, rows[1], "running")
            third = await service.start_writing(rows[2])
            fourth = await service.start_writing(rows[3])
            assert third["admission_accepted"] is True and third["workflow_accepted"] is False
            assert third["admission"]["state"] == "queued" and third["admission"]["queue_position"] == 1
            assert fourth["admission"]["queue_position"] == 2
        # No worker process remains. Server history owns both queue and slots.
        async with worker():
            restored = await until(service, rows[2], "queued")
            assert restored["admission"]["active_count"] == 2
            await client.get_workflow_handle(rows[0].temporal_workflow_id).signal(ProbeBusinessWorkflow.finish)
            started = await until(service, rows[2], "running")
            assert started["workflow_accepted"] is True and started["admission"]["active_count"] == 2
            fourth = await until(service, rows[3], "queued")
            assert fourth["admission"]["queue_position"] == 1
            await client.get_workflow_handle(rows[1].temporal_workflow_id).signal(ProbeBusinessWorkflow.finish)
            await until(service, rows[3], "running")
            for row in rows[2:]:
                await client.get_workflow_handle(row.temporal_workflow_id).signal(ProbeBusinessWorkflow.finish)
                await until(service, row, "terminal")
            histories = [await client.get_workflow_handle(identifier).fetch_history()
                         for identifier in identifiers[:5]]
        await Replayer(workflows=ADMISSION_WORKFLOWS).replay_workflows(iter_history(histories))
    finally:
        for identifier in identifiers:
            try:
                await client.get_workflow_handle(identifier).terminate("isolated admission acceptance cleanup")
            except Exception:
                pass


async def iter_history(histories):
    for history in histories:
        yield history
