from dataclasses import dataclass, field

from app.analytics.data_source import BehaviorDataSource
from app.analytics.hot_news_enrichment import HotNewsEnrichmentService
from app.analytics.news_content import NewsContentRepository
from app.clients.fastgpt import FastGPTClient
from app.clients.knowledge_base import (
    FastGPTKnowledgeSearchClient,
    KnowledgeSearchClient,
)
from app.config import Settings
from app.services.agents.hot_news import HotNewsAnalysisAgentRunner
from app.services.hot_news_analysis import (
    HotNewsAnalysisInputBuilder,
    HotNewsAnalysisService,
    HotNewsAnalysisValidator,
)
from app.services.memory_aware_hot_news_analysis import (
    MemoryAwareHotNewsAnalysisService,
)
from app.services.memory_context_application import (
    MemoryContextApplicationService,
)
from app.services.memory_prompt import MemoryPromptInputBuilder
from app.services.hot_news_orchestration import (
    HotNewsBaselineProvider,
    HotNewsOrchestrationPolicy,
    HotNewsOrchestrationService,
)
from app.activities.hot_news import HotNewsActivities
from app.db.session import Database
from app.observability.hot_news import (
    HotNewsRunMetrics,
    InMemoryHotNewsRunMetrics,
)
from app.services.hot_news_run_store import PostgresHotNewsRunStore
from app.services.data_loop.automatic_feedback import (
    AutomaticFeedbackPolicy,
    AutomaticHotNewsFeedbackSink,
)
from app.services.active_bundle_hot_news import (
    ActiveProductionBundleHotNewsService,
)
from app.services.production_bundle_runtime import (
    ProductionBundleRuntimeRegistry,
)

@dataclass
class HotNewsRuntime:
    """这边主要分为三个类：热点新闻执行类/调用FastGPT大模型接口/调用知识库检索"""
    service: HotNewsOrchestrationService
    memory_aware_analysis_service: MemoryAwareHotNewsAnalysisService
    fastgpt_client: FastGPTClient
    knowledge_client: KnowledgeSearchClient
    _owns_knowledge_client: bool = field(default=False, repr=False)

    async def close(self) -> None:
        await self.fastgpt_client.close()
        if self._owns_knowledge_client:
            close = getattr(self.knowledge_client, "close", None)
            if close is not None:
                await close()

@dataclass
class HotNewsWorkerRuntime:
    """这边要持有数据库等内容"""
    activities: HotNewsActivities
    run_store: PostgresHotNewsRunStore
    database: Database
    hot_news_runtime: HotNewsRuntime
    run_metrics: HotNewsRunMetrics

    async def close(self) -> None:
        await self.hot_news_runtime.close()
        await self.database.close()


def create_hot_news_orchestration_service(
    *,
    behavior_data_source: BehaviorDataSource,
    baseline_provider: HotNewsBaselineProvider,
    content_repository: NewsContentRepository,
    knowledge_search: KnowledgeSearchClient,
    analysis_service: HotNewsAnalysisService,
    policy: HotNewsOrchestrationPolicy,
) -> HotNewsOrchestrationService:
    """组装稳定领域 Port；企业 SDK 只能在调用方先转换为 Adapter。"""

    enrichment_service = HotNewsEnrichmentService(
        content_repository=content_repository,
        knowledge_search=knowledge_search,
    )
    return HotNewsOrchestrationService(
        behavior_data_source=behavior_data_source,
        baseline_provider=baseline_provider,
        enrichment_service=enrichment_service,
        analysis_service=analysis_service,
        policy=policy,
    )


"""这边是创建热点新闻执行对象"""
def create_hot_news_runtime(
        settings: Settings,
        *,
        behavior_data_source: BehaviorDataSource,
        baseline_provider: HotNewsBaselineProvider,
        content_repository: NewsContentRepository,
        policy: HotNewsOrchestrationPolicy,
        knowledge_search: KnowledgeSearchClient | None = None,
) -> HotNewsRuntime:
    if not settings.fastgpt_hot_news_app_id:
        raise ValueError("FASTGPT_HOT_NEWS_APP_ID is required")
    if knowledge_search is None and not settings.fastgpt_dataset_id:
        raise ValueError("FASTGPT_DATASET_ID is required")

    fastgpt_client = FastGPTClient(settings)
    owns_knowledge_client = knowledge_search is None
    knowledge_client = (
        FastGPTKnowledgeSearchClient(settings)
        if knowledge_search is None
        else knowledge_search
    )

    analysis_service = HotNewsAnalysisService(
        input_builder=HotNewsAnalysisInputBuilder(),
        runner=HotNewsAnalysisAgentRunner(
            fastgpt_client,
            settings.fastgpt_hot_news_app_id,
        ),
        validator=HotNewsAnalysisValidator(),
    )
    memory_aware_analysis_service = MemoryAwareHotNewsAnalysisService(
        memory_context_service=MemoryContextApplicationService(),
        memory_prompt_builder=MemoryPromptInputBuilder(),
        analysis_service=analysis_service,
    )

    orchestration_service = create_hot_news_orchestration_service(
        behavior_data_source=behavior_data_source,
        baseline_provider=baseline_provider,
        content_repository=content_repository,
        knowledge_search=knowledge_client,
        analysis_service=analysis_service,
        policy=policy,
    )

    return HotNewsRuntime(
        service=orchestration_service,
        memory_aware_analysis_service=memory_aware_analysis_service,
        fastgpt_client=fastgpt_client,
        knowledge_client=knowledge_client,
        _owns_knowledge_client=owns_knowledge_client,
    )

def create_hot_news_worker_runtime(
    settings: Settings,
    *,
    behavior_data_source: BehaviorDataSource,
    baseline_provider: HotNewsBaselineProvider,
    content_repository: NewsContentRepository,
    policy: HotNewsOrchestrationPolicy,
    knowledge_search: KnowledgeSearchClient | None = None,
    run_metrics: HotNewsRunMetrics | None = None,
) -> HotNewsWorkerRuntime:
    # 创建数据库
    database = Database(settings)
    # 装配热点计算、内容富化、FastGPT分析主链路
    hot_news_runtime = create_hot_news_runtime(
        settings,
        behavior_data_source=behavior_data_source,
        baseline_provider=baseline_provider,
        content_repository=content_repository,
        policy=policy,
        knowledge_search=knowledge_search,
    )
    runtime_registry = ProductionBundleRuntimeRegistry.from_json(
        hot_news_runtime.fastgpt_client,
        settings.hot_news_runtime_manifest_json,
    )
    active_bundle_service = ActiveProductionBundleHotNewsService(
        database=database,
        runtime_registry=runtime_registry,
        behavior_data_source=behavior_data_source,
        baseline_provider=baseline_provider,
        content_repository=content_repository,
        knowledge_search=hot_news_runtime.knowledge_client,
        policy_template=policy,
    )
    # Activity 重试时，通过 PostgreSQL 进行幂等计算和持久化结果
    run_store = PostgresHotNewsRunStore(database)
    feedback_sink = AutomaticHotNewsFeedbackSink(
        database=database,
        run_store=run_store,
        policy=AutomaticFeedbackPolicy(
            version="automatic-feedback-v1",
        ),
    )
    # Temporal 只负责调用主链路和持久化结果
    resolved_run_metrics = run_metrics or InMemoryHotNewsRunMetrics()
    activities = HotNewsActivities(
        orchestration_service=active_bundle_service,
        run_store=run_store,
        run_metrics=resolved_run_metrics,
        feedback_sink=feedback_sink,
    )

    return HotNewsWorkerRuntime(
        activities=activities,
        run_store=run_store,
        database=database,
        hot_news_runtime=hot_news_runtime,
        run_metrics=resolved_run_metrics,
    )
