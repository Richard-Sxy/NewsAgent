from uuid import uuid4

from app.schemas.production_bundle import (
    CohortGateThreshold,
    EvaluationCohortMetrics,
    EvaluationGatePolicy,
    EvaluationMetricSet,
    EvaluationSuiteMetrics,
)
from app.services.data_loop.evaluation_gate import EvaluationGate


def metric_set(
    *,
    passed: int = 9,
    total: int = 10,
    evidence_precision: float = 0.95,
    metric_coverage: float = 0.95,
    critical_failures: int = 0,
) -> EvaluationMetricSet:
    return EvaluationMetricSet(
        case_count=total,
        passed_count=passed,
        pass_rate=passed / total,
        evidence_precision=evidence_precision,
        metric_coverage=metric_coverage,
        critical_failures=critical_failures,
    )


def suite(
    *,
    candidate: EvaluationMetricSet | None = None,
    online: EvaluationMetricSet | None = None,
    previous: EvaluationMetricSet | None = None,
) -> EvaluationSuiteMetrics:
    candidate = candidate or metric_set()
    online = online or metric_set(passed=8)
    previous = previous or metric_set(passed=8)

    def cohort(layer):
        return EvaluationCohortMetrics(
            dataset_id=uuid4(),
            layer=layer,
            candidate=candidate,
            online_baseline=online,
            previous_experiment=previous,
        )

    return EvaluationSuiteMetrics(
        golden=cohort("golden"),
        fresh_bad_case=cohort("fresh_bad_case"),
        high_risk_regression=cohort("high_risk_regression"),
    )


def policy(**updates) -> EvaluationGatePolicy:
    threshold = CohortGateThreshold(
        min_case_count=10,
        min_pass_rate=0.8,
        min_evidence_precision=0.9,
        min_metric_coverage=0.9,
        max_critical_failures=0,
    )
    values = {
        "policy_version": "gate-v1",
        "golden": threshold,
        "fresh_bad_case": threshold,
        "high_risk_regression": threshold,
        "max_pass_rate_regression": 0,
        "max_evidence_precision_regression": 0,
        "max_metric_coverage_regression": 0,
        "require_previous_experiment_baseline": True,
    }
    values.update(updates)
    return EvaluationGatePolicy(**values)


def test_gate_passes_all_three_datasets_and_both_baselines() -> None:
    decision = EvaluationGate().evaluate(metrics=suite(), policy=policy())

    assert decision.passed is True
    assert decision.failures == ()
    assert decision.policy_version == "gate-v1"


def test_gate_reports_absolute_and_double_baseline_regressions() -> None:
    candidate = metric_set(
        passed=7,
        evidence_precision=0.7,
        metric_coverage=0.7,
        critical_failures=1,
    )
    baseline = metric_set(
        passed=9,
        evidence_precision=0.95,
        metric_coverage=0.95,
    )

    decision = EvaluationGate().evaluate(
        metrics=suite(
            candidate=candidate,
            online=baseline,
            previous=baseline,
        ),
        policy=policy(),
    )

    assert decision.passed is False
    codes = {failure.code for failure in decision.failures}
    assert "pass_rate_below_minimum" in codes
    assert "critical_failures_above_maximum" in codes
    assert "pass_rate_regressed_vs_online" in codes
    assert "pass_rate_regressed_vs_previous_experiment" in codes
    assert {failure.layer for failure in decision.failures} == {
        "golden",
        "fresh_bad_case",
        "high_risk_regression",
    }


def test_previous_baseline_is_required_by_policy() -> None:
    value = suite()
    without_previous = value.model_copy(
        update={
            "golden": value.golden.model_copy(
                update={"previous_experiment": None}
            ),
            "fresh_bad_case": value.fresh_bad_case.model_copy(
                update={"previous_experiment": None}
            ),
            "high_risk_regression": value.high_risk_regression.model_copy(
                update={"previous_experiment": None}
            ),
        }
    )

    decision = EvaluationGate().evaluate(
        metrics=without_previous,
        policy=policy(),
    )

    assert decision.passed is False
    assert [failure.code for failure in decision.failures].count(
        "missing_previous_experiment_baseline"
    ) == 3


def test_policy_can_allow_a_bounded_regression() -> None:
    candidate = metric_set(
        passed=8,
        evidence_precision=0.91,
        metric_coverage=0.91,
    )
    baseline = metric_set(
        passed=9,
        evidence_precision=0.95,
        metric_coverage=0.95,
    )

    decision = EvaluationGate().evaluate(
        metrics=suite(
            candidate=candidate,
            online=baseline,
            previous=baseline,
        ),
        policy=policy(
            max_pass_rate_regression=0.1,
            max_evidence_precision_regression=0.05,
            max_metric_coverage_regression=0.05,
        ),
    )

    assert decision.passed is True
