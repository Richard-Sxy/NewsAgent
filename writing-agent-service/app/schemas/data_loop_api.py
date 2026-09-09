"""HTTP contracts for starting and approving bounded Data Loop runs."""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator


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
