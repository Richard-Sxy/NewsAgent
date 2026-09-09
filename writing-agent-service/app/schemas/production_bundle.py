"""Data/Model Loop 的候选配置、评测门禁与生产版本契约。

本模块刻意不包含任何自动发布语义。候选通过确定性评测后仍需人工批准，
批准与激活是两个独立命令；只有显式激活或回滚命令才会改变当前生产版本。
"""

from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


NonBlank128 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
NonBlank160 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=160),
]
NonBlank500 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

ConfigurationAsset = Literal[
    "metric_definition",
    "hot_score_policy",
    "reranker_policy",
    "analysis_prompt",
    "fastgpt_app",
    "model",
    "output_schema",
    "validator",
    "memory_resolver_policy",
]
DatasetLayer = Literal[
    "golden",
    "fresh_bad_case",
    "high_risk_regression",
]
CandidateStatus = Literal[
    "pending_evaluation",
    "evaluation_passed",
    "evaluation_failed",
    "approved",
    "rejected",
    "activated",
]
BundleStatus = Literal["active", "inactive"]
EvaluationRunStatus = Literal["completed", "failed"]
PromotionAction = Literal["approve", "reject", "activate", "rollback"]


class ProductionBundleSpec(BaseModel):
    """一次线上运行所需的完整、不可变配置快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_definition_version: NonBlank128
    hot_score_policy_version: NonBlank128
    reranker_policy_version: NonBlank128
    analysis_prompt_version: NonBlank128
    fastgpt_app_id: NonBlank128
    model_version: NonBlank128
    output_schema_version: NonBlank128
    validator_version: NonBlank128
    memory_resolver_policy_version: NonBlank128


class ConfigurationDiff(BaseModel):
    """候选相对基准 Bundle 的一个受控资产版本变更。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset: ConfigurationAsset
    before_version: NonBlank128
    after_version: NonBlank128
    reason: NonBlank500

    @model_validator(mode="after")
    def require_actual_change(self) -> Self:
        if self.before_version == self.after_version:
            raise ValueError("configuration diff must change the asset version")
        return self


class ProductionBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    tenant_id: NonBlank128
    bundle_version: NonBlank128
    spec: ProductionBundleSpec
    content_sha256: Sha256Hex
    status: BundleStatus
    derived_from_bundle_id: UUID | None = None
    source_candidate_id: UUID | None = None
    created_by: NonBlank128
    created_at: AwareDatetime
    activated_by: NonBlank128
    activated_at: AwareDatetime
    deactivated_at: AwareDatetime | None = None
    revision: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.activated_at < self.created_at:
            raise ValueError("activated_at cannot be earlier than created_at")
        if (
            self.deactivated_at is not None
            and self.deactivated_at < self.activated_at
        ):
            raise ValueError("deactivated_at cannot be earlier than activated_at")
        if self.status == "active" and self.deactivated_at is not None:
            raise ValueError("active bundle cannot have deactivated_at")
        if self.status == "inactive" and self.deactivated_at is None:
            raise ValueError("inactive bundle requires deactivated_at")
        has_parent = self.derived_from_bundle_id is not None
        has_candidate = self.source_candidate_id is not None
        if has_parent != has_candidate:
            raise ValueError(
                "derived bundle requires both parent and source candidate"
            )
        return self


class ConfigurationCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    tenant_id: NonBlank128
    base_bundle_id: UUID
    candidate_version: NonBlank128
    proposed_spec: ProductionBundleSpec
    structured_diff: tuple[ConfigurationDiff, ...] = Field(min_length=1)
    content_sha256: Sha256Hex
    status: CandidateStatus = "pending_evaluation"
    proposed_by: NonBlank128
    proposal_reason: NonBlank500
    created_at: AwareDatetime
    revision: int = Field(default=1, ge=1)
    approved_evaluation_run_id: UUID | None = None

    @model_validator(mode="after")
    def validate_unique_assets_and_approval_link(self) -> Self:
        assets = [change.asset for change in self.structured_diff]
        if len(assets) != len(set(assets)):
            raise ValueError("structured_diff can contain each asset only once")
        if self.status in {"approved", "activated"}:
            if self.approved_evaluation_run_id is None:
                raise ValueError(
                    "approved candidate requires approved_evaluation_run_id"
                )
        elif self.approved_evaluation_run_id is not None:
            raise ValueError(
                "approved_evaluation_run_id is only allowed after approval"
            )
        return self


class EvaluationMetricSet(BaseModel):
    """由离线回放器确定性计算的一组指标。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_count: int = Field(ge=1)
    passed_count: int = Field(ge=0)
    pass_rate: float = Field(ge=0, le=1)
    evidence_precision: float = Field(ge=0, le=1)
    metric_coverage: float = Field(ge=0, le=1)
    critical_failures: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.passed_count > self.case_count:
            raise ValueError("passed_count cannot exceed case_count")
        expected_rate = self.passed_count / self.case_count
        if abs(self.pass_rate - expected_rate) > 1e-6:
            raise ValueError("pass_rate must equal passed_count / case_count")
        return self


class EvaluationCohortMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: UUID
    layer: DatasetLayer
    candidate: EvaluationMetricSet
    online_baseline: EvaluationMetricSet
    previous_experiment: EvaluationMetricSet | None = None

    @model_validator(mode="after")
    def require_comparable_case_counts(self) -> Self:
        expected = self.candidate.case_count
        if self.online_baseline.case_count != expected:
            raise ValueError("online baseline must evaluate the same cases")
        if (
            self.previous_experiment is not None
            and self.previous_experiment.case_count != expected
        ):
            raise ValueError("previous experiment must evaluate the same cases")
        return self


class EvaluationSuiteMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    golden: EvaluationCohortMetrics
    fresh_bad_case: EvaluationCohortMetrics
    high_risk_regression: EvaluationCohortMetrics

    @model_validator(mode="after")
    def validate_layers_and_dataset_ids(self) -> Self:
        cohorts = (
            ("golden", self.golden),
            ("fresh_bad_case", self.fresh_bad_case),
            ("high_risk_regression", self.high_risk_regression),
        )
        for expected, cohort in cohorts:
            if cohort.layer != expected:
                raise ValueError(f"{expected} cohort has an invalid layer")
        dataset_ids = [cohort.dataset_id for _, cohort in cohorts]
        if len(dataset_ids) != len(set(dataset_ids)):
            raise ValueError("evaluation cohorts must use distinct datasets")
        previous_presence = {
            cohort.previous_experiment is not None for _, cohort in cohorts
        }
        if len(previous_presence) != 1:
            raise ValueError(
                "previous experiment metrics must be present for all cohorts or none"
            )
        return self


class CohortGateThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_case_count: int = Field(default=1, ge=1)
    min_pass_rate: float = Field(default=0.0, ge=0, le=1)
    min_evidence_precision: float = Field(default=0.0, ge=0, le=1)
    min_metric_coverage: float = Field(default=0.0, ge=0, le=1)
    max_critical_failures: int = Field(default=0, ge=0)


class EvaluationGatePolicy(BaseModel):
    """门禁阈值也必须版本化并随评测快照保存。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: NonBlank128
    golden: CohortGateThreshold
    fresh_bad_case: CohortGateThreshold
    high_risk_regression: CohortGateThreshold
    max_pass_rate_regression: float = Field(default=0.0, ge=0, le=1)
    max_evidence_precision_regression: float = Field(
        default=0.0, ge=0, le=1
    )
    max_metric_coverage_regression: float = Field(default=0.0, ge=0, le=1)
    require_previous_experiment_baseline: bool = True


class EvaluationGateFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: NonBlank128
    layer: DatasetLayer
    metric: NonBlank128
    actual: float
    required: float
    baseline: Literal["absolute", "online", "previous_experiment"]
    message: NonBlank500


class EvaluationGateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    policy_version: NonBlank128
    failures: tuple[EvaluationGateFailure, ...] = ()

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.passed == bool(self.failures):
            raise ValueError("passed must be true exactly when failures is empty")
        return self


class CandidateEvaluationRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    tenant_id: NonBlank128
    candidate_id: UUID
    base_bundle_id: UUID
    previous_experiment_candidate_id: UUID | None = None
    suite_metrics: EvaluationSuiteMetrics
    gate_policy: EvaluationGatePolicy
    gate_decision: EvaluationGateDecision
    evaluator_version: NonBlank128
    status: EvaluationRunStatus
    artifact_uri: NonBlank500
    artifact_sha256: Sha256Hex
    error_type: NonBlank128 | None = None
    error_message: NonBlank500 | None = None
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_run(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot be earlier than started_at")
        has_previous = self.previous_experiment_candidate_id is not None
        metrics_have_previous = (
            self.suite_metrics.golden.previous_experiment is not None
        )
        if has_previous != metrics_have_previous:
            raise ValueError(
                "previous experiment id and metrics must be supplied together"
            )
        if self.status == "completed":
            if self.error_type is not None or self.error_message is not None:
                raise ValueError("completed evaluation cannot contain an error")
        elif self.error_type is None or self.error_message is None:
            raise ValueError("failed evaluation requires error details")
        if self.gate_decision.policy_version != self.gate_policy.policy_version:
            raise ValueError("gate decision and policy versions must match")
        return self


class PromotionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    tenant_id: NonBlank128
    action: PromotionAction
    candidate_id: UUID | None = None
    evaluation_run_id: UUID | None = None
    from_bundle_id: UUID | None = None
    to_bundle_id: UUID | None = None
    actor_id: NonBlank128
    reason: NonBlank500
    idempotency_key: NonBlank160
    request_fingerprint: Sha256Hex
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_action_links(self) -> Self:
        has_candidate = self.candidate_id is not None
        has_evaluation = self.evaluation_run_id is not None
        has_from = self.from_bundle_id is not None
        has_to = self.to_bundle_id is not None
        expected = {
            "approve": (True, True, False, False),
            "reject": (True, False, False, False),
            "activate": (True, True, True, True),
            "rollback": (False, False, True, True),
        }[self.action]
        if (has_candidate, has_evaluation, has_from, has_to) != expected:
            raise ValueError("promotion decision links do not match action")
        if has_from and self.from_bundle_id == self.to_bundle_id:
            raise ValueError("bundle transition source and target must differ")
        return self


class ProposeConfigurationCandidateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: NonBlank128
    base_bundle_id: UUID
    candidate_version: NonBlank128
    proposed_spec: ProductionBundleSpec
    structured_diff: tuple[ConfigurationDiff, ...] = Field(min_length=1)
    proposed_by: NonBlank128
    proposal_reason: NonBlank500
    idempotency_key: NonBlank160


class RecordCandidateEvaluationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: NonBlank128
    candidate_id: UUID
    base_bundle_id: UUID
    previous_experiment_candidate_id: UUID | None = None
    suite_metrics: EvaluationSuiteMetrics
    gate_policy: EvaluationGatePolicy
    evaluator_version: NonBlank128
    artifact_uri: NonBlank500
    artifact_sha256: Sha256Hex
    started_at: AwareDatetime
    completed_at: AwareDatetime
    expected_candidate_revision: int = Field(ge=1)
    idempotency_key: NonBlank160


class ApproveCandidateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: NonBlank128
    candidate_id: UUID
    evaluation_run_id: UUID
    approved_by: NonBlank128
    reason: NonBlank500
    expected_candidate_revision: int = Field(ge=1)
    idempotency_key: NonBlank160


class RejectCandidateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: NonBlank128
    candidate_id: UUID
    rejected_by: NonBlank128
    reason: NonBlank500
    expected_candidate_revision: int = Field(ge=1)
    idempotency_key: NonBlank160


class ActivateCandidateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: NonBlank128
    candidate_id: UUID
    evaluation_run_id: UUID
    activated_by: NonBlank128
    reason: NonBlank500
    expected_candidate_revision: int = Field(ge=1)
    idempotency_key: NonBlank160


class RollbackProductionBundleCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: NonBlank128
    target_bundle_id: UUID
    rolled_back_by: NonBlank128
    reason: NonBlank500
    idempotency_key: NonBlank160
