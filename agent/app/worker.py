import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from app.modules.writing.news_steps import NewsStepActivities, StateHandler, StepHandler
from app.bootstrap import create_worker_runtime
from app.observability.process import telemetry_process
from app.observability.temporal import temporal_interceptors
from app.config import Settings, get_settings
from app.modules.writing.news_writing import NewsWritingWorkflow
from app.modules.conversation.admission import AdmissionActivities
from app.modules.conversation.admission_workflows import ADMISSION_WORKFLOWS
from app.db.session import Database

""" Temporal 任务执行 Worker """
@telemetry_process("newsagent-writing-worker")
async def run_worker(
    handler: StepHandler | None = None,
    state_handler: StateHandler | None = None,
    settings: Settings | None = None,
    admission_activities: AdmissionActivities | None = None,
) -> None:
    """注册 Workflow 与 Activity; 部署时每个 Pod 只启动一个 Worker。"""
    resolved = settings or get_settings()
    runtime = None if handler is not None else create_worker_runtime(resolved)
    resolved_handler = handler or runtime.handler
    # 调用 Temporal 的 Client
    client = await Client.connect(
        resolved.temporal_address,
        namespace=resolved.temporal_namespace,
        interceptors=temporal_interceptors(),
    )
    resolved_state_handler = state_handler or (runtime.state_handler if runtime else None)
    activities = NewsStepActivities(resolved_handler, resolved_state_handler)
    workflows = [NewsWritingWorkflow]
    registered = [activities.run_news_step, activities.transition_job_state]
    admission_database = None
    if resolved.conversation_async_admission_enabled:
        if admission_activities is None:
            admission_database = None if runtime is not None else Database(resolved)
            admission_activities = AdmissionActivities(client=client,
                database=runtime.database if runtime is not None else admission_database, settings=resolved)
        workflows.extend(ADMISSION_WORKFLOWS)
        registered.extend(admission_activities.registered)
    # 调用 Worker
    worker = Worker(
        client,
        task_queue=resolved.temporal_task_queue,
        workflows=workflows,
        activities=registered,
    )
    try:
        await worker.run()
    finally:
        if runtime is not None:
            await runtime.close()
        if admission_database is not None:
            await admission_database.close()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
