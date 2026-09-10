"""Stable, payload-bounded contracts for the Data Loop Temporal workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


DataLoopStepType = Literal[
    "freeze_dataset",
    "attribute_errors",
    "evaluate_candidate",
    "record_promotion_decision",
    "activate_bundle",
]
DataLoopApprovalAction = Literal["approve", "reject"]


@dataclass(frozen=True)
class DataLoopRunRequest:
    """Start one bounded feedback-to-bundle evaluation run."""

    tenant_id: str
    window_start: datetime
    window_end: datetime
    dataset_name: str
    dataset_version: str
    golden_dataset_id: str
    high_risk_regression_dataset_id: str
    candidate_id: str
    previous_experiment_candidate_id: str | None
    evaluation_policy_version: str
    idempotency_key: str

    def validate(self) -> None:
        for field_name in (
            "tenant_id",
            "dataset_name",
            "dataset_version",
            "golden_dataset_id",
            "high_risk_regression_dataset_id",
            "candidate_id",
            "evaluation_policy_version",
            "idempotency_key",
        ):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} cannot be empty")
        if self.previous_experiment_candidate_id is not None:
            if not self.previous_experiment_candidate_id.strip():
                raise ValueError(
                    "previous_experiment_candidate_id cannot be empty"
                )
        if self.golden_dataset_id == self.high_risk_regression_dataset_id:
            raise ValueError("evaluation cohort dataset ids must be distinct")
        if self.window_start.tzinfo is None or self.window_start.utcoffset() is None:
            raise ValueError("window_start must be timezone-aware")
        if self.window_end.tzinfo is None or self.window_end.utcoffset() is None:
            raise ValueError("window_end must be timezone-aware")
        if self.window_start >= self.window_end:
            raise ValueError("window_start must be less than window_end")


@dataclass(frozen=True)
class DataLoopStepCommand:
    """Idempotent command sent from Workflow to the application Activity."""

    tenant_id: str
    run_idempotency_key: str
    step_type: DataLoopStepType
    step_key: str
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DataLoopStepOutcome:
    """Small control-plane response; large data remains in PostgreSQL/S3."""

    step_key: str
    status: Literal["completed", "degraded"]
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DataLoopHumanDecision:
    """A trusted operator decision delivered to the waiting Workflow."""

    action: DataLoopApprovalAction
    actor_id: str
    reason: str
    idempotency_key: str

    def validate(self) -> None:
        if self.action not in {"approve", "reject"}:
            raise ValueError("action must be approve or reject")
        for field_name in ("actor_id", "reason", "idempotency_key"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} cannot be empty")


@dataclass(frozen=True)
class DataLoopDecisionUpdate:
    """Tenant-scoped command for the atomic promotion-decision Update."""

    tenant_id: str
    decision: DataLoopHumanDecision

    def validate(self) -> None:
        if not self.tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        self.decision.validate()


@dataclass(frozen=True)
class DataLoopDecisionUpdateResult:
    """Durable acknowledgement returned by the Temporal Update handler."""

    action: DataLoopApprovalAction
    idempotency_key: str
    created: bool


@dataclass(frozen=True)
class DataLoopWorkflowSnapshot:
    tenant_id: str
    phase: str
    dataset_id: str | None
    evaluation_run_id: str | None
    gate_passed: bool | None
    waiting_for_approval: bool


@dataclass(frozen=True)
class DataLoopActivationRecoveryContext:
    """Trusted state read from the source Workflow before activation recovery."""

    tenant_id: str
    phase: str
    candidate_id: str
    evaluation_run_id: str | None
    gate_passed: bool | None
    decision: DataLoopHumanDecision | None


@dataclass(frozen=True)
class DataLoopActivationRecoveryRequest:
    """Bounded retry of an already-approved candidate activation."""

    tenant_id: str
    source_workflow_id: str
    candidate_id: str
    evaluation_run_id: str
    approval_decision: DataLoopHumanDecision
    requested_by: str
    idempotency_key: str

    def validate(self) -> None:
        for field_name in (
            "tenant_id",
            "source_workflow_id",
            "candidate_id",
            "evaluation_run_id",
            "requested_by",
            "idempotency_key",
        ):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} cannot be empty")
        self.approval_decision.validate()
        if self.approval_decision.action != "approve":
            raise ValueError("activation recovery requires an approval decision")


@dataclass(frozen=True)
class DataLoopActivationRecoveryResult:
    status: Literal["activated"]
    source_workflow_id: str
    candidate_id: str
    evaluation_run_id: str
    production_bundle_id: str


@dataclass(frozen=True)
class DataLoopWorkflowResult:
    status: Literal[
        "activated",
        "evaluation_failed",
        "rejected",
        "approval_expired",
    ]
    candidate_id: str
    dataset_id: str | None = None
    evaluation_run_id: str | None = None
    production_bundle_id: str | None = None
    reason: str | None = None
