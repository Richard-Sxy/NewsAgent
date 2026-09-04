from dataclasses import dataclass

from app.clients.fastgpt import FastGPTClient
from app.config import Settings
from app.db.session import Database
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
    fastgpt_client: FastGPTClient

    async def close(self) -> None:
        await self.fastgpt_client.close()
        await self.database.close()


def create_worker_runtime(settings: Settings) -> WorkerRuntime:
    """在 Worker 进程启动时集中装配全部生产依赖。"""
    database = Database(settings)
    fastgpt_client = FastGPTClient(settings)
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
            fastgpt_client, settings.fastgpt_research_app_id
        ),
        writer_runner=WriterAgentRunner(
            fastgpt_client, settings.fastgpt_writer_app_id
        ),
        reviewer_runner=ReviewerAgentRunner(
            fastgpt_client, settings.fastgpt_reviewer_app_id
        ),
        research_app_id=settings.fastgpt_research_app_id,
        writer_app_id=settings.fastgpt_writer_app_id,
        reviewer_app_id=settings.fastgpt_reviewer_app_id,
    )
    return WorkerRuntime(
        handler,
        JobStateHandler(database, outbox_service),
        database,
        fastgpt_client,
    )
