"""HTTP contracts for starting and approving bounded Data Loop runs."""

from datetime import datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.analysis_feedback import (
    AnalysisFeedbackCase,
    AnalysisFeedbackLabel,
    AnalysisFeedbackLabelContent,
    FeedbackProblemType,
    FeedbackSeverity,
    PublicationOutcome,
)
from app.schemas.evaluation_dataset import (
    EvaluationDatasetLayer,
    EvaluationDatasetReference,
)
from app.schemas.hot_news_decision import DecisionType
from app.schemas.production_bundle import (
    ConfigurationCandidate,
    ConfigurationDiff,
    ProductionBundle,
    ProductionBundleSpec,
    PromotionDecision,
)


NonBlank128 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
IdempotencyKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=8, max_length=160),
]


class StartDataLoopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    window_start: AwareDatetime
    window_end: AwareDatetime
    dataset_name: NonBlank128
    dataset_version: NonBlank128
    golden_dataset_id: UUID
    high_risk_regression_dataset_id: UUID
    candidate_id: UUID
    previous_experiment_candidate_id: UUID | None = None
    evaluation_policy_version: NonBlank128
    idempotency_key: IdempotencyKey

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.window_start >= self.window_end:
            raise ValueError("window_start must be less than window_end")
        if self.golden_dataset_id == self.high_risk_regression_dataset_id:
            raise ValueError("evaluation cohort dataset ids must be distinct")
        return self


class StartDataLoopResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    status: Literal["accepted"] = "accepted"


class SubmitDataLoopDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["approve", "reject"]
    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
    ]
    idempotency_key: IdempotencyKey


class DataLoopSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    phase: str
    dataset_id: str | None
    evaluation_run_id: str | None
    gate_passed: bool | None
    waiting_for_approval: bool


class DataLoopDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    status: Literal["submitted"] = "submitted"
    created: bool


class RecoverDataLoopActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    idempotency_key: IdempotencyKey


class RecoverDataLoopActivationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_workflow_id: str
    recovery_workflow_id: str
    status: Literal["accepted"] = "accepted"


class RecordOperatorDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    news_id: NonBlank128
    decision_type: DecisionType
    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
    ]
    correction_payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
    ]
    supersedes_decision_id: UUID | None = None
    feedback_problem_type: FeedbackProblemType = "analysis_incorrect"
    feedback_severity: FeedbackSeverity = "medium"

    @model_validator(mode="after")
    def validate_correction(self) -> Self:
        if self.decision_type == "corrected" and not self.correction_payload:
            raise ValueError("corrected decision requires correction_payload")
        if self.decision_type != "corrected" and self.correction_payload:
            raise ValueError(
                "correction_payload is only allowed for corrected decisions"
            )
        return self


class OperatorDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: UUID
    decision_type: DecisionType
    created: bool


class FeedbackCaseListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cases: tuple[AnalysisFeedbackCase, ...]


class SubmitFeedbackLabelRequest(AnalysisFeedbackLabelContent):
    expected_previous_version: int | None = Field(default=None, ge=1)
    idempotency_key: IdempotencyKey


class ApproveFeedbackLabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_label_version: int = Field(ge=1)
    idempotency_key: IdempotencyKey


class FeedbackLabelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: AnalysisFeedbackLabel
    created: bool


class FeedbackLabelListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    labels: tuple[AnalysisFeedbackLabel, ...]


class PublicationOutcomeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: PublicationOutcome
    created: bool
    feedback_case_id: UUID | None = None


class FreezeDatasetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_name: NonBlank128
    dataset_version: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    ]
    dataset_layer: EvaluationDatasetLayer
    description: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
    ]
    feedback_case_ids: tuple[UUID, ...] = Field(min_length=1, max_length=200)
    source_cutoff_at: AwareDatetime
    idempotency_key: IdempotencyKey

    @model_validator(mode="after")
    def validate_case_ids(self) -> Self:
        if len(self.feedback_case_ids) != len(set(self.feedback_case_ids)):
            raise ValueError("feedback_case_ids cannot contain duplicates")
        return self


class FreezeDatasetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset: EvaluationDatasetReference
    created: bool


class BootstrapProductionBundleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_version: NonBlank128
    spec: ProductionBundleSpec


class ProductionBundleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle: ProductionBundle
    created: bool


class ProposeCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_bundle_id: UUID
    candidate_version: NonBlank128
    proposed_spec: ProductionBundleSpec
    structured_diff: tuple[ConfigurationDiff, ...] = Field(min_length=1)
    proposal_reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
    ]
    idempotency_key: IdempotencyKey


class CandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate: ConfigurationCandidate
    created: bool


class RollbackProductionBundleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_bundle_id: UUID
    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
    ]
    idempotency_key: IdempotencyKey


class RollbackProductionBundleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    active_bundle: ProductionBundle
    decision: PromotionDecision
    created: bool
