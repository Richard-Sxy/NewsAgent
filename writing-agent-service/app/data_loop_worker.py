"""Temporal Worker dedicated to bounded Data Loop workflows."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from temporalio.client import Client
from temporalio.worker import Worker

from app.activities.data_loop import DataLoopActivities, DataLoopStepHandler
from app.clients.fastgpt import FastGPTClient
from app.config import Settings, get_settings
from app.db.session import Database
from app.services.agents.error_attribution import ErrorAttributionAgentRunner
from app.services.data_loop.artifacts import S3DataLoopArtifactStore
from app.services.data_loop.dataset_freezer import (
    S3EvaluationDatasetArtifactStore,
)
from app.services.data_loop.offline_replay import (
    HotNewsOfflineReplayService,
)
from app.services.data_loop.step_handler import HotNewsDataLoopStepHandler
from app.services.production_bundle_runtime import (
    ProductionBundleRuntimeRegistry,
)
from app.workflows.data_loop import (
    HotNewsDataLoopActivationRecoveryWorkflow,
    HotNewsDataLoopWorkflow,
)


@dataclass(slots=True)
class DataLoopWorkerRuntime:
    handler: HotNewsDataLoopStepHandler
    database: Database
    fastgpt_client: FastGPTClient

    async def close(self) -> None:
        await self.fastgpt_client.close()
        await self.database.close()


def create_data_loop_worker_runtime(settings: Settings) -> DataLoopWorkerRuntime:
    database = Database(settings)
    fastgpt = FastGPTClient(settings)
    attribution = (
        ErrorAttributionAgentRunner(
            fastgpt,
            settings.fastgpt_error_attribution_app_id,
        )
        if settings.fastgpt_error_attribution_app_id
        else None
    )
    runtime_registry = ProductionBundleRuntimeRegistry.from_json(
        fastgpt,
        settings.hot_news_runtime_manifest_json,
    )
    handler = HotNewsDataLoopStepHandler(
        database=database,
        dataset_artifact_store=S3EvaluationDatasetArtifactStore(settings),
        data_loop_artifact_store=S3DataLoopArtifactStore(settings),
        offline_replay=HotNewsOfflineReplayService(
            runtime_registry,
            max_concurrency=settings.data_loop_replay_max_concurrency,
            max_cases_per_cohort=settings.data_loop_max_cases_per_cohort,
        ),
        error_attribution_runner=attribution,
        max_cases_per_cohort=settings.data_loop_max_cases_per_cohort,
    )
    return DataLoopWorkerRuntime(
        handler=handler,
        database=database,
        fastgpt_client=fastgpt,
    )


async def run_data_loop_worker(
    handler: DataLoopStepHandler | None = None,
    *,
    settings: Settings | None = None,
) -> None:
    """Register bounded Data Loop Workflows and their thin Activity."""

    resolved = settings or get_settings()
    runtime = (
        None
        if handler is not None
        else create_data_loop_worker_runtime(resolved)
    )
    resolved_handler = handler or runtime.handler
    try:
        client = await Client.connect(
            resolved.temporal_address,
            namespace=resolved.temporal_namespace,
        )
        activities = DataLoopActivities(resolved_handler)
        worker = Worker(
            client,
            task_queue=resolved.temporal_data_loop_task_queue,
            workflows=[
                HotNewsDataLoopWorkflow,
                HotNewsDataLoopActivationRecoveryWorkflow,
            ],
            activities=[activities.run_data_loop_step],
        )
        await worker.run()
    finally:
        if runtime is not None:
            await runtime.close()


def main() -> None:
    asyncio.run(run_data_loop_worker())


if __name__ == "__main__":
    main()
