"""Data Loop 反馈 Case 与人工标签的严格领域契约。"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.schemas.hot_news import (
    DriverType,
    HotNewsAnalysisInput,
    HotNewsAnalysisReport,
    MetricKey,
)


NonBlank64 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
NonBlank128 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
NonBlank160 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=160),
]
NonBlank256 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]
NonBlank1000 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
]
IdempotencyKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=8, max_length=160),
]

FeedbackSource = Literal[
    "operator_rejected",
    "operator_corrected",
    "validation_failed",
    "low_confidence",
    "retrieval_error",
    "post_publish_outcome",
]
FeedbackStatus = Literal[
    "collected",
    "needs_label",
    "labeled",
    "excluded",
    "frozen",
]
FeedbackProblemType = Literal[
    "analysis_incorrect",
    "unsupported_claim",
    "metric_mismatch",
    "evidence_mismatch",
    "missing_evidence",
    "low_confidence",
    "retrieval_miss",
    "retrieval_false_positive",
    "schema_violation",
    "policy_violation",
    "outcome_underperformance",
    "other",
]
FeedbackSeverity = Literal["low", "medium", "high", "critical"]
FeedbackReferenceType = Literal[
    "analysis_run",
    "operator_decision",
    "validation_attempt",
    "retrieval_attempt",
    "publication_outcome",
]
FeedbackVerdict = Literal[
    "correct",
    "incorrect",
    "partially_correct",
    "not_evaluable",
]
FeedbackLabelApprovalStatus = Literal[
    "pending",
    "approved",
    "rejected",
    "superseded",
]


class PublicationOutcomeMetrics(BaseModel):
    """发布后效果的窗口级聚合指标，不允许携带用户明细。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    impressions: int = Field(ge=0)
    clicks: int = Field(ge=0)
    unique_users: int = Field(ge=0)
    effective_consumptions: int = Field(ge=0)
    interactions: int = Field(ge=0)
    complaints: int = Field(default=0, ge=0)
    corrections: int = Field(default=0, ge=0)


class PublicationOutcomePayload(BaseModel):
    """一个发布效果窗口的不可变业务内容。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    run_idempotency_key: NonBlank160
    news_id: NonBlank128
    external_publication_id: NonBlank256
    source_system: NonBlank128
    metric_definition_version: NonBlank64
    window_start: AwareDatetime
    window_end: AwareDatetime
    metrics: PublicationOutcomeMetrics

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.window_start >= self.window_end:
            raise ValueError("window_start must be earlier than window_end")
        return self


class RecordPublicationOutcomeCommand(PublicationOutcomePayload):
    """幂等记录一个聚合发布效果窗口。"""

    idempotency_key: IdempotencyKey


class PublicationOutcome(PublicationOutcomePayload):
    """已持久化的聚合发布效果事实。"""

    id: UUID
    tenant_id: NonBlank128
    recorded_at: AwareDatetime
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: IdempotencyKey

    @model_validator(mode="after")
    def validate_recording_order(self) -> Self:
        if self.window_end > self.recorded_at:
            raise ValueError("window_end cannot be later than recorded_at")
        return self


class FeedbackSourceReference(BaseModel):
    """一个可审计且不含原始用户行为的来源引用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_type: FeedbackReferenceType
    reference_id: NonBlank256
    request_id: NonBlank256 | None = None
    diagnostic_code: NonBlank128 | None = None
    diagnostic_summary: NonBlank1000 | None = None


class AnalysisFeedbackCasePayload(BaseModel):
    """Case 的不可变业务内容，也是内容哈希的输入。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID | None = None
    run_idempotency_key: NonBlank160
    news_id: NonBlank128
    source_type: FeedbackSource
    problem_type: FeedbackProblemType
    severity: FeedbackSeverity = "medium"
    production_bundle_version: NonBlank128
    analysis_input_snapshot: HotNewsAnalysisInput
    analysis_output_snapshot: HotNewsAnalysisReport | None = None
    source_reference: FeedbackSourceReference
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def validate_snapshot_identity(self) -> Self:
        if self.analysis_input_snapshot.news_id != self.news_id:
            raise ValueError(
                "analysis_input_snapshot.news_id must match news_id"
            )
        if (
            self.analysis_output_snapshot is not None
            and self.analysis_output_snapshot.news_id != self.news_id
        ):
            raise ValueError(
                "analysis_output_snapshot.news_id must match news_id"
            )
        if self.source_type.startswith("operator_"):
            if self.run_id is None:
                raise ValueError("operator feedback requires run_id")
            if self.source_reference.reference_type != "operator_decision":
                raise ValueError(
                    "operator feedback requires operator_decision reference"
                )
            if self.analysis_output_snapshot is None:
                raise ValueError(
                    "operator feedback requires analysis_output_snapshot"
                )
        if (
            self.source_type == "validation_failed"
            and self.source_reference.reference_type
            != "validation_attempt"
        ):
            raise ValueError(
                "validation_failed requires validation_attempt reference"
            )
        if (
            self.source_type == "low_confidence"
            and self.analysis_output_snapshot is None
        ):
            raise ValueError(
                "low_confidence requires analysis_output_snapshot"
            )
        return self


class CollectAnalysisFeedbackCommand(AnalysisFeedbackCasePayload):
    """幂等收集一条 Feedback Case。"""

    idempotency_key: IdempotencyKey


class AnalysisFeedbackCase(AnalysisFeedbackCasePayload):
    """Data Loop 中可审计的 Feedback Case。"""

    id: UUID
    tenant_id: NonBlank128
    status: FeedbackStatus
    recorded_at: AwareDatetime
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: IdempotencyKey

    @model_validator(mode="after")
    def validate_recording_order(self) -> Self:
        if self.occurred_at > self.recorded_at:
            raise ValueError("occurred_at cannot be later than recorded_at")
        return self


class AnalysisFeedbackLabelContent(BaseModel):
    """可直接用于确定性评测的强类型人工标签。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: FeedbackVerdict
    allowed_dominant_drivers: tuple[DriverType, ...] = Field(
        default=(), max_length=5
    )
    required_evidence_news_ids: tuple[NonBlank128, ...] = Field(
        default=(), max_length=20
    )
    forbidden_evidence_news_ids: tuple[NonBlank128, ...] = Field(
        default=(), max_length=20
    )
    required_metric_keys: tuple[MetricKey, ...] = Field(
        default=(), max_length=12
    )
    must_state_limitation: bool = False
    operator_comment: NonBlank1000

    @field_validator(
        "allowed_dominant_drivers",
        "required_evidence_news_ids",
        "forbidden_evidence_news_ids",
        "required_metric_keys",
    )
    @classmethod
    def validate_unique_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("label collections cannot contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_evidence_sets(self) -> Self:
        overlap = set(self.required_evidence_news_ids).intersection(
            self.forbidden_evidence_news_ids
        )
        if overlap:
            raise ValueError(
                "required and forbidden evidence news ids cannot overlap"
            )
        if (
            self.verdict != "not_evaluable"
            and not self.allowed_dominant_drivers
        ):
            raise ValueError(
                "evaluable label requires allowed_dominant_drivers"
            )
        return self


class SubmitAnalysisFeedbackLabelCommand(AnalysisFeedbackLabelContent):
    """提交新标签版本；提交后仍需人工批准。"""

    feedback_case_id: UUID
    expected_previous_version: int | None = Field(default=None, ge=1)
    labeled_by: NonBlank128
    labeled_at: AwareDatetime
    idempotency_key: IdempotencyKey


class ApproveAnalysisFeedbackLabelCommand(BaseModel):
    """批准某个当前标签版本。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    feedback_case_id: UUID
    label_id: UUID
    expected_label_version: int = Field(ge=1)
    approved_by: NonBlank128
    approved_at: AwareDatetime
    idempotency_key: IdempotencyKey


class AnalysisFeedbackLabel(AnalysisFeedbackLabelContent):
    """Feedback Case 的一个不可变标签版本。"""

    id: UUID
    tenant_id: NonBlank128
    feedback_case_id: UUID
    label_version: int = Field(ge=1)
    approval_status: FeedbackLabelApprovalStatus
    labeled_by: NonBlank128
    labeled_at: AwareDatetime
    approved_by: NonBlank128 | None = None
    approved_at: AwareDatetime | None = None
    approval_idempotency_key: IdempotencyKey | None = None
    idempotency_key: IdempotencyKey
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def validate_approval_metadata(self) -> Self:
        approval_fields = (
            self.approved_by,
            self.approved_at,
            self.approval_idempotency_key,
        )
        if self.approval_status == "approved" and any(
            item is None for item in approval_fields
        ):
            raise ValueError("approved label requires approval metadata")
        if self.approval_status in {"pending", "rejected"} and any(
            item is not None for item in approval_fields
        ):
            raise ValueError(
                "unapproved label cannot contain approval metadata"
            )
        if self.labeled_at > self.recorded_at:
            raise ValueError("labeled_at cannot be later than recorded_at")
        if (
            self.approved_at is not None
            and self.approved_at < self.labeled_at
        ):
            raise ValueError("approved_at cannot be earlier than labeled_at")
        return self
