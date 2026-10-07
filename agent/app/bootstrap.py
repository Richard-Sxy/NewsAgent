from dataclasses import dataclass

from app.config import Settings
from app.db.session import Database
from app.model_runtime.agent_client import NativeStructuredAgentClient
from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.core import StructuredInferenceService
from app.model_runtime.factory import ModelRuntimePorts, build_model_runtime_ports
from app.services.agents import ResearchAgentRunner, ReviewerAgentRunner, WriterAgentRunner
from app.services.artifact_pipeline import ArtifactPipeline
from app.services.checkpoint import CheckpointService
from app.services.execution import ExecutionService
from app.services.news_step_handler import NewsStepHandler
from app.services.job_state import JobStateHandler
from app.services.outbox import OutboxService
from app.storage.s3 import S3ArtifactStore


@dataclass
class WorkerRuntime:
    handler: NewsStepHandler
    state_handler: JobStateHandler
    database: Database
    model_ports: ModelRuntimePorts

    async def close(self) -> None:
        await self.model_ports.close()
        await self.database.close()


def create_worker_runtime(settings: Settings) -> WorkerRuntime:
    """在 Worker 进程启动时集中装配全部生产依赖。"""
    if settings.model_runtime_backend != "native":
        raise ValueError("writing Worker requires the Python model runtime")
    if settings.model_runtime_config_path is None:
        raise ValueError("MODEL_RUNTIME_CONFIG_PATH is required")
    model_config = load_model_runtime_config(settings.model_runtime_config_path)
    for scene in ("research", "outline", "section", "revise", "assemble", "review"):
        model_config.agent_scene(scene)
    model_ports = build_model_runtime_ports(model_config, environment=settings.environment)
    agent_client = NativeStructuredAgentClient(
        StructuredInferenceService(
            inference=model_ports.inference,
            prompts=model_ports.prompts,
        ),
        model_config,
    )
    database = Database(settings)
    artifact_store = S3ArtifactStore(settings)
    outbox_service = OutboxService()
    checkpoint_service = CheckpointService(outbox_service)
    pipeline = ArtifactPipeline(artifact_store, checkpoint_service)
    handler = NewsStepHandler(
        database=database,
        artifact_store=artifact_store,
        checkpoint_service=checkpoint_service,
        execution_service=ExecutionService(),
        artifact_pipeline=pipeline,
        research_runner=ResearchAgentRunner(
            agent_client, "python:research"
        ),
        writer_runner=WriterAgentRunner(
            agent_client, "python:writer"
        ),
        reviewer_runner=ReviewerAgentRunner(
            agent_client, "python:reviewer"
        ),
        research_app_id="python:research",
        writer_app_id="python:writer",
        reviewer_app_id="python:reviewer",
    )
    return WorkerRuntime(
        handler,
        JobStateHandler(database, outbox_service),
        database,
        model_ports,
    )
