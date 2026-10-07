"""Temporal Worker dedicated to bounded Data Loop workflows."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from temporalio.client import Client
from temporalio.worker import Worker

from app.activities.data_loop import DataLoopActivities, DataLoopStepHandler
from app.config import Settings, get_settings
from app.data_analysis.factory import create_analysis_runner
from app.db.session import Database
from app.model_runtime.agent_client import NativeStructuredAgentClient
from app.model_runtime.bundle_runtime import NativeProductionBundleRuntimeRegistry
from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.core import StructuredInferenceService
from app.model_runtime.factory import ModelRuntimePorts, build_model_runtime_ports
from app.services.agents.error_attribution import ErrorAttributionAgentRunner
from app.services.data_loop.artifacts import S3DataLoopArtifactStore
from app.services.data_loop.dataset_analysis import DataLoopDatasetAnalysisService
from app.services.data_loop.dataset_freezer import (
    S3EvaluationDatasetArtifactStore,
)
from app.services.data_loop.offline_replay import (
    HotNewsOfflineReplayService,
)
from app.services.data_loop.step_handler import HotNewsDataLoopStepHandler
from app.services.hot_news_query import HotNewsQueryService
from app.workflows.data_loop import (
    HotNewsDataLoopActivationRecoveryWorkflow,
    HotNewsDataLoopWorkflow,
)


@dataclass(slots=True)
class DataLoopWorkerRuntime:
    handler: HotNewsDataLoopStepHandler
    database: Database
    model_ports: ModelRuntimePorts
    analysis_runner: object | None = None

    async def close(self) -> None:
        try:
            if self.analysis_runner is not None and hasattr(self.analysis_runner, "close"):
                await self.analysis_runner.close()
        finally:
            try:
                await self.model_ports.close()
            finally:
                await self.database.close()


def create_data_loop_worker_runtime(settings: Settings) -> DataLoopWorkerRuntime:
    if settings.model_runtime_backend != "native":
        raise ValueError("Data Loop Worker requires the Python model runtime")
    if settings.model_runtime_config_path is None:
        raise ValueError("MODEL_RUNTIME_CONFIG_PATH is required")
    model_config = load_model_runtime_config(settings.model_runtime_config_path)
    model_config.agent_scene("hot_news_error_attribution")
    model_ports = build_model_runtime_ports(model_config, environment=settings.environment)
    database = Database(settings)
    agent_client = NativeStructuredAgentClient(
        StructuredInferenceService(
            inference=model_ports.inference,
            prompts=model_ports.prompts,
        ),
        model_config,
    )
    attribution = ErrorAttributionAgentRunner(
        agent_client,
        "python:error-attribution",
    )
    runtime_registry = NativeProductionBundleRuntimeRegistry.from_json(
        model_ports.inference,
        model_ports.prompts,
        settings.hot_news_runtime_manifest_json,
        allowed_model_routes=model_config.inference.model_routes,
    )
    analysis_runner = create_analysis_runner(
        settings, enabled=settings.data_loop_data_analysis_enabled,
    )
    dataset_analysis = (
        None if analysis_runner is None else DataLoopDatasetAnalysisService(
            source_runs=HotNewsQueryService(database=database),
            runner=analysis_runner,
            max_cases=settings.data_loop_max_cases_per_cohort,
        )
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
        dataset_analysis=dataset_analysis,
        max_cases_per_cohort=settings.data_loop_max_cases_per_cohort,
    )
    return DataLoopWorkerRuntime(
        handler=handler,
        database=database,
        model_ports=model_ports,
        analysis_runner=analysis_runner,
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
