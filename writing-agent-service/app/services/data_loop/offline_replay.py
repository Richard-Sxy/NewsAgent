"""Three-cohort deterministic offline replay for Production Bundle candidates."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from app.clients.fastgpt import FastGPTClient
from app.evaluation.hot_news import (
    HotNewsEvaluationReport,
    HotNewsEvaluationRunner,
    HotNewsEvaluationService,
)
from app.schemas.evaluation_dataset import EvaluationDatasetManifest
from app.schemas.production_bundle import (
    EvaluationCohortMetrics,
    EvaluationMetricSet,
    EvaluationSuiteMetrics,
    ProductionBundleSpec,
)
from app.services.agents.hot_news import HotNewsAnalysisAgentRunner
from app.services.data_loop.dataset_freezer import (
    to_hot_news_evaluation_dataset,
)


OFFLINE_REPLAY_VERSION = "hot-news-offline-replay-v1"


class BundleEvaluationRunnerFactory(Protocol):
    def ensure_supported(self, spec: ProductionBundleSpec) -> None: ...

    def ensure_transition_supported(
        self,
        *,
        baseline: ProductionBundleSpec,
        candidate: ProductionBundleSpec,
    ) -> None: ...

    def create(
        self,
        spec: ProductionBundleSpec,
    ) -> HotNewsEvaluationRunner: ...


class FastGPTBundleEvaluationRunnerFactory:
    """Legacy factory for direct evaluator tests.

    Production Data Loop assembly uses ``ProductionBundleRuntimeRegistry``.
    This compatibility factory deliberately refuses transitions because an App
    id alone cannot prove that the remaining Bundle assets were executed.
    """

    def __init__(self, client: FastGPTClient) -> None:
        self._client = client

    def create(
        self,
        spec: ProductionBundleSpec,
    ) -> HotNewsEvaluationRunner:
        return HotNewsAnalysisAgentRunner(
            self._client,
            spec.fastgpt_app_id,
        )

    def ensure_supported(self, spec: ProductionBundleSpec) -> None:
        # Retained only for direct legacy tests. Transition validation below
        # prevents this factory from approving a changed Bundle.
        if not spec.fastgpt_app_id.strip():
            raise ValueError("FastGPT app id cannot be empty")

    def ensure_transition_supported(
        self,
        *,
        baseline: ProductionBundleSpec,
        candidate: ProductionBundleSpec,
    ) -> None:
        if baseline != candidate:
            raise ValueError(
                "production replay requires a full Bundle runtime registry; "
                "the legacy FastGPT-only factory cannot evaluate transitions"
            )


@dataclass(frozen=True, slots=True)
class OfflineReplayResult:
    suite_metrics: EvaluationSuiteMetrics
    artifact_payload: dict[str, Any]


class HotNewsOfflineReplayService:
    """Run candidate and baselines on exactly the same immutable inputs."""

    def __init__(
        self,
        runner_factory: BundleEvaluationRunnerFactory,
        *,
        evaluator_version: str = OFFLINE_REPLAY_VERSION,
        max_concurrency: int = 8,
        max_cases_per_cohort: int = 20,
    ) -> None:
        if not evaluator_version.strip():
            raise ValueError("evaluator_version cannot be empty")
        self._factory = runner_factory
        self.evaluator_version = evaluator_version
        if not 1 <= max_concurrency <= 32:
            raise ValueError("max_concurrency must be between 1 and 32")
        if not 1 <= max_cases_per_cohort <= 100:
            raise ValueError("max_cases_per_cohort must be between 1 and 100")
        self.max_concurrency = max_concurrency
        self.max_cases_per_cohort = max_cases_per_cohort

    def ensure_runtime_supported(self, spec: ProductionBundleSpec) -> None:
        self._factory.ensure_supported(spec)

    async def evaluate(
        self,
        *,
        manifests: tuple[
            EvaluationDatasetManifest,
            EvaluationDatasetManifest,
            EvaluationDatasetManifest,
        ],
        candidate_spec: ProductionBundleSpec,
        online_baseline_spec: ProductionBundleSpec,
        previous_experiment_spec: ProductionBundleSpec | None = None,
    ) -> OfflineReplayResult:
        by_layer = {manifest.dataset_layer: manifest for manifest in manifests}
        expected_layers = {
            "golden",
            "fresh_bad_case",
            "high_risk_regression",
        }
        if set(by_layer) != expected_layers:
            raise ValueError(
                "offline replay requires one distinct manifest per cohort layer"
            )

        oversized = {
            layer: len(manifest.cases)
            for layer, manifest in by_layer.items()
            if len(manifest.cases) > self.max_cases_per_cohort
        }
        if oversized:
            raise ValueError(
                "offline replay cohort exceeds the bounded case limit "
                f"{self.max_cases_per_cohort}: {oversized}"
            )

        self._factory.ensure_transition_supported(
            baseline=online_baseline_spec,
            candidate=candidate_spec,
        )
        if previous_experiment_spec is not None:
            self._factory.ensure_transition_supported(
                baseline=online_baseline_spec,
                candidate=previous_experiment_spec,
            )

        cohorts: dict[str, EvaluationCohortMetrics] = {}
        artifact_reports: dict[str, Any] = {}
        ordered_layers = (
            "golden",
            "fresh_bad_case",
            "high_risk_regression",
        )
        specs: tuple[tuple[str, ProductionBundleSpec], ...] = (
            ("candidate", candidate_spec),
            ("online_baseline", online_baseline_spec),
            *((
                ("previous_experiment", previous_experiment_spec),
            ) if previous_experiment_spec is not None else ()),
        )
        limiter = asyncio.Semaphore(self.max_concurrency)
        pending = {
            (layer, role): asyncio.create_task(
                HotNewsEvaluationService(
                    self._factory.create(spec),
                    concurrency_limiter=limiter,
                ).evaluate(to_hot_news_evaluation_dataset(by_layer[layer]))
            )
            for layer in ordered_layers
            for role, spec in specs
        }
        try:
            completed_reports = await asyncio.gather(*pending.values())
        except BaseException:
            # Do not leave model calls running after one infrastructure failure
            # has already made this bounded replay attempt unusable.
            for task in pending.values():
                task.cancel()
            await asyncio.gather(*pending.values(), return_exceptions=True)
            raise
        reports = {
            key: report
            for key, report in zip(
                pending,
                completed_reports,
                strict=True,
            )
        }

        for layer in ordered_layers:
            manifest = by_layer[layer]
            candidate_report = reports[(layer, "candidate")]
            baseline_report = reports[(layer, "online_baseline")]
            previous_report = reports.get((layer, "previous_experiment"))

            cohorts[layer] = EvaluationCohortMetrics(
                dataset_id=manifest.dataset_id,
                layer=layer,
                candidate=self._to_metric_set(manifest, candidate_report),
                online_baseline=self._to_metric_set(
                    manifest, baseline_report
                ),
                previous_experiment=(
                    None
                    if previous_report is None
                    else self._to_metric_set(manifest, previous_report)
                ),
            )
            artifact_reports[layer] = {
                "dataset_id": str(manifest.dataset_id),
                "dataset_sha256": manifest.content_sha256,
                "candidate": self._safe_report(candidate_report),
                "online_baseline": self._safe_report(baseline_report),
                "previous_experiment": (
                    None
                    if previous_report is None
                    else self._safe_report(previous_report)
                ),
            }

        suite = EvaluationSuiteMetrics(
            golden=cohorts["golden"],
            fresh_bad_case=cohorts["fresh_bad_case"],
            high_risk_regression=cohorts["high_risk_regression"],
        )
        return OfflineReplayResult(
            suite_metrics=suite,
            artifact_payload={
                "schema_version": "1.0",
                "evaluator_version": self.evaluator_version,
                "candidate_spec": candidate_spec.model_dump(mode="json"),
                "online_baseline_spec": online_baseline_spec.model_dump(
                    mode="json"
                ),
                "previous_experiment_spec": (
                    None
                    if previous_experiment_spec is None
                    else previous_experiment_spec.model_dump(mode="json")
                ),
                "suite_metrics": suite.model_dump(mode="json"),
                "reports": artifact_reports,
            },
        )

    @staticmethod
    def _to_metric_set(
        manifest: EvaluationDatasetManifest,
        report: HotNewsEvaluationReport,
    ) -> EvaluationMetricSet:
        if len(manifest.cases) != report.total_cases:
            raise ValueError("evaluation report case count does not match manifest")
        severity_by_case = {
            case.case_id: case.severity for case in manifest.cases
        }
        evidence_clean = sum(
            item.forbidden_evidence_count == 0 for item in report.cases
        )
        critical_failures = sum(
            (not item.contract_valid)
            or (
                severity_by_case.get(item.case_id) == "critical"
                and not item.passed
            )
            for item in report.cases
        )
        return EvaluationMetricSet(
            case_count=report.total_cases,
            passed_count=report.passed_cases,
            pass_rate=report.pass_rate,
            evidence_precision=evidence_clean / report.total_cases,
            metric_coverage=report.mean_required_metric_coverage,
            critical_failures=critical_failures,
        )

    @staticmethod
    def _safe_report(report: HotNewsEvaluationReport) -> dict[str, Any]:
        """Persist score evidence without raw model text or error messages."""

        return {
            "dataset_name": report.dataset_name,
            "dataset_version": report.dataset_version,
            "dataset_sha256": report.dataset_sha256,
            "total_cases": report.total_cases,
            "passed_cases": report.passed_cases,
            "pass_rate": report.pass_rate,
            "contract_pass_rate": report.contract_pass_rate,
            "dominant_driver_accuracy": report.dominant_driver_accuracy,
            "mean_required_evidence_recall": (
                report.mean_required_evidence_recall
            ),
            "mean_required_metric_coverage": (
                report.mean_required_metric_coverage
            ),
            "cases": [
                item.model_dump(mode="json", exclude={"error_message"})
                for item in report.cases
            ],
        }
