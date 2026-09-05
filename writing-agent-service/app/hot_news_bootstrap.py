from dataclasses import dataclass

from app.analytics.data_source import BehaviorDataSource
from app.analytics.hot_news_enrichment import HotNewsEnrichmentService
from app.analytics.news_content import NewsContentRepository
from app.clients.fastgpt import FastGPTClient
from app.clients.knowledge_base import FastGPTKnowledgeSearchClient
from app.config import Settings
from app.services.agents.hot_news import HotNewsAnalysisAgentRunner
from app.services.hot_news_analysis import (
    HotNewsAnalysisInputBuilder,
    HotNewsAnalysisService,
    HotNewsAnalysisValidator,
)
from app.services.hot_news_orchestration import (
    HotNewsBaselineProvider,
    HotNewsOrchestrationPolicy,
    HotNewsOrchestrationService,
)

@dataclass
class HotNewsRuntime:
    """这边主要分为三个类：热点新闻执行类/调用FastGPT大模型接口/调用知识库检索"""
    service: HotNewsOrchestrationService
    fastgpt_client: FastGPTClient
    knowledge_client: FastGPTKnowledgeSearchClient

    async def close(self) -> None:
        await self.fastgpt_client.close()
        await self.knowledge_client.close()

"""这边是创建热点新闻执行对象"""
def create_hot_news_runtime(
        settings: Settings,
        *,
        behavior_data_source: BehaviorDataSource,
        baseline_provider: HotNewsBaselineProvider,
        content_repository: NewsContentRepository,
        policy: HotNewsOrchestrationPolicy,
) -> HotNewsRuntime:
    if not settings.fastgpt_hot_news_app_id:
        raise ValueError("FASTGPT_HOT_NEW_APP_ID is required")
    if not settings.fastgpt_dataset_id:
        raise ValueError("FASTGPT_DATASET_ID is required")

    fastgpt_client = FastGPTClient(settings)
    knowledge_client = FastGPTKnowledgeSearchClient(settings)

    analysis_service = HotNewsAnalysisService(
        input_builder=HotNewsAnalysisInputBuilder,
        runner=HotNewsAnalysisAgentRunner(
            fastgpt_client,
            settings.fastgpt_hot_news_app_id,
        ),
        validator=HotNewsAnalysisValidator(),
    )

    enrichment_service = HotNewsEnrichmentService(
        content_repository=content_repository,
        knowledge_search=knowledge_client
    )

    orchestration_service = HotNewsOrchestrationService(
        behavior_data_source=behavior_data_source,
        baseline_provider=baseline_provider,
        enrichment_service=enrichment_service,
        analysis_service=analysis_service,
        policy=policy,
    )

    return HotNewsRuntime(
        service=orchestration_service,
        fastgpt_client=fastgpt_client,
        knowledge_client=knowledge_client,
    )