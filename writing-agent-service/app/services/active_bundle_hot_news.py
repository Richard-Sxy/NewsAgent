"""Online hot-news execution resolved from the tenant's active Bundle."""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy.exc import SQLAlchemyError

from app.analytics.data_source import BehaviorDataSource
from app.analytics.hot_news_enrichment import HotNewsEnrichmentService
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
from app.services.hot_news_orchestration import (
    HotNewsBaselineProvider,
    HotNewsOrchestrationPolicy,
    HotNewsOrchestrationService,
    HotNewsRunRequest,
    HotNewsRunResult,
)
from app.services.production_bundle_runtime import (
    ProductionBundleRuntimeRegistry,
)


class ActiveProductionBundlePersistenceError(RuntimeError):
    """A transient active-Bundle lookup failure."""

    retryable = True


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
        runtime_registry: ProductionBundleRuntimeRegistry,
        behavior_data_source: BehaviorDataSource,
        baseline_provider: HotNewsBaselineProvider,
        content_repository: NewsContentRepository,
        knowledge_search: KnowledgeSearchClient,
        policy_template: HotNewsOrchestrationPolicy,
    ) -> None:
        self._database = database
        self._runtime_registry = runtime_registry
        self._behavior_data_source = behavior_data_source
        self._baseline_provider = baseline_provider
        self._enrichment_service = HotNewsEnrichmentService(
            content_repository=content_repository,
            knowledge_search=knowledge_search,
        )
        self._policy_template = policy_template

    async def run(self, request: HotNewsRunRequest) -> HotNewsRunResult:
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
        policy = replace(
            self._policy_template,
            production_bundle_version=active.bundle_version,
        )
        analysis_service = HotNewsAnalysisService(
            input_builder=HotNewsAnalysisInputBuilder(
                policy_version=active.spec.analysis_prompt_version,
            ),
            runner=self._runtime_registry.create(active.spec),
            validator=HotNewsAnalysisValidator(),
        )
        service = HotNewsOrchestrationService(
            behavior_data_source=self._behavior_data_source,
            baseline_provider=self._baseline_provider,
            enrichment_service=self._enrichment_service,
            analysis_service=analysis_service,
            policy=policy,
        )
        return await service.run(request)
