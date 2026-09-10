import json

import pytest

from app.schemas.production_bundle import ProductionBundleSpec
from app.services.production_bundle_runtime import (
    ProductionBundleRuntimeRegistry,
    UnsupportedProductionBundleRuntimeError,
)


def spec(app_id: str = "app-v1", **updates) -> ProductionBundleSpec:
    values = {
        "metric_definition_version": "metric-v1",
        "hot_score_policy_version": "score-v1",
        "reranker_policy_version": "reranker-v1",
        "analysis_prompt_version": f"prompt-{app_id}",
        "fastgpt_app_id": app_id,
        "model_version": "model-v1",
        "output_schema_version": "schema-v1",
        "validator_version": "validator-v1",
        "memory_resolver_policy_version": "memory-v1",
    }
    values.update(updates)
    return ProductionBundleSpec(**values)


def test_registry_resolves_only_registered_complete_snapshots() -> None:
    registered = spec()
    registry = ProductionBundleRuntimeRegistry(object(), (registered,))

    runner = registry.create(registered)

    assert runner.app_id == "app-v1"
    with pytest.raises(
        UnsupportedProductionBundleRuntimeError,
        match="not registered",
    ):
        registry.create(spec("unregistered"))


def test_registry_rejects_fake_local_runtime_versions() -> None:
    with pytest.raises(
        UnsupportedProductionBundleRuntimeError,
        match="multiple local",
    ):
        ProductionBundleRuntimeRegistry(
            object(),
            (
                spec("app-v1"),
                spec("app-v2", metric_definition_version="metric-v2"),
            ),
        )


def test_registry_binds_prompt_and_model_to_immutable_app_id() -> None:
    with pytest.raises(
        UnsupportedProductionBundleRuntimeError,
        match="multiple prompt/model",
    ):
        ProductionBundleRuntimeRegistry(
            object(),
            (
                spec("app-v1"),
                spec("app-v1", analysis_prompt_version="prompt-mutated"),
            ),
        )


def test_registry_loads_strict_json_manifest_and_allows_external_transition() -> None:
    baseline = spec("app-v1")
    candidate = spec(
        "app-v2",
        analysis_prompt_version="prompt-v2",
        model_version="model-v2",
    )
    payload = json.dumps(
        [
            baseline.model_dump(mode="json"),
            candidate.model_dump(mode="json"),
        ]
    )
    registry = ProductionBundleRuntimeRegistry.from_json(object(), payload)

    registry.ensure_transition_supported(
        baseline=baseline,
        candidate=candidate,
    )

    with pytest.raises(
        UnsupportedProductionBundleRuntimeError,
        match="JSON array",
    ):
        ProductionBundleRuntimeRegistry.from_json(object(), "{}")


def test_validation_only_registry_cannot_execute_models() -> None:
    registered = spec()
    registry = ProductionBundleRuntimeRegistry.validation_only(
        json.dumps([registered.model_dump(mode="json")])
    )

    registry.ensure_supported(registered)
    with pytest.raises(
        UnsupportedProductionBundleRuntimeError,
        match="cannot execute",
    ):
        registry.create(registered)
