"""Hot-news execution uses only the Python model runtime."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain.errors import AgentOutputValidationError
from app.config import Settings
from app.model_runtime.bundle_contract import UnsupportedProductionBundleRuntimeError
from app.model_runtime.bundle_runtime import NativeProductionBundleRuntimeRegistry
from app.model_runtime.core import InferenceRequest, RawInferenceResult
from app.model_runtime.hot_news import local_hot_news_prompts
from app.model_runtime.local_inference import LocalHotNewsInference
from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.factory import build_model_runtime_ports
from app.schemas.hot_news import HotNewsAnalysisInput, HotNewsMetrics, HotScoreComponents
from app.schemas.production_bundle import ProductionBundleSpec
from app.services.hot_news_analysis import (
    HotNewsAnalysisInputBuilder,
    HotNewsAnalysisService,
    HotNewsAnalysisValidator,
)
from app.services.hot_news_orchestration import (
    HotNewsOrchestrationService,
    HotNewsRunRequest,
)
from app.analytics.hot_news_enrichment import HotNewsEnrichmentService
from examples.hot_news_e2e_support import build_hot_news_e2e_dependencies
from examples.native_hot_news_e2e_knowledge import build_native_e2e_knowledge_search


LOCAL_MODEL_CONFIG = Path(__file__).resolve().parents[1] / "deploy/model-runtime.local.yml"


def native_spec(**updates: str) -> ProductionBundleSpec:
    values = {
        "metric_definition_version": "e2e-metrics-v1",
        "hot_score_policy_version": "e2e-hot-score-v1",
        "reranker_policy_version": "e2e-reranker-v1",
        "analysis_prompt_version": "native-e2e-prompt-v1",
        "model_profile_id": "retired-native-field",
        "model_version": "native-local-model-v1",
        "output_schema_version": "e2e-output-schema-v1",
        "validator_version": "e2e-validator-v1",
        "memory_resolver_policy_version": "e2e-memory-resolver-v1",
    }
    values.update(updates)
    return ProductionBundleSpec(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 3])
async def test_native_hot_news_runs_full_domain_chain(concurrency) -> None:
    from app.services.hot_news_analysis_batch import HotNewsAnalysisBatch
    scenario = build_hot_news_e2e_dependencies()
    config = load_model_runtime_config(LOCAL_MODEL_CONFIG)
    ports = build_model_runtime_ports(config, environment="e2e")
    knowledge = await build_native_e2e_knowledge_search(
        scenario,
        embedding=ports.embedding,
        embedding_version=config.embedding.model_routes[0],
        embedding_batch_size=config.embedding.max_batch_size,
    )
    spec = native_spec()
    registry = NativeProductionBundleRuntimeRegistry(
        ports.inference,
        ports.prompts,
        (spec,),
        allowed_model_routes=config.inference.model_routes,
    )
    analysis = HotNewsAnalysisService(
        input_builder=HotNewsAnalysisInputBuilder(
            policy_version=spec.analysis_prompt_version
        ),
        runner=registry.create(spec, tenant_id=scenario.tenant_id),
        validator=HotNewsAnalysisValidator(),
    )
    service = HotNewsOrchestrationService(
        behavior_data_source=scenario.behavior_data_source,
        baseline_provider=scenario.baseline_provider,
        enrichment_service=HotNewsEnrichmentService(
            content_repository=scenario.content_repository,
            knowledge_search=knowledge,
        ),
        analysis_service=analysis,
        policy=scenario.policy,
        analysis_batch=HotNewsAnalysisBatch(max_concurrency=concurrency),
    )

    try:
        result = await service.run(
            HotNewsRunRequest(
                tenant_id=scenario.tenant_id,
                window_start=scenario.window_start,
                window_end=scenario.window_end,
                production_bundle_version=scenario.production_bundle_version,
            )
        )
    finally:
        await ports.close()

    assert result.analyzed_news
    assert len(result.analyzed_news) == len(result.ranked_news)
    assert result.analysis_execution.max_concurrency == concurrency
    assert result.analysis_execution.task_count == len(result.ranked_news)
    assert [item.news_id for item in result.analyzed_news] == [item.current.news_id for item in result.ranked_news]
    for item in result.analyzed_news:
        assert item.analysis.value.news_id == item.news_id
        assert item.analysis.request_id.startswith("native-local-")
        assert item.analysis.usage["local_stub"] is True
        assert "本地" in item.analysis.value.trend_assessment


@pytest.mark.asyncio
async def test_native_runner_rejects_malformed_model_output() -> None:
    class InvalidInference:
        async def complete(self, request: InferenceRequest) -> RawInferenceResult:
            return RawInferenceResult("not JSON", request_id="bad-request")

    spec = native_spec()
    registry = NativeProductionBundleRuntimeRegistry(
        InvalidInference(), local_hot_news_prompts(), (spec,)
    )
    scenario = build_hot_news_e2e_dependencies()
    analysis_input = HotNewsAnalysisInput(
        news_id="news-1",
        title="本地测试",
        content_type="article",
        window_start=scenario.window_start,
        window_end=scenario.window_end,
        hot_score=0.5,
        metrics=HotNewsMetrics(
            impressions=10,
            clicks=2,
            ctr=0.2,
            unique_users=2,
            effective_consumptions=1,
            interactions=1,
        ),
        score_components=HotScoreComponents(
            click=0.2,
            consumption=0.1,
            interaction=0.1,
            growth=0.1,
        ),
        analysis_policy_version="native-e2e-prompt-v1",
    )

    with pytest.raises(AgentOutputValidationError) as error:
        await registry.create(spec, tenant_id=scenario.tenant_id).run(analysis_input)
    assert error.value.request_id == "bad-request"


def test_native_registry_requires_registered_prompt_and_ignores_retired_app_id() -> None:
    spec = native_spec()
    registry = NativeProductionBundleRuntimeRegistry(
        LocalHotNewsInference(), local_hot_news_prompts(), (spec,)
    )
    changed_app = native_spec(model_profile_id="another-archival-value")
    registry.ensure_transition_supported(baseline=spec, candidate=spec)
    with pytest.raises(UnsupportedProductionBundleRuntimeError, match="not registered"):
        registry.create(changed_app)
    registry_with_both = NativeProductionBundleRuntimeRegistry(
        LocalHotNewsInference(), local_hot_news_prompts(), (spec, changed_app)
    )
    registry_with_both.ensure_transition_supported(
        baseline=spec, candidate=changed_app
    )
    with pytest.raises(UnsupportedProductionBundleRuntimeError, match="unregistered Python prompt"):
        NativeProductionBundleRuntimeRegistry(
            LocalHotNewsInference(),
            local_hot_news_prompts(),
            (native_spec(analysis_prompt_version="unknown"),),
        )


def test_validation_only_native_registry_never_calls_model() -> None:
    spec = native_spec()
    registry = NativeProductionBundleRuntimeRegistry.validation_only(
        local_hot_news_prompts(),
        json.dumps([spec.model_dump(mode="json")]),
    )
    with pytest.raises(UnsupportedProductionBundleRuntimeError, match="cannot execute"):
        registry.create(spec)


def test_settings_accept_only_native_runtime() -> None:
    common = {
        "_env_file": None,
        "environment": "e2e",
        "database_url": "postgresql+psycopg://test:test@localhost/test",
        "redis_url": "redis://localhost:6379/0",
        "temporal_address": "localhost:7233",
        "temporal_namespace": "default",
        "artifact_bucket": "test-artifacts",
        "model_runtime_config_path": "deploy/model-runtime.local.yml",
    }
    native = Settings(**common, model_runtime_backend="native")
    with pytest.raises(ValueError, match="Input should be 'native'"):
        Settings(**common, model_runtime_backend="external_framework")
