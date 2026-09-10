"""Fail-closed resolution of versioned Production Bundle runtimes.

The repository cannot manufacture a FastGPT deployment, a historical Python
metric implementation, or a reranker from a version string.  Operators must
therefore register every executable full Bundle snapshot in deployment
configuration.  The current Data Loop supports changing the externally bound
FastGPT application/prompt/model profile; deterministic Python assets must stay
on the one implementation loaded by this worker.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from pydantic import TypeAdapter, ValidationError

from app.clients.fastgpt import FastGPTClient
from app.schemas.production_bundle import ProductionBundleSpec
from app.services.agents.hot_news import HotNewsAnalysisAgentRunner
from app.services.production_bundle import bundle_spec_sha256


class UnsupportedProductionBundleRuntimeError(ValueError):
    """The immutable Bundle cannot be executed by this worker."""

    retryable = False


# These assets are implemented inside this Python worker.  A frozen
# analysis-input dataset cannot honestly evaluate a change to any of them.
LOCAL_EXECUTION_FIELDS = (
    "metric_definition_version",
    "hot_score_policy_version",
    "reranker_policy_version",
    "output_schema_version",
    "validator_version",
    "memory_resolver_policy_version",
)

EXTERNAL_PROFILE_FIELDS = (
    "fastgpt_app_id",
    "analysis_prompt_version",
    "model_version",
)


class ProductionBundleRuntimeRegistry:
    """Resolve only explicitly registered, executable full Bundle snapshots.

    A single worker process deliberately supports one local Python execution
    signature.  Multiple external FastGPT profiles may be registered, but one
    app id is bound to exactly one prompt/model identity.  This prevents a
    candidate from passing replay merely because ignored version strings were
    changed in its ledger snapshot.
    """

    def __init__(
        self,
        client: FastGPTClient | None,
        profiles: Iterable[ProductionBundleSpec],
    ) -> None:
        values = tuple(profiles)
        if not values:
            raise UnsupportedProductionBundleRuntimeError(
                "HOT_NEWS_RUNTIME_MANIFEST_JSON must register at least one "
                "complete ProductionBundleSpec"
            )

        local_signature = self._local_signature(values[0])
        by_digest: dict[str, ProductionBundleSpec] = {}
        app_bindings: dict[str, tuple[str, str]] = {}
        for spec in values:
            if self._local_signature(spec) != local_signature:
                raise UnsupportedProductionBundleRuntimeError(
                    "one worker cannot register multiple local metric/ranking/"
                    "schema/validator/memory implementations"
                )
            external_signature = (
                spec.analysis_prompt_version,
                spec.model_version,
            )
            old_binding = app_bindings.setdefault(
                spec.fastgpt_app_id,
                external_signature,
            )
            if old_binding != external_signature:
                raise UnsupportedProductionBundleRuntimeError(
                    "one FastGPT app id cannot represent multiple prompt/model "
                    "versions"
                )
            digest = bundle_spec_sha256(spec)
            by_digest[digest] = spec

        self._client = client
        self._profiles = by_digest
        self.local_signature = local_signature

    @classmethod
    def from_json(
        cls,
        client: FastGPTClient | None,
        manifest_json: str,
    ) -> "ProductionBundleRuntimeRegistry":
        try:
            decoded = json.loads(manifest_json)
            profiles = TypeAdapter(tuple[ProductionBundleSpec, ...]).validate_python(
                decoded
            )
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            raise UnsupportedProductionBundleRuntimeError(
                "HOT_NEWS_RUNTIME_MANIFEST_JSON must be a JSON array of "
                "complete ProductionBundleSpec objects"
            ) from exc
        return cls(client, profiles)

    @classmethod
    def validation_only(
        cls,
        manifest_json: str,
    ) -> "ProductionBundleRuntimeRegistry":
        """Build a manifest validator that is incapable of model execution."""

        return cls.from_json(None, manifest_json)

    def ensure_supported(self, spec: ProductionBundleSpec) -> None:
        if bundle_spec_sha256(spec) not in self._profiles:
            raise UnsupportedProductionBundleRuntimeError(
                "production bundle snapshot is not registered in this "
                "worker's immutable runtime manifest"
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
            field
            for field in LOCAL_EXECUTION_FIELDS
            if getattr(baseline, field) != getattr(candidate, field)
        ]
        if changed_local:
            raise UnsupportedProductionBundleRuntimeError(
                "analysis-input replay cannot evaluate local asset changes: "
                f"{changed_local}; deploy a versioned upstream replay adapter first"
            )
        external_changed = any(
            getattr(baseline, field) != getattr(candidate, field)
            for field in EXTERNAL_PROFILE_FIELDS
        )
        if external_changed and (
            baseline.fastgpt_app_id == candidate.fastgpt_app_id
        ):
            raise UnsupportedProductionBundleRuntimeError(
                "prompt/model changes require a new immutable FastGPT app id"
            )

    def create(
        self,
        spec: ProductionBundleSpec,
    ) -> HotNewsAnalysisAgentRunner:
        self.ensure_supported(spec)
        if self._client is None:
            raise UnsupportedProductionBundleRuntimeError(
                "validation-only runtime registry cannot execute a model"
            )
        return HotNewsAnalysisAgentRunner(self._client, spec.fastgpt_app_id)

    @staticmethod
    def _local_signature(spec: ProductionBundleSpec) -> tuple[str, ...]:
        return tuple(getattr(spec, field) for field in LOCAL_EXECUTION_FIELDS)
