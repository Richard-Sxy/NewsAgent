from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from app.analytics.data_source import BehaviorDataSource
from app.analytics.news_content import NewsContentRepository
from app.clients.knowledge_base import KnowledgeSearchClient
from app.model_runtime.core import InferencePort, PromptRegistry
from app.model_runtime.config_file import (
    load_model_runtime_config,
    validate_runtime_environment,
)
from app.model_runtime.factory import ModelRuntimePorts, build_model_runtime_ports
from app.model_runtime.knowledge_factory import build_native_knowledge_search, build_native_knowledge_index
from app.knowledge.search_factory import close_knowledge_search_index
from app.observability.hot_news import HotNewsRunMetrics
from app.config import Settings, get_settings
from app.db.session import Database
from app.hot_news_bootstrap import create_hot_news_worker_runtime
from app.hot_news_dependencies import build_hot_news_dependencies
from app.services.hot_news_orchestration import (
    HotNewsBaselineProvider,
    HotNewsOrchestrationPolicy,
)
from app.services.active_bundle_hot_news import HotNewsRunDependenciesFactory
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
    inference_port: InferencePort | None = None,
    prompt_registry: PromptRegistry | None = None,
    run_dependencies_factory: HotNewsRunDependenciesFactory | None = None,
) -> None:
    """ 注册热点 Workflow 和 Activity,并持续消费热点任务 """
    resolved_settings = settings or get_settings()
    native_ports: ModelRuntimePorts | None = None
    knowledge_database: Database | None = None
    knowledge_index = None
    allowed_model_routes: tuple[str, ...] | None = None
    runtime = None
    try:
        if resolved_settings.model_runtime_config_path is None:
            raise ValueError("MODEL_RUNTIME_CONFIG_PATH is required")
        config = load_model_runtime_config(resolved_settings.model_runtime_config_path)
        validate_runtime_environment(config, environment=resolved_settings.environment)
        allowed_model_routes = config.inference.model_routes
        if inference_port is None:
            native_ports = build_model_runtime_ports(
                config, environment=resolved_settings.environment
            )
            inference_port = native_ports.inference
        if knowledge_search is None:
            if native_ports is None:
                native_ports = build_model_runtime_ports(
                    config, environment=resolved_settings.environment
                )
            knowledge_database = Database(resolved_settings)
            knowledge_index = build_native_knowledge_index(
                config=config, embedding=native_ports.embedding, database=knowledge_database,
                config_path=resolved_settings.knowledge_search_config_path,
            )
            knowledge_search = build_native_knowledge_search(
                config=config,
                embedding=native_ports.embedding,
                database=knowledge_database,
                index=knowledge_index,
            )
        prompt_registry = prompt_registry or config.prompt_registry()
        runtime = create_hot_news_worker_runtime(
            resolved_settings,
            behavior_data_source=behavior_data_source,
            baseline_provider=baseline_provider,
            content_repository=content_repository,
            policy=policy,
            knowledge_search=knowledge_search,
            run_metrics=run_metrics,
            inference_port=inference_port,
            prompt_registry=prompt_registry,
            allowed_model_routes=allowed_model_routes,
            run_dependencies_factory=run_dependencies_factory,
        )
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
        await close_knowledge_search_index(knowledge_index)
        if runtime is not None:
            await runtime.close()
        if native_ports is not None:
            await native_ports.close()
        if knowledge_database is not None:
            await knowledge_database.close()

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
