import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from app.activities.news_steps import NewsStepActivities, StateHandler, StepHandler
from app.bootstrap import create_worker_runtime
from app.config import Settings, get_settings
from app.workflows.news_writing import NewsWritingWorkflow

""" Temporal 任务执行 Worker """
async def run_worker(
    handler: StepHandler | None = None,
    state_handler: StateHandler | None = None,
    settings: Settings | None = None,
) -> None:
    """注册 Workflow 与 Activity；部署时每个 Pod 只启动一个 Worker。"""
    resolved = settings or get_settings()
    runtime = None if handler is not None else create_worker_runtime(resolved)
    resolved_handler = handler or runtime.handler
    # 调用 Temporal 的 Client
    client = await Client.connect(
        resolved.temporal_address,
        namespace=resolved.temporal_namespace,
    )
    resolved_state_handler = state_handler or (runtime.state_handler if runtime else None)
    activities = NewsStepActivities(resolved_handler, resolved_state_handler)
    # 调用 Worker
    worker = Worker(
        client,
        task_queue=resolved.temporal_task_queue,
        workflows=[NewsWritingWorkflow],
        activities=[activities.run_news_step, activities.transition_job_state],
    )
    try:
        await worker.run()
    finally:
        if runtime is not None:
            await runtime.close()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
