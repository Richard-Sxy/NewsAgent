from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.schemas.production_bundle import (
    ActivateCandidateCommand,
    ApproveCandidateCommand,
    CohortGateThreshold,
    ConfigurationDiff,
    EvaluationCohortMetrics,
    EvaluationGatePolicy,
    EvaluationMetricSet,
    EvaluationSuiteMetrics,
    ProductionBundle,
    ProductionBundleSpec,
    ProposeConfigurationCandidateCommand,
    RecordCandidateEvaluationCommand,
    RollbackProductionBundleCommand,
)
from app.services.production_bundle import (
    ProductionBundleDomainService,
    ProductionBundleRuleViolation,
    bundle_spec_sha256,
)


NOW = datetime(2026, 9, 9, 8, tzinfo=timezone.utc)


def spec(**updates) -> ProductionBundleSpec:
    values = {
        "metric_definition_version": "metric-v1",
        "hot_score_policy_version": "score-v1",
        "reranker_policy_version": "rerank-v1",
        "analysis_prompt_version": "prompt-v1",
        "fastgpt_app_id": "app-v1",
        "model_version": "model-v1",
        "output_schema_version": "schema-v1",
        "validator_version": "validator-v1",
        "memory_resolver_policy_version": "memory-v1",
    }
    values.update(updates)
    return ProductionBundleSpec(**values)


def active_bundle(**updates) -> ProductionBundle:
    bundle_spec = updates.pop("spec", spec())
    values = {
        "id": uuid4(),
        "tenant_id": "tenant-1",
        "bundle_version": "bundle-v1",
        "spec": bundle_spec,
        "content_sha256": bundle_spec_sha256(bundle_spec),
        "status": "active",
        "created_by": "operator-0",
        "created_at": NOW - timedelta(days=10),
        "activated_by": "operator-0",
        "activated_at": NOW - timedelta(days=10),
    }
    values.update(updates)
    return ProductionBundle(**values)


def proposal(base: ProductionBundle, **updates):
    proposed = updates.pop(
        "proposed_spec",
        base.spec.model_copy(update={"analysis_prompt_version": "prompt-v2"}),
    )
    values = {
        "tenant_id": base.tenant_id,
        "base_bundle_id": base.id,
        "candidate_version": "bundle-v2",
        "proposed_spec": proposed,
        "structured_diff": (
            ConfigurationDiff(
                asset="analysis_prompt",
                before_version="prompt-v1",
                after_version="prompt-v2",
                reason="修复因果表述 bad case",
            ),
        ),
        "proposed_by": "agent-attribution",
        "proposal_reason": "热点归因显示提示词边界不清晰",
        "idempotency_key": "candidate-request-1",
    }
    values.update(updates)
    return ProposeConfigurationCandidateCommand(**values)


def suite() -> EvaluationSuiteMetrics:
    candidate_metric = EvaluationMetricSet(
        case_count=10,
        passed_count=10,
        pass_rate=1,
        evidence_precision=1,
        metric_coverage=1,
        critical_failures=0,
    )
    baseline_metric = candidate_metric.model_copy(
        update={"passed_count": 9, "pass_rate": 0.9}
    )

    def cohort(layer):
        return EvaluationCohortMetrics(
            dataset_id=uuid4(),
            layer=layer,
            candidate=candidate_metric,
            online_baseline=baseline_metric,
            previous_experiment=baseline_metric,
        )

    return EvaluationSuiteMetrics(
        golden=cohort("golden"),
        fresh_bad_case=cohort("fresh_bad_case"),
        high_risk_regression=cohort("high_risk_regression"),
    )


def gate_policy() -> EvaluationGatePolicy:
    threshold = CohortGateThreshold(
        min_case_count=10,
        min_pass_rate=0.9,
        min_evidence_precision=0.9,
        min_metric_coverage=0.9,
    )
    return EvaluationGatePolicy(
        policy_version="gate-v1",
        golden=threshold,
        fresh_bad_case=threshold,
        high_risk_regression=threshold,
        require_previous_experiment_baseline=True,
    )


def evaluation_command(candidate, **updates):
    metrics = updates.pop("suite_metrics", suite())
    values = {
        "tenant_id": candidate.tenant_id,
        "candidate_id": candidate.id,
        "base_bundle_id": candidate.base_bundle_id,
        "previous_experiment_candidate_id": uuid4(),
        "suite_metrics": metrics,
        "gate_policy": gate_policy(),
        "evaluator_version": "offline-replay-v1",
        "artifact_uri": "s3://evaluation/run.json",
        "artifact_sha256": "a" * 64,
        "started_at": NOW - timedelta(minutes=5),
        "completed_at": NOW - timedelta(minutes=1),
        "expected_candidate_revision": candidate.revision,
        "idempotency_key": "evaluation-request-1",
    }
    values.update(updates)
    return RecordCandidateEvaluationCommand(**values)


def test_candidate_diff_must_exactly_describe_snapshot_change() -> None:
    base = active_bundle()
    command = proposal(
        base,
        proposed_spec=base.spec.model_copy(
            update={
                "analysis_prompt_version": "prompt-v2",
                "validator_version": "validator-v2",
            }
        ),
    )

    with pytest.raises(ProductionBundleRuleViolation, match="does not match"):
        ProductionBundleDomainService().propose_candidate(
            base_bundle=base,
            command=command,
            now=NOW,
        )


def test_candidate_evaluation_and_approval_do_not_activate_bundle() -> None:
    service = ProductionBundleDomainService()
    base = active_bundle()
    candidate = service.propose_candidate(
        base_bundle=base,
        command=proposal(base),
        now=NOW,
    )
    evaluated = service.record_evaluation(
        candidate=candidate,
        command=evaluation_command(candidate),
        now=NOW,
    )
    approve = ApproveCandidateCommand(
        tenant_id=candidate.tenant_id,
        candidate_id=candidate.id,
        evaluation_run_id=evaluated.evaluation_run.id,
        approved_by="operator-1",
        reason="三组回归集与双基准均通过",
        expected_candidate_revision=evaluated.candidate.revision,
        idempotency_key="approve-request-1",
    )

    reviewed = service.approve_candidate(
        candidate=evaluated.candidate,
        evaluation_run=evaluated.evaluation_run,
        command=approve,
        now=NOW,
    )

    assert reviewed.candidate.status == "approved"
    assert base.status == "active"
    assert reviewed.decision.action == "approve"
    assert reviewed.decision.to_bundle_id is None


def test_activation_requires_approval_and_creates_audited_bundle_switch() -> None:
    service = ProductionBundleDomainService()
    base = active_bundle()
    candidate = service.propose_candidate(
        base_bundle=base,
        command=proposal(base),
        now=NOW,
    )
    evaluated = service.record_evaluation(
        candidate=candidate,
        command=evaluation_command(candidate),
        now=NOW,
    )
    activation = ActivateCandidateCommand(
        tenant_id=candidate.tenant_id,
        candidate_id=candidate.id,
        evaluation_run_id=evaluated.evaluation_run.id,
        activated_by="operator-1",
        reason="人工确认上线",
        expected_candidate_revision=evaluated.candidate.revision,
        idempotency_key="activate-request-1",
    )

    with pytest.raises(ProductionBundleRuleViolation, match="approved"):
        service.activate_candidate(
            candidate=evaluated.candidate,
            evaluation_run=evaluated.evaluation_run,
            active_bundle=base,
            command=activation,
            now=NOW,
        )

    reviewed = service.approve_candidate(
        candidate=evaluated.candidate,
        evaluation_run=evaluated.evaluation_run,
        command=ApproveCandidateCommand(
            tenant_id=candidate.tenant_id,
            candidate_id=candidate.id,
            evaluation_run_id=evaluated.evaluation_run.id,
            approved_by="operator-1",
            reason="批准候选",
            expected_candidate_revision=evaluated.candidate.revision,
            idempotency_key="approve-request-1",
        ),
        now=NOW,
    )
    activation = activation.model_copy(
        update={"expected_candidate_revision": reviewed.candidate.revision}
    )
    result = service.activate_candidate(
        candidate=reviewed.candidate,
        evaluation_run=evaluated.evaluation_run,
        active_bundle=base,
        command=activation,
        now=NOW,
    )

    assert result.previous_bundle.status == "inactive"
    assert result.activated_bundle.status == "active"
    assert result.activated_bundle.spec.analysis_prompt_version == "prompt-v2"
    assert result.activated_bundle.derived_from_bundle_id == base.id
    assert result.candidate.status == "activated"
    assert result.decision.from_bundle_id == base.id
    assert result.decision.to_bundle_id == result.activated_bundle.id


def test_explicit_rollback_reactivates_history_and_preserves_audit() -> None:
    service = ProductionBundleDomainService()
    old = active_bundle().model_copy(
        update={
            "status": "inactive",
            "deactivated_at": NOW - timedelta(days=1),
        }
    )
    current = active_bundle(
        bundle_version="bundle-v2",
        derived_from_bundle_id=old.id,
    )
    command = RollbackProductionBundleCommand(
        tenant_id="tenant-1",
        target_bundle_id=old.id,
        rolled_back_by="operator-2",
        reason="线上证据覆盖出现退化",
        idempotency_key="rollback-request-1",
    )

    result = service.rollback_bundle(
        active_bundle=current,
        target_bundle=old,
        command=command,
        now=NOW,
    )

    assert result.previous_bundle.status == "inactive"
    assert result.activated_bundle.status == "active"
    assert result.decision.action == "rollback"
    assert result.decision.from_bundle_id == current.id
    assert result.decision.to_bundle_id == old.id


def test_cross_tenant_candidate_is_hidden_as_unavailable() -> None:
    base = active_bundle()
    command = proposal(base, tenant_id="tenant-other")

    with pytest.raises(ProductionBundleRuleViolation, match="not available"):
        ProductionBundleDomainService().propose_candidate(
            base_bundle=base,
            command=command,
            now=NOW,
        )
