"""Assemble the Python-owned hot-news runtime."""

from dataclasses import dataclass

from redis.asyncio import Redis

from app.activities.hot_news import HotNewsActivities
from app.analytics.data_source import BehaviorDataSource
from app.analytics.hot_news_enrichment import HotNewsEnrichmentService
from app.analytics.metric_source import NewsMetricSource
from app.analytics.news_content import NewsContentRepository
from app.analytics.text2sql_metric_source import HotNewsMetricSqlTemplate, Text2SqlNewsMetricSource
from app.clients.enterprise.sql_warehouse import SqlWarehouseClient
from app.clients.knowledge_base import KnowledgeSearchClient
from app.config import Settings
from app.db.session import Database
from app.model_runtime.bundle_runtime import NativeProductionBundleRuntimeRegistry
from app.model_runtime.core import InferencePort, PromptRegistry
from app.observability.hot_news import HotNewsRunMetrics, InMemoryHotNewsRunMetrics
from app.repositories.hot_event import PostgresHotEventRepository
from app.schemas.text2sql import Text2SqlSchema
from app.services.active_bundle_hot_news import ActiveProductionBundleHotNewsService, HotNewsRunDependenciesFactory
from app.services.agents.text2sql import Text2SqlAgentRunner
from app.services.data_loop.automatic_feedback import AutomaticFeedbackPolicy, AutomaticHotNewsFeedbackSink
from app.services.hot_event_lifecycle import HotEventLifecycleService
from app.services.hot_news_analysis import HotNewsAnalysisService
from app.services.hot_news_event_stream import RedisHotNewsEventStream
from app.services.hot_news_orchestration import (
    HotNewsBaselineProvider,
    HotNewsOrchestrationPolicy,
    HotNewsOrchestrationService,
)
from app.services.hot_news_run_store import PostgresHotNewsRunStore


@dataclass
class NativeHotNewsRuntime:
    knowledge_client: KnowledgeSearchClient

    async def close(self) -> None:
        close = getattr(self.knowledge_client, "close", None)
        if close is not None:
            await close()


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
) -> Text2SqlNewsMetricSource:
    """Assemble deterministic templates with an optional Python Agent fallback."""

    return Text2SqlNewsMetricSource(
        warehouse=warehouse,
        schema=schema,
        template=template,
        generator=generator,
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
) -> HotNewsWorkerRuntime:
    if inference_port is None or prompt_registry is None or knowledge_search is None:
        raise ValueError(
            "hot-news worker requires an InferencePort, PromptRegistry and "
            "KnowledgeSearchClient"
        )
    database = Database(settings)
    redis = Redis.from_url(str(settings.redis_url), decode_responses=False)
    hot_news_runtime = NativeHotNewsRuntime(knowledge_search)
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
