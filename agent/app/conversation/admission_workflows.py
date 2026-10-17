"""Durable FIFO admission; no lease timeout can release a running business task."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.conversation.admission_contracts import (
        ADMISSION_POLL_SECONDS, AdmissionRequest, AdmissionTicket, TenantAdmissionState,
    )


READ_RETRY = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=1))
START_RETRY = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=1))


def admission_error_code(exc, fallback):
    current = exc
    for _ in range(5):
        kind = getattr(current, "type", None)
        if kind in {"admission_queue_full", "admission_policy_conflict", "admission_request_conflict",
                    "admission_tenant_mismatch", "admission_enqueue_rejected"}:
            return kind
        current = getattr(current, "cause", None)
        if current is None:
            break
    return fallback


@workflow.defn(name="conversation-tenant-admission-v1")
class TenantAdmissionWorkflow:
    def __init__(self):
        self.state: TenantAdmissionState | None = None

    @workflow.run
    async def run(self, initial: TenantAdmissionState) -> None:
        self.state = initial
        cycles = 0
        while True:
            if not self.state.initialized:
                try:
                    existing = await workflow.execute_activity("conversation_admission_bootstrap",
                        self.state.tenant_id, result_type=list[str],
                        start_to_close_timeout=timedelta(seconds=60), retry_policy=READ_RETRY)
                    self.state.active.extend(AdmissionTicket("", item, "bootstrap") for item in existing
                        if not any(active.business_workflow_id == item for active in self.state.active))
                    self.state.initialized = True
                    self.state.bootstrap_error = None
                except Exception:
                    self.state.bootstrap_error = "admission_bootstrap_unavailable"
                    await workflow.sleep(15)
                    continue
            for ticket in self.state.grant():
                await self._signal(ticket, "granted", self.state.snapshot(ticket.request_workflow_id))
            if self.state.active:
                try:
                    observed = await workflow.execute_activity("conversation_admission_describe_many",
                        [ticket.business_workflow_id for ticket in self.state.active], result_type=list[dict],
                        start_to_close_timeout=timedelta(seconds=30), retry_policy=READ_RETRY)
                except Exception:
                    observed = []  # failed observation never releases a slot
                for observation in observed:
                    if observation["state"] == "terminal":
                        for ticket in self.state.complete(observation["workflow_id"]):
                            await self._signal(ticket, "observed", {
                                **self.state.snapshot(ticket.request_workflow_id), **observation})
                    elif observation["state"] == "running":
                        if self.state.observations.get(observation["workflow_id"]) == "running":
                            continue
                        self.state.observations[observation["workflow_id"]] = "running"
                        for ticket in self.state.active:
                            if ticket.business_workflow_id == observation["workflow_id"]:
                                await self._signal(ticket, "observed", {
                                    **self.state.snapshot(ticket.request_workflow_id), **observation})
            cycles += 1
            if cycles >= 200:
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new(self.state)
            if not self.state.active and not self.state.pending:
                await workflow.wait_condition(lambda: bool(self.state.pending))
                continue
            try:
                await workflow.wait_condition(lambda: bool(self.state.pending) and len(self.state.active) < self.state.budget,
                    timeout=timedelta(seconds=ADMISSION_POLL_SECONDS))
            except TimeoutError:
                pass

    async def _signal(self, ticket: AdmissionTicket, name: str, snapshot: dict):
        if not ticket.request_workflow_id:
            return
        try:
            await workflow.get_external_workflow_handle(ticket.request_workflow_id).signal(name, snapshot)
        except Exception:
            # Missing/externally terminated admission executors never justify
            # releasing an active slot. Business describe remains authoritative.
            pass

    @workflow.update
    async def enqueue(self, request: AdmissionRequest) -> dict:
        await workflow.wait_condition(lambda: self.state is not None)
        return self.state.enqueue(request)

    @workflow.query
    def snapshot(self, request_workflow_id: str = "") -> dict:
        return self.state.snapshot(request_workflow_id) if self.state else {"state": "initializing"}


@workflow.defn(name="conversation-admission-request-v1")
class AdmissionRequestWorkflow:
    def __init__(self):
        self.request: AdmissionRequest | None = None
        self._snapshot: dict = {"state": "submitted", "workflow_accepted": False, "admission_accepted": False}
        self._granted = False
        self._terminal = False
        self._accepted = False

    @workflow.run
    async def run(self, request: AdmissionRequest) -> dict:
        request.validate()
        self.request = request
        try:
            queued = await workflow.execute_activity("conversation_admission_enqueue", request, result_type=dict,
                start_to_close_timeout=timedelta(seconds=15), retry_policy=READ_RETRY)
            if not self._granted:
                self._snapshot.update(queued)
                self._granted = queued.get("state") == "granted"
            self._snapshot["admission_accepted"] = True
            self._accepted = True
        except Exception as exc:
            self._snapshot.update(state="rejected", error_code=admission_error_code(exc, "admission_enqueue_unavailable"))
            self._accepted = True
            return self.snapshot(request.tenant_id)
        await workflow.wait_condition(lambda: self._granted)
        self._snapshot["state"] = "starting"
        try:
            result = await workflow.execute_activity("conversation_admission_dispatch", request, result_type=dict,
                start_to_close_timeout=timedelta(seconds=15), retry_policy=START_RETRY)
            self._snapshot["workflow_accepted"] = True
            self._snapshot["state"] = "terminal" if result["state"] == "terminal" or self._terminal else "running"
            self._terminal = result["state"] == "terminal" or self._terminal
        except Exception:
            if not self._terminal and not self._snapshot.get("workflow_accepted"):
                self._snapshot.update(state="unknown", error_code="admission_start_uncertain")
            # Exactly one start retry is allowed. Only the coordinator's
            # subsequent read-only describe can resolve this uncertainty.
        await workflow.wait_condition(lambda: self._terminal)
        await workflow.wait_condition(workflow.all_handlers_finished)
        return self.snapshot(request.tenant_id)

    @workflow.signal
    def granted(self, snapshot: dict):
        self._granted = True
        self._snapshot.update(snapshot)

    @workflow.signal
    def observed(self, snapshot: dict):
        self._snapshot.update({key: value for key, value in snapshot.items() if key != "state"})
        if snapshot["state"] in {"running", "terminal"}:
            self._snapshot["workflow_accepted"] = True
            self._snapshot["state"] = snapshot["state"]
            self._snapshot["error_code"] = None
            self._terminal = snapshot["state"] == "terminal"

    @workflow.update
    async def accepted(self, tenant_id: str) -> dict:
        await workflow.wait_condition(lambda: self._accepted)
        return self.snapshot(tenant_id)

    @workflow.query
    def snapshot(self, tenant_id: str) -> dict:
        if self.request is not None and tenant_id != self.request.tenant_id:
            raise ValueError("admission_tenant_mismatch")
        result = dict(self._snapshot)
        if self.request is not None and self.request.kind == "evaluation":
            result["evaluation_policy_version"] = self.request.payload["evaluation_policy_version"]
        return result


ADMISSION_WORKFLOWS = [TenantAdmissionWorkflow, AdmissionRequestWorkflow]
