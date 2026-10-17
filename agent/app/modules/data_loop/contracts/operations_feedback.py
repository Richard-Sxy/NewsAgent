"""Versioned operator judgments, kept separate from reviewed evaluation labels."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


OperationsFeedbackCategory = Literal[
    "analysis_error", "editorial_judgment", "strategy_judgment", "positive_example",
]
OperationsReasonCode = Literal[
    "analysis_supported", "useful_recommendation", "insufficient_evidence",
    "fact_or_attribution_error", "low_public_value", "audience_mismatch",
    "no_material_update", "duplicate_event", "frequency_budget_exhausted",
    "timing_inappropriate", "content_expired", "copy_misleading",
    "channel_or_consent_mismatch", "risk_requires_escalation", "other_with_explanation",
]


class OperationsFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["operations-feedback-v1"] = "operations-feedback-v1"
    category: OperationsFeedbackCategory
    reason_codes: tuple[OperationsReasonCode, ...] = Field(min_length=1, max_length=15)

    @field_validator("reason_codes")
    @classmethod
    def unique_reason_codes(cls, value: tuple[OperationsReasonCode, ...]):
        if len(value) != len(set(value)):
            raise ValueError("operator reason codes cannot contain duplicates")
        return value


OPERATOR_FEEDBACK_SOURCES = (
    "operator_accepted", "operator_rejected", "operator_deferred", "operator_corrected",
)
LEGACY_OPERATOR_ERROR_SOURCES = ("operator_rejected", "operator_corrected")


def is_fresh_bad_case_eligible(*, verdict: str, source_type: str, source_reference: dict) -> bool:
    """Only reviewed errors belong in Fresh; preserve legacy error-source semantics."""
    if verdict not in {"incorrect", "partially_correct"}:
        return False
    if source_type not in OPERATOR_FEEDBACK_SOURCES:
        return True
    feedback = source_reference.get("operator_feedback")
    if feedback is None:
        return source_type in LEGACY_OPERATOR_ERROR_SOURCES
    return isinstance(feedback, dict) and feedback.get("category") == "analysis_error"
