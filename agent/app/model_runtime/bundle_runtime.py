"""
版本配套检查：检查 Production Bundle 是否可执行，并按绑定的模型、Prompt版本创建创建热点分析 Runner。
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from pydantic import TypeAdapter, ValidationError

from app.model_runtime.core import InferencePort, PromptRegistry, StructuredInferenceService
from app.model_runtime.hot_news import HOT_NEWS_SCENE, NativeHotNewsAnalysisRunner
from app.model_runtime.bundle_contract import (
    LOCAL_EXECUTION_FIELDS,
    UnsupportedProductionBundleRuntimeError,
)
from app.schemas.production_bundle import ProductionBundleSpec
from app.services.production_bundle import bundle_spec_sha256


class NativeProductionBundleRuntimeRegistry:
    """仅执行已注册的提示/模型对；应用程序 ID 已存档。"""

    def __init__(
        self,
        inference: InferencePort | None,
        prompts: PromptRegistry,
        profiles: Iterable[ProductionBundleSpec],
        *,
        allowed_model_routes: tuple[str, ...] | None = None,
    ) -> None:
        values = tuple(profiles)
        if not values:
            raise UnsupportedProductionBundleRuntimeError(
                "HOT_NEWS_RUNTIME_MANIFEST_JSON must register at least one Bundle"
            )
        local_signature = tuple(
            getattr(values[0], field) for field in LOCAL_EXECUTION_FIELDS
        )
        by_digest: dict[str, ProductionBundleSpec] = {}
        for spec in values:
            if (
                allowed_model_routes is not None
                and spec.model_version not in allowed_model_routes
            ):
                raise UnsupportedProductionBundleRuntimeError(
                    f"unapproved inference model route: {spec.model_version}"
                )
            if tuple(getattr(spec, field) for field in LOCAL_EXECUTION_FIELDS) != local_signature:
                raise UnsupportedProductionBundleRuntimeError(
                    "one worker cannot register multiple local metric/ranking/"
                    "schema/validator/memory implementations"
                )
            try:
                prompts.resolve(scene=HOT_NEWS_SCENE, version=spec.analysis_prompt_version)
            except ValueError as exc:
                raise UnsupportedProductionBundleRuntimeError(
                    f"unregistered Python prompt: {spec.analysis_prompt_version}"
                ) from exc
            by_digest[bundle_spec_sha256(spec)] = spec
        self._inference = inference
        self._prompts = prompts
        self._profiles = by_digest
        self.local_signature = local_signature

    @classmethod
    def from_json(
        cls,
        inference: InferencePort | None,
        prompts: PromptRegistry,
        manifest_json: str,
        *,
        allowed_model_routes: tuple[str, ...] | None = None,
    ) -> "NativeProductionBundleRuntimeRegistry":
        try:
            profiles = TypeAdapter(tuple[ProductionBundleSpec, ...]).validate_python(
                json.loads(manifest_json)
            )
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            raise UnsupportedProductionBundleRuntimeError(
                "HOT_NEWS_RUNTIME_MANIFEST_JSON must be a JSON array of complete Bundles"
            ) from exc
        return cls(
            inference, prompts, profiles,
            allowed_model_routes=allowed_model_routes,
        )

    @classmethod
    def validation_only(
        cls,
        prompts: PromptRegistry,
        manifest_json: str,
        *,
        allowed_model_routes: tuple[str, ...] | None = None,
    ) -> "NativeProductionBundleRuntimeRegistry":
        return cls.from_json(
            None, prompts, manifest_json,
            allowed_model_routes=allowed_model_routes,
        )

    def ensure_supported(self, spec: ProductionBundleSpec) -> None:
        if bundle_spec_sha256(spec) not in self._profiles:
            raise UnsupportedProductionBundleRuntimeError(
                "production bundle snapshot is not registered in this worker"
            )

    def ensure_transition_supported(
        self,
        *,
        baseline: ProductionBundleSpec,
        candidate: ProductionBundleSpec,
    ) -> None:
        self.ensure_supported(baseline)
        self.ensure_supported(candidate)
        changed_local = [
            field for field in LOCAL_EXECUTION_FIELDS
            if getattr(baseline, field) != getattr(candidate, field)
        ]
        if changed_local:
            raise UnsupportedProductionBundleRuntimeError(
                "analysis-input replay cannot evaluate local asset changes: "
                f"{changed_local}"
            )

    def create(
        self,
        spec: ProductionBundleSpec,
        *,
        tenant_id: str = "offline-evaluation",
    ) -> NativeHotNewsAnalysisRunner:
        self.ensure_supported(spec)
        if self._inference is None:
            raise UnsupportedProductionBundleRuntimeError(
                "validation-only native registry cannot execute a model"
            )
        return NativeHotNewsAnalysisRunner(
            StructuredInferenceService(
                inference=self._inference,
                prompts=self._prompts,
            ),
            tenant_id=tenant_id,
            prompt_version=spec.analysis_prompt_version,
            model_route=spec.model_version,
        )
