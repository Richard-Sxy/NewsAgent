"""Assemble the Python-owned hot-news runtime."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.modules.behavior_tags.runtime import LocalBehaviorRuntime
    from app.modules.reader_profiles.service import ReaderProfileService

from redis.asyncio import Redis

from app.modules.hot_news.workflows.activities import HotNewsActivities
from app.modules.hot_news.scoring.data_source import BehaviorDataSource
from app.modules.hot_news.analysis.hot_news_enrichment import HotNewsEnrichmentService
from app.modules.hot_news.scoring.metric_source import NewsMetricSource
from app.analytics.news_content import NewsContentRepository
from app.modules.hot_news.scoring.text2sql_metric_source import HotNewsMetricSqlTemplate, Text2SqlNewsMetricSource
from app.clients.enterprise.sql_warehouse import SqlWarehouseClient
from app.clients.knowledge_base import KnowledgeSearchClient
from app.config import Settings
from app.db.session import Database
from app.modules.data_loop.bundle_runtime import NativeProductionBundleRuntimeRegistry
from app.model_runtime.core import InferencePort, PromptRegistry
from app.observability.hot_news import HotNewsRunMetrics, InMemoryHotNewsRunMetrics
from app.modules.hot_news.persistence.hot_event_repository import PostgresHotEventRepository
from app.schemas.text2sql import Text2SqlSchema
from app.modules.hot_news.application.active_bundle_hot_news import ActiveProductionBundleHotNewsService, HotNewsRunDependenciesFactory
from app.modules.hot_news.analysis.text2sql_agent import Text2SqlAgentRunner
from app.modules.data_loop.automatic_feedback import AutomaticFeedbackPolicy, AutomaticHotNewsFeedbackSink
from app.modules.hot_news.application.hot_event_lifecycle import HotEventLifecycleService
from app.modules.hot_news.analysis.hot_news_analysis import HotNewsAnalysisService
from app.modules.hot_news.application.hot_news_event_stream import RedisHotNewsEventStream
from app.modules.hot_news.application.hot_news_orchestration import (
    HotNewsBaselineProvider,
    HotNewsOrchestrationPolicy,
    HotNewsOrchestrationService,
)
from app.modules.hot_news.adapters.hot_news_run_store import PostgresHotNewsRunStore


@dataclass
class NativeHotNewsRuntime:
    knowledge_client: KnowledgeSearchClient
    reader_profile_service: "ReaderProfileService | None" = None

    async def close(self) -> None:
        close = getattr(self.knowledge_client, "close", None)
        try:
            if close is not None:
                await close()
        finally:
            if self.reader_profile_service is not None:
                await self.reader_profile_service.close()


@dataclass
class HotNewsWorkerRuntime:
    activities: HotNewsActivities
    run_store: PostgresHotNewsRunStore
    database: Database
    hot_news_runtime: NativeHotNewsRuntime
    run_metrics: HotNewsRunMetrics
    redis: Redis

    async def close(self) -> None:
        await self.hot_news_runtime.close()
        await self.redis.aclose()
        await self.database.close()


def create_hot_news_orchestration_service(
    *,
    baseline_provider: HotNewsBaselineProvider,
    content_repository: NewsContentRepository,
    knowledge_search: KnowledgeSearchClient,
    analysis_service: HotNewsAnalysisService,
    policy: HotNewsOrchestrationPolicy,
    behavior_data_source: BehaviorDataSource | None = None,
    metric_source: NewsMetricSource | None = None,
) -> HotNewsOrchestrationService:
    return HotNewsOrchestrationService(
        behavior_data_source=behavior_data_source,
        metric_source=metric_source,
        baseline_provider=baseline_provider,
        enrichment_service=HotNewsEnrichmentService(
            content_repository=content_repository,
            knowledge_search=knowledge_search,
        ),
        analysis_service=analysis_service,
        policy=policy,
    )


def create_text2sql_metric_source(
    settings: Settings,
    *,
    warehouse: SqlWarehouseClient,
    schema: Text2SqlSchema,
    template: HotNewsMetricSqlTemplate | None = None,
    generator: Text2SqlAgentRunner | None = None,
    metric_contract: HotNewsMetricSqlTemplate | None = None,
) -> Text2SqlNewsMetricSource:
    """Assemble deterministic templates with an optional Python Agent fallback."""

    return Text2SqlNewsMetricSource(
        warehouse=warehouse,
        schema=schema,
        template=template,
        generator=generator,
        metric_contract=metric_contract,
        max_rows=settings.text2sql_max_rows,
        timeout_ms=settings.text2sql_timeout_ms,
    )


def create_hot_news_worker_runtime(
    settings: Settings,
    *,
    baseline_provider: HotNewsBaselineProvider,
    content_repository: NewsContentRepository,
    policy: HotNewsOrchestrationPolicy,
    behavior_data_source: BehaviorDataSource | None = None,
    metric_source: NewsMetricSource | None = None,
    knowledge_search: KnowledgeSearchClient | None = None,
    run_metrics: HotNewsRunMetrics | None = None,
    inference_port: InferencePort | None = None,
    prompt_registry: PromptRegistry | None = None,
    allowed_model_routes: tuple[str, ...] | None = None,
    run_dependencies_factory: HotNewsRunDependenciesFactory | None = None,
    behavior_runtime: "LocalBehaviorRuntime | None" = None,
    reader_profile_service: "ReaderProfileService | None" = None,
) -> HotNewsWorkerRuntime:
    if inference_port is None or prompt_registry is None or knowledge_search is None:
        raise ValueError(
            "hot-news worker requires an InferencePort, PromptRegistry and "
            "KnowledgeSearchClient"
        )
    database = Database(settings)
    redis = Redis.from_url(str(settings.redis_url), decode_responses=False)
    hot_news_runtime = NativeHotNewsRuntime(knowledge_search, reader_profile_service)
    runtime_registry = NativeProductionBundleRuntimeRegistry.from_json(
        inference_port,
        prompt_registry,
        settings.hot_news_runtime_manifest_json,
        allowed_model_routes=allowed_model_routes,
    )
    active_bundle_service = ActiveProductionBundleHotNewsService(
        database=database,
        runtime_registry=runtime_registry,
        behavior_data_source=behavior_data_source,
        metric_source=metric_source,
        baseline_provider=baseline_provider,
        content_repository=content_repository,
        knowledge_search=knowledge_search,
        policy_template=policy,
        run_dependencies_factory=run_dependencies_factory,
        analysis_max_concurrency=settings.hot_news_analysis_max_concurrency,
        behavior_runtime=behavior_runtime,
        reader_profile_service=reader_profile_service,
    )
    run_store = PostgresHotNewsRunStore(database)
    feedback_sink = AutomaticHotNewsFeedbackSink(
        database=database,
        run_store=run_store,
        policy=AutomaticFeedbackPolicy(version="automatic-feedback-v1"),
    )
    event_lifecycle = HotEventLifecycleService(
        repository=PostgresHotEventRepository(database),
    )
    resolved_run_metrics = run_metrics or InMemoryHotNewsRunMetrics()
    activities = HotNewsActivities(
        orchestration_service=active_bundle_service,
        run_store=run_store,
        run_metrics=resolved_run_metrics,
        feedback_sink=feedback_sink,
        event_lifecycle=event_lifecycle,
        event_stream=RedisHotNewsEventStream(redis, settings),
    )
    return HotNewsWorkerRuntime(
        activities=activities,
        run_store=run_store,
        database=database,
        hot_news_runtime=hot_news_runtime,
        run_metrics=resolved_run_metrics,
        redis=redis,
    )
