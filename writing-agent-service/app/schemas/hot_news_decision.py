from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

DecisionType = Literal[
    "accepted",
    "rejected",
    "deferred",
    "corrected",
]

"""记录热点新闻执行命令"""
class RecordHotNewsDecisionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    news_id: str = Field(min_length=1, max_length=128)
    decision_type: DecisionType
    reason: str = Field(min_length=1, max_length=128)
    correction_payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=128)
    supersedes_decision_id: UUID | None = None

    @model_validator(mode="after")
    def validate_correction(self):
        if self.decision_type == "corrected":
            if not self.correction_payload:
                raise ValueError(
                    "corrected decision requires correction_payload"
                )
        elif self.correction_payload:
            raise ValueError(
                "correction_payload is only allowed for corrected decisions"
            )

        return self