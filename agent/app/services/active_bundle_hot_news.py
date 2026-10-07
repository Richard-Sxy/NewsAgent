"""选择运行版本：读取当前生效的 Bundle，为本次运行绑定模型、Prompt、业务规则。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Awaitable, Callable

from sqlalchemy.exc import SQLAlchemyError

from app.analytics.data_source import BehaviorDataSource
from app.analytics.hot_news_enrichment import HotNewsEnrichmentService
from app.analytics.metric_source import NewsMetricSource
from app.analytics.news_content import NewsContentRepository
from app.clients.knowledge_base import KnowledgeSearchClient
from app.db.session import Database
from app.domain.errors import HotNewsDataQualityError
from app.repositories.production_bundle import (
    PostgresProductionBundleRepository,
    ProductionBundleDataCorruptedError,
)
from app.services.hot_news_analysis import (
    HotNewsAnalysisInputBuilder,
    HotNewsAnalysisService,
    HotNewsAnalysisValidator,
)
from app.services.hot_news_analysis_batch import HotNewsAnalysisBatch
from app.services.hot_news_orchestration import (
    HotNewsBaselineProvider,
    HotNewsOrchestrationPolicy,
    HotNewsOrchestrationService,
    HotNewsRunRequest,
    HotNewsRunResult,
)
from app.model_runtime.bundle_runtime import NativeProductionBundleRuntimeRegistry


class ActiveProductionBundlePersistenceError(RuntimeError):
    """A transient active-Bundle lookup failure."""

    retryable = True


@dataclass(frozen=True)
class HotNewsRunDependencies:
    """Run-scoped metric tools; no shared mutable query state between runs."""

    metric_source: NewsMetricSource
    baseline_provider: HotNewsBaselineProvider
    content_repository: NewsContentRepository
    policy: HotNewsOrchestrationPolicy


HotNewsRunDependenciesFactory = Callable[[HotNewsRunRequest], Awaitable[HotNewsRunDependencies]]


class ActiveProductionBundleHotNewsService:
    """Resolve a complete immutable runtime at the start of every online run.

    Activation becomes effective without restarting the worker.  A run keeps
    one resolved snapshot for its full lifetime, so concurrent activation can
    only affect the next run and cannot mix component versions mid-window.
    """

    def __init__(
        self,
        *,
        database: Database,
        runtime_registry: NativeProductionBundleRuntimeRegistry,
        baseline_provider: HotNewsBaselineProvider,
        content_repository: NewsContentRepository,
        knowledge_search: KnowledgeSearchClient,
        policy_template: HotNewsOrchestrationPolicy,
        behavior_data_source: BehaviorDataSource | None = None,
        metric_source: NewsMetricSource | None = None,
        run_dependencies_factory: HotNewsRunDependenciesFactory | None = None,
        analysis_max_concurrency: int = 1,
    ) -> None:
        self._database = database
        self._runtime_registry = runtime_registry
        self._behavior_data_source = behavior_data_source
        self._metric_source = metric_source
        self._baseline_provider = baseline_provider
        self._enrichment_service = HotNewsEnrichmentService(
            content_repository=content_repository,
            knowledge_search=knowledge_search,
        )
        self._policy_template = policy_template
        self._knowledge_search = knowledge_search
        self._run_dependencies_factory = run_dependencies_factory
        # One limiter survives creation of the per-run Bundle service.
        self._analysis_batch = HotNewsAnalysisBatch(max_concurrency=analysis_max_concurrency)

    async def run(self, request: HotNewsRunRequest) -> HotNewsRunResult:
        request.validate()
        try:
            async with self._database.session() as session:
                active = await PostgresProductionBundleRepository(
                    session
                ).get_active_bundle(tenant_id=request.tenant_id)
        except ProductionBundleDataCorruptedError as exc:
            raise HotNewsDataQualityError(
                "the tenant's active production bundle is corrupted"
            ) from exc
        except SQLAlchemyError as exc:
            raise ActiveProductionBundlePersistenceError(
                "failed to resolve the tenant's active production bundle"
            ) from exc

        if active is None:
            raise HotNewsDataQualityError(
                "tenant has no active production bundle"
            )
        if request.production_bundle_version != active.bundle_version:
            raise HotNewsDataQualityError(
                "run request does not target the currently active production "
                "bundle"
            )

        self._runtime_registry.ensure_supported(active.spec)
        dependencies = None
        if request.sql_query_id is not None:
            if self._run_dependencies_factory is None:
                raise HotNewsDataQualityError("SQL-backed hot-news tool is not configured")
            dependencies = await self._run_dependencies_factory(request)
        policy = replace(
            dependencies.policy if dependencies is not None else self._policy_template,
            production_bundle_version=active.bundle_version,
        )
        analysis_service = HotNewsAnalysisService(
            input_builder=HotNewsAnalysisInputBuilder(
                policy_version=active.spec.analysis_prompt_version,
            ),
            runner=self._runtime_registry.create(
                active.spec,
                tenant_id=request.tenant_id,
            ),
            validator=HotNewsAnalysisValidator(),
        )
        service = HotNewsOrchestrationService(
            behavior_data_source=None if dependencies is not None else self._behavior_data_source,
            metric_source=dependencies.metric_source if dependencies is not None else self._metric_source,
            baseline_provider=dependencies.baseline_provider if dependencies is not None else self._baseline_provider,
            enrichment_service=HotNewsEnrichmentService(
                content_repository=dependencies.content_repository,
                knowledge_search=self._knowledge_search,
            ) if dependencies is not None else self._enrichment_service,
            analysis_service=analysis_service,
            policy=policy,
            analysis_batch=self._analysis_batch,
        )
        result = await service.run(request)
        if dependencies is not None:
            trace = await dependencies.metric_source.get_tool_trace()
            trace["scope_sha256"] = request.sql_query_scope_sha256
            result = replace(result, sql_tool_trace=trace)
        return result
