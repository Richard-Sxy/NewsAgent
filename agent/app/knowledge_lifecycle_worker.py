"""Temporal indexing Worker plus recoverable PG dispatch and reconciliation.

Start explicitly with KNOWLEDGE_LIFECYCLE_CONFIG_PATH; legacy workers retain
their original behavior. Pending jobs survive dispatch failure and restarts.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta
from hashlib import sha256
import logging

from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.worker import Worker

from app.observability.process import telemetry_process
from app.observability.temporal import temporal_interceptors
from app.config import get_settings
from app.db.session import Database
from app.modules.knowledge.tier_lifecycle_factory import build_lifecycle_runtime
from app.modules.knowledge.tier_lifecycle_config import KnowledgeLifecycleConfig
from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.factory import build_model_runtime_ports
from app.workflows.knowledge_lifecycle import KnowledgeIndexWorkflow, KnowledgeReconcileWorkflow

log = logging.getLogger(__name__)


def reconcile_workflow_id(task_queue: str) -> str:
    """One scanner per deployment queue, without exposing queue names in IDs."""
    return "knowledge-lifecycle-reconcile-" + sha256(task_queue.encode("utf-8")).hexdigest()


class LifecycleActivities:
    def __init__(self, service):
        self.service = service

    @activity.defn(name="process_knowledge_index_job")
    async def process(self, value: dict):
        try:
            return await self.service.process_job(**value)
        except Exception as exc:
            job = await self.service.ledger.get_job(**value)
            raise ApplicationError(type(exc).__name__,
                non_retryable=isinstance(exc, (ValueError, TypeError)) or bool(job and job["status"] == "failed")) from None

    @activity.defn(name="scan_knowledge_lifecycle_page")
    async def scan(self, value: dict):
        cursor = value.get("after_key")
        try:
            return await self.service.scan_page(after_key=tuple(cursor) if cursor else None)
        except Exception as exc:
            # Database/page failures still fail the activity and receive bounded
            # retries. SDK messages must not enter durable Temporal history.
            raise ApplicationError(type(exc).__name__,
                non_retryable=isinstance(exc, (ValueError, TypeError))) from None


async def dispatch_once(*, client, service, after_job_id=None):
    cfg = service.config
    rows = await service.ledger.list_pending(limit=cfg.scan_batch_size, after_job_id=after_job_id)
    for job in rows:
        # Completed obsolete projections are handled by the periodic sweep.
        if job["status"] == "complete":
            continue
        try:
            # Empty snapshots are valid for first-seen withdrawal tombstones;
            # older test/adapter rows may omit config. Real content jobs freeze
            # their entire policy, including queue and retry/timeout budgets.
            frozen = KnowledgeLifecycleConfig.model_validate(job["config"]) if job.get("config") else cfg
        except (ValueError, TypeError) as exc:
            await service.ledger.record_error(tenant_id=job["tenant_id"], job_id=job["job_id"],
                error="Dispatch" + type(exc).__name__, retryable=False)
            continue
        budgets = {key: getattr(frozen, key) for key in ("max_attempts", "retry_initial_interval_seconds",
            "retry_max_interval_seconds", "activity_timeout_seconds", "workflow_timeout_seconds")}
        workflow_id = f"knowledge-index-{job['job_id']}"
        try:
            await client.start_workflow(KnowledgeIndexWorkflow.run,
                {**budgets, "tenant_id": job["tenant_id"], "job_id": job["job_id"]},
                id=workflow_id, task_queue=frozen.task_queue,
                execution_timeout=timedelta(seconds=frozen.workflow_timeout_seconds),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
        except WorkflowAlreadyStartedError:
            description = await client.get_workflow_handle(workflow_id).describe()
            if description.status and description.status.name in {"FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"}:
                await service.ledger.record_error(tenant_id=job["tenant_id"], job_id=job["job_id"],
                    error="Workflow" + description.status.name, retryable=False)
    return rows[-1]["job_id"] if rows else None


async def dispatch_loop(*, client, service):
    cfg = service.config
    cursor, next_scan = None, 0.0
    while True:
        try:
            cursor = await dispatch_once(client=client, service=service, after_job_id=cursor)
            now = asyncio.get_running_loop().time()
            if now >= next_scan:
                budgets = {key: getattr(cfg, key) for key in ("max_attempts", "retry_initial_interval_seconds",
                    "retry_max_interval_seconds", "activity_timeout_seconds", "workflow_timeout_seconds")}
                try:
                    await client.start_workflow(KnowledgeReconcileWorkflow.run, budgets,
                        id=reconcile_workflow_id(cfg.task_queue), task_queue=cfg.task_queue,
                        run_timeout=timedelta(seconds=cfg.workflow_timeout_seconds),
                        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE)
                except WorkflowAlreadyStartedError:
                    pass
                next_scan = now + cfg.migration_interval_seconds
        except Exception as exc:
            # Pending rows are intentionally retained. No SDK message/credentials.
            log.error("knowledge dispatch will retry: %s", type(exc).__name__)
        await asyncio.sleep(cfg.dispatch_interval_seconds)


@telemetry_process("newsagent-knowledge-worker")
async def run():
    settings = get_settings()
    if not settings.knowledge_lifecycle_config_path or not settings.model_runtime_config_path:
        raise ValueError("KNOWLEDGE_LIFECYCLE_CONFIG_PATH and MODEL_RUNTIME_CONFIG_PATH are required")
    model = load_model_runtime_config(settings.model_runtime_config_path)
    database = Database(settings)
    ports, runtime = None, None
    try:
        ports = build_model_runtime_ports(model, environment=settings.environment)
        runtime = build_lifecycle_runtime(lifecycle_config_path=settings.knowledge_lifecycle_config_path,
            search_config_path=settings.knowledge_search_config_path, model_config=model,
            embedding=ports.embedding, database=database, environment=settings.environment)
        # Validate schema/index/load contracts before accepting queue work.
        await runtime.validate()
        client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace,
            interceptors=temporal_interceptors())
        activities = LifecycleActivities(runtime.service)
        worker = Worker(client, task_queue=runtime.service.config.task_queue,
            workflows=[KnowledgeIndexWorkflow, KnowledgeReconcileWorkflow],
            activities=[activities.process, activities.scan],
            max_concurrent_activities=1)
        dispatcher = asyncio.create_task(dispatch_loop(client=client, service=runtime.service))
        try:
            await worker.run()
        finally:
            dispatcher.cancel()
            with suppress(asyncio.CancelledError):
                await dispatcher
    finally:
        if runtime:
            await runtime.index.close()
        if ports:
            await ports.close()
        await database.close()


def main():
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()
