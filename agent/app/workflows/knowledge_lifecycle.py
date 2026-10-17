"""Small durable workflows; payloads contain IDs and budgets, never article text."""
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy


def retry_policy(value):
    return RetryPolicy(maximum_attempts=value["max_attempts"],
        initial_interval=timedelta(seconds=value["retry_initial_interval_seconds"]),
        maximum_interval=timedelta(seconds=value["retry_max_interval_seconds"]))


@workflow.defn
class KnowledgeIndexWorkflow:
    @workflow.run
    async def run(self, value: dict) -> dict:
        return await workflow.execute_activity("process_knowledge_index_job",
            {"tenant_id": value["tenant_id"], "job_id": value["job_id"]},
            start_to_close_timeout=timedelta(seconds=value["activity_timeout_seconds"]),
            schedule_to_close_timeout=timedelta(seconds=value["workflow_timeout_seconds"]),
            retry_policy=retry_policy(value))


@workflow.defn
class KnowledgeReconcileWorkflow:
    @workflow.run
    async def run(self, value: dict) -> dict:
        cursor = value.get("after_key")
        # Preserve replay determinism for a pre-upgrade workflow already using
        # the 100-page branch. Fresh runs checkpoint each successful page so the
        # run timeout cannot keep a large scan trapped in its first pages.
        page_limit = 1 if workflow.patched("knowledge-reconcile-page-checkpoint-v1") else 100
        for _ in range(page_limit):
            cursor = await workflow.execute_activity("scan_knowledge_lifecycle_page",
                {"after_key": cursor},
                start_to_close_timeout=timedelta(seconds=value["activity_timeout_seconds"]),
                schedule_to_close_timeout=timedelta(seconds=value["workflow_timeout_seconds"]),
                retry_policy=retry_policy(value))
            if cursor is None:
                return {"status": "complete"}
        workflow.continue_as_new({**value, "after_key": cursor})
