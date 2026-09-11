from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from app.analytics.data_source import BehaviorDataSource
from app.analytics.news_content import NewsContentRepository
from app.clients.knowledge_base import KnowledgeSearchClient
from app.observability.hot_news import HotNewsRunMetrics
from app.config import Settings, get_settings
from app.hot_news_bootstrap import create_hot_news_worker_runtime
from app.hot_news_dependencies import build_hot_news_dependencies
from app.services.hot_news_orchestration import (
    HotNewsBaselineProvider,
    HotNewsOrchestrationPolicy,
)
from app.workflows.hot_news import (
    HotNewsMonitorWorkflow,
    HotNewsWindowDispatcherWorkflow,
)

async def run_hot_news_worker(
    *,
    behavior_data_source: BehaviorDataSource,
    baseline_provider: HotNewsBaselineProvider,
    content_repository: NewsContentRepository,
    policy: HotNewsOrchestrationPolicy,
    knowledge_search: KnowledgeSearchClient | None = None,
    run_metrics: HotNewsRunMetrics | None = None,
    settings: Settings | None = None,
) -> None:
    """ 注册热点 Workflow 和 Activity,并持续消费热点任务 """
    resolved_settings = settings or get_settings()

    runtime = create_hot_news_worker_runtime(
        resolved_settings,
        behavior_data_source=behavior_data_source,
        baseline_provider=baseline_provider,
        content_repository=content_repository,
        policy=policy,
        knowledge_search=knowledge_search,
        run_metrics=run_metrics,
    )

    try:
        client = await Client.connect(
            resolved_settings.temporal_address,
            namespace=resolved_settings.temporal_namespace,
        )

        worker = Worker(
            client,
            task_queue=resolved_settings.temporal_hot_news_task_queue,
            workflows=[
                HotNewsMonitorWorkflow,
                HotNewsWindowDispatcherWorkflow,
            ],
            activities=[
                runtime.activities.run_hot_news_window,
            ],
        )

        await worker.run()
    finally:
        await runtime.close()

def main() -> None:
    """容器入口：解析依赖工厂并常驻消费 hot-news 队列。
       依赖装配失败时直接退出非 0，不降级、不返回到假数据。
    """
    settings = get_settings()
    dependencies = build_hot_news_dependencies(settings)
    asyncio.run(
        run_hot_news_worker(
            behavior_data_source=dependencies.behavior_data_source,
            baseline_provider=dependencies.baseline_provider,
            content_repository=dependencies.content_repository,
            policy=dependencies.policy,
            knowledge_search=getattr(dependencies, "knowledge_search", None),
            settings=settings,
        )
    )

if __name__ == "__main__":
    main()