"""A bounded diagnostic workflow, isolated from all NewsAgent business queues."""
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.observability.telemetry import operation


@activity.defn(name="observability_contract_step_v1")
async def observability_contract_step(private_input: str) -> dict:
    with operation("diagnostic.model.boundary"):
        attempt = activity.info().attempt
        if attempt == 1:
            raise ApplicationError("SECRET_MODEL_FAILURE", type="DiagnosticRetry")
        return {"attempt": attempt, "result": "completed"}


@workflow.defn(name="observability_contract_workflow_v1")
class ObservabilityContractWorkflow:
    @workflow.run
    async def run(self, private_input: str) -> dict:
        result = await workflow.execute_activity(
            observability_contract_step, private_input,
            start_to_close_timeout=timedelta(seconds=5),
            retry_policy=RetryPolicy(initial_interval=timedelta(milliseconds=50),
                maximum_interval=timedelta(milliseconds=50), maximum_attempts=2),
        )
        # Force another durable workflow task before completion and replay.
        await workflow.sleep(timedelta(milliseconds=10))
        return result
