"""候选配置的确定性离线评测门禁。"""

from __future__ import annotations

from app.schemas.production_bundle import (
    CohortGateThreshold,
    DatasetLayer,
    EvaluationCohortMetrics,
    EvaluationGateDecision,
    EvaluationGateFailure,
    EvaluationGatePolicy,
    EvaluationSuiteMetrics,
)


class EvaluationGate:
    """用固定阈值和双基准比较评测结果，不调用 LLM。"""

    def evaluate(
        self,
        *,
        metrics: EvaluationSuiteMetrics,
        policy: EvaluationGatePolicy,
    ) -> EvaluationGateDecision:
        failures: list[EvaluationGateFailure] = []
        cohorts = (
            ("golden", metrics.golden, policy.golden),
            (
                "fresh_bad_case",
                metrics.fresh_bad_case,
                policy.fresh_bad_case,
            ),
            (
                "high_risk_regression",
                metrics.high_risk_regression,
                policy.high_risk_regression,
            ),
        )

        for layer, cohort, threshold in cohorts:
            failures.extend(
                self._absolute_failures(
                    layer=layer,
                    cohort=cohort,
                    threshold=threshold,
                )
            )
            failures.extend(
                self._baseline_failures(
                    layer=layer,
                    cohort=cohort,
                    baseline_name="online",
                    max_pass_rate_regression=(
                        policy.max_pass_rate_regression
                    ),
                    max_evidence_precision_regression=(
                        policy.max_evidence_precision_regression
                    ),
                    max_metric_coverage_regression=(
                        policy.max_metric_coverage_regression
                    ),
                )
            )

            previous = cohort.previous_experiment
            if previous is None:
                if policy.require_previous_experiment_baseline:
                    failures.append(
                        EvaluationGateFailure(
                            code="missing_previous_experiment_baseline",
                            layer=layer,
                            metric="previous_experiment",
                            actual=0,
                            required=1,
                            baseline="previous_experiment",
                            message=(
                                f"{layer} 缺少上一实验版本的对比指标"
                            ),
                        )
                    )
            else:
                failures.extend(
                    self._baseline_failures(
                        layer=layer,
                        cohort=cohort,
                        baseline_name="previous_experiment",
                        max_pass_rate_regression=(
                            policy.max_pass_rate_regression
                        ),
                        max_evidence_precision_regression=(
                            policy.max_evidence_precision_regression
                        ),
                        max_metric_coverage_regression=(
                            policy.max_metric_coverage_regression
                        ),
                    )
                )

        return EvaluationGateDecision(
            passed=not failures,
            policy_version=policy.policy_version,
            failures=tuple(failures),
        )

    @staticmethod
    def _absolute_failures(
        *,
        layer: DatasetLayer,
        cohort: EvaluationCohortMetrics,
        threshold: CohortGateThreshold,
    ) -> list[EvaluationGateFailure]:
        candidate = cohort.candidate
        checks = (
            (
                "case_count_below_minimum",
                "case_count",
                float(candidate.case_count),
                float(threshold.min_case_count),
            ),
            (
                "pass_rate_below_minimum",
                "pass_rate",
                candidate.pass_rate,
                threshold.min_pass_rate,
            ),
            (
                "evidence_precision_below_minimum",
                "evidence_precision",
                candidate.evidence_precision,
                threshold.min_evidence_precision,
            ),
            (
                "metric_coverage_below_minimum",
                "metric_coverage",
                candidate.metric_coverage,
                threshold.min_metric_coverage,
            ),
        )
        failures = [
            EvaluationGateFailure(
                code=code,
                layer=layer,
                metric=metric,
                actual=actual,
                required=required,
                baseline="absolute",
                message=f"{layer}.{metric}={actual} 低于门禁 {required}",
            )
            for code, metric, actual, required in checks
            if actual < required
        ]
        if candidate.critical_failures > threshold.max_critical_failures:
            failures.append(
                EvaluationGateFailure(
                    code="critical_failures_above_maximum",
                    layer=layer,
                    metric="critical_failures",
                    actual=float(candidate.critical_failures),
                    required=float(threshold.max_critical_failures),
                    baseline="absolute",
                    message=(
                        f"{layer}.critical_failures="
                        f"{candidate.critical_failures} 超过门禁 "
                        f"{threshold.max_critical_failures}"
                    ),
                )
            )
        return failures

    @staticmethod
    def _baseline_failures(
        *,
        layer: DatasetLayer,
        cohort: EvaluationCohortMetrics,
        baseline_name: str,
        max_pass_rate_regression: float,
        max_evidence_precision_regression: float,
        max_metric_coverage_regression: float,
    ) -> list[EvaluationGateFailure]:
        if baseline_name == "online":
            baseline = cohort.online_baseline
            typed_baseline = "online"
        else:
            baseline = cohort.previous_experiment
            typed_baseline = "previous_experiment"
        if baseline is None:
            return []

        comparisons = (
            (
                "pass_rate",
                cohort.candidate.pass_rate,
                baseline.pass_rate - max_pass_rate_regression,
            ),
            (
                "evidence_precision",
                cohort.candidate.evidence_precision,
                baseline.evidence_precision
                - max_evidence_precision_regression,
            ),
            (
                "metric_coverage",
                cohort.candidate.metric_coverage,
                baseline.metric_coverage - max_metric_coverage_regression,
            ),
        )
        return [
            EvaluationGateFailure(
                code=f"{metric}_regressed_vs_{typed_baseline}",
                layer=layer,
                metric=metric,
                actual=actual,
                required=max(0.0, required),
                baseline=typed_baseline,
                message=(
                    f"{layer}.{metric}={actual} 低于"
                    f"{typed_baseline} 可接受下限 {max(0.0, required)}"
                ),
            )
            for metric, actual, required in comparisons
            if actual + 1e-12 < max(0.0, required)
        ]
