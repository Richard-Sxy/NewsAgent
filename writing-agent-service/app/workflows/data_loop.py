"""Bounded feedback-to-production-bundle Temporal workflow."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.workflows.data_loop_contracts import (
        DataLoopHumanDecision,
        DataLoopRunRequest,
        DataLoopStepCommand,
        DataLoopStepOutcome,
        DataLoopWorkflowResult,
        DataLoopWorkflowSnapshot,
    )


DATA_LOOP_WORKFLOW_NAME = "hot-news-data-loop-v1"
DATA_LOOP_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2,
    maximum_interval=timedelta(seconds=30),
    # Side-effecting operations get one retry at most.
    maximum_attempts=2,
)
DATA_LOOP_APPROVAL_TIMEOUT = timedelta(hours=48)


@workflow.defn(name=DATA_LOOP_WORKFLOW_NAME)
class HotNewsDataLoopWorkflow:
    """Freeze, evaluate and wait for a human-controlled bundle promotion."""

    def __init__(self) -> None:
        self._tenant_id = ""
        self._phase = "created"
        self._dataset_id: str | None = None
        self._evaluation_run_id: str | None = None
        self._gate_passed: bool | None = None
        self._decision: DataLoopHumanDecision | None = None

    @workflow.run
    async def run(self, request: DataLoopRunRequest) -> DataLoopWorkflowResult:
        request.validate()
        self._tenant_id = request.tenant_id

        frozen = await self._execute(
            request,
            step_type="freeze_dataset",
            step_key="freeze-dataset-v1",
            inputs={
                "window_start": request.window_start.isoformat(),
                "window_end": request.window_end.isoformat(),
                "dataset_name": request.dataset_name,
                "dataset_version": request.dataset_version,
            },
        )
        self._dataset_id = self._required_string(frozen, "dataset_id")

        # Attribution helps people understand failures but is not an authority
        # for deterministic scoring, therefore its failure may degrade.
        attribution_status = "completed"
        try:
            await self._execute(
                request,
                step_type="attribute_errors",
                step_key="attribute-errors-v1",
                inputs={"dataset_id": self._dataset_id},
            )
        except Exception:
            attribution_status = "degraded"

        evaluated = await self._execute(
            request,
            step_type="evaluate_candidate",
            step_key="evaluate-candidate-v1",
            inputs={
                "dataset_id": self._dataset_id,
                "golden_dataset_id": request.golden_dataset_id,
                "high_risk_regression_dataset_id": (
                    request.high_risk_regression_dataset_id
                ),
                "candidate_id": request.candidate_id,
                "previous_experiment_candidate_id": (
                    request.previous_experiment_candidate_id
                ),
                "evaluation_policy_version": request.evaluation_policy_version,
                "attribution_status": attribution_status,
            },
        )
        self._evaluation_run_id = self._required_string(
            evaluated,
            "evaluation_run_id",
        )
        self._gate_passed = bool(evaluated.values.get("gate_passed", False))
        if not self._gate_passed:
            self._phase = "evaluation_failed"
            return DataLoopWorkflowResult(
                status="evaluation_failed",
                candidate_id=request.candidate_id,
                dataset_id=self._dataset_id,
                evaluation_run_id=self._evaluation_run_id,
                reason=str(evaluated.values.get("reason") or "evaluation gate failed"),
            )

        self._phase = "waiting_approval"
        decision = await self._wait_for_decision()
        if decision is None:
            await self._record_decision(
                request,
                action="reject",
                actor_id="system",
                reason="approval timeout; default is no promotion",
                idempotency_key=f"{request.idempotency_key}:approval-timeout",
            )
            self._phase = "approval_expired"
            return DataLoopWorkflowResult(
                status="approval_expired",
                candidate_id=request.candidate_id,
                dataset_id=self._dataset_id,
                evaluation_run_id=self._evaluation_run_id,
                reason="approval timeout; candidate was not activated",
            )

        decision.validate()
        await self._record_decision(
            request,
            action=decision.action,
            actor_id=decision.actor_id,
            reason=decision.reason,
            idempotency_key=decision.idempotency_key,
        )
        if decision.action == "reject":
            self._phase = "rejected"
            return DataLoopWorkflowResult(
                status="rejected",
                candidate_id=request.candidate_id,
                dataset_id=self._dataset_id,
                evaluation_run_id=self._evaluation_run_id,
                reason=decision.reason,
            )

        activated = await self._execute(
            request,
            step_type="activate_bundle",
            step_key="activate-bundle-v1",
            inputs={
                "candidate_id": request.candidate_id,
                "evaluation_run_id": self._evaluation_run_id,
                "actor_id": decision.actor_id,
                "reason": decision.reason,
                "idempotency_key": f"{decision.idempotency_key}:activate",
            },
        )
        production_bundle_id = self._required_string(
            activated,
            "production_bundle_id",
        )
        self._phase = "activated"
        return DataLoopWorkflowResult(
            status="activated",
            candidate_id=request.candidate_id,
            dataset_id=self._dataset_id,
            evaluation_run_id=self._evaluation_run_id,
            production_bundle_id=production_bundle_id,
        )

    async def _wait_for_decision(self) -> DataLoopHumanDecision | None:
        try:
            await workflow.wait_condition(
                lambda: self._decision is not None,
                timeout=DATA_LOOP_APPROVAL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return None
        return self._decision

    async def _record_decision(
        self,
        request: DataLoopRunRequest,
        *,
        action: str,
        actor_id: str,
        reason: str,
        idempotency_key: str,
    ) -> None:
        await self._execute(
            request,
            step_type="record_promotion_decision",
            step_key="record-promotion-decision-v1",
            inputs={
                "candidate_id": request.candidate_id,
                "evaluation_run_id": self._evaluation_run_id,
                "action": action,
                "actor_id": actor_id,
                "reason": reason,
                "idempotency_key": idempotency_key,
            },
        )

    async def _execute(
        self,
        request: DataLoopRunRequest,
        *,
        step_type: str,
        step_key: str,
        inputs: dict,
    ) -> DataLoopStepOutcome:
        self._phase = step_type
        return await workflow.execute_activity(
            "run_data_loop_step",
            DataLoopStepCommand(
                tenant_id=request.tenant_id,
                run_idempotency_key=request.idempotency_key,
                step_type=step_type,
                step_key=step_key,
                inputs=inputs,
            ),
            start_to_close_timeout=timedelta(minutes=60),
            schedule_to_close_timeout=timedelta(minutes=90),
            retry_policy=DATA_LOOP_RETRY_POLICY,
            result_type=DataLoopStepOutcome,
        )

    @workflow.signal
    def submit_promotion_decision(self, decision: DataLoopHumanDecision) -> None:
        if self._phase != "waiting_approval" or self._decision is not None:
            return
        self._decision = decision

    @workflow.query
    def snapshot(self) -> DataLoopWorkflowSnapshot:
        return DataLoopWorkflowSnapshot(
            tenant_id=self._tenant_id,
            phase=self._phase,
            dataset_id=self._dataset_id,
            evaluation_run_id=self._evaluation_run_id,
            gate_passed=self._gate_passed,
            waiting_for_approval=self._phase == "waiting_approval",
        )

    @staticmethod
    def _required_string(outcome: DataLoopStepOutcome, key: str) -> str:
        value = outcome.values.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Data Loop step {outcome.step_key!r} omitted {key!r}")
        return value
