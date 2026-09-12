"""Bounded feedback-to-production-bundle Temporal workflow."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.workflows.data_loop_contracts import (
        DataLoopActivationRecoveryContext,
        DataLoopActivationRecoveryRequest,
        DataLoopActivationRecoveryResult,
        DataLoopDecisionUpdate,
        DataLoopDecisionUpdateResult,
        DataLoopHumanDecision,
        DataLoopRunRequest,
        DataLoopStepCommand,
        DataLoopStepOutcome,
        DataLoopWorkflowResult,
        DataLoopWorkflowSnapshot,
    )


DATA_LOOP_WORKFLOW_NAME = "hot-news-data-loop-v1"
DATA_LOOP_ACTIVATION_RECOVERY_WORKFLOW_NAME = (
    "hot-news-data-loop-activation-recovery-v1"
)
DATA_LOOP_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2,
    maximum_interval=timedelta(seconds=30),
    # Side-effecting operations get one retry at most.
    maximum_attempts=2,
)
DATA_LOOP_APPROVAL_TIMEOUT = timedelta(hours=48)
DATA_LOOP_ACTIVITY_HEARTBEAT_TIMEOUT = timedelta(seconds=30)


@workflow.defn(name=DATA_LOOP_WORKFLOW_NAME)
class HotNewsDataLoopWorkflow:
    """Freeze, evaluate and wait for a human-controlled bundle promotion."""

    def __init__(self) -> None:
        self._tenant_id = ""
        self._candidate_id = ""
        self._phase = "created"
        self._dataset_id: str | None = None
        self._evaluation_run_id: str | None = None
        self._gate_passed: bool | None = None
        self._decision: DataLoopHumanDecision | None = None

    @workflow.run
    async def run(self, request: DataLoopRunRequest) -> DataLoopWorkflowResult:
        request.validate()
        self._tenant_id = request.tenant_id
        self._candidate_id = request.candidate_id

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
        attribution_values: dict = {}
        try:
            attributed = await self._execute(
                request,
                step_type="attribute_errors",
                step_key="attribute-errors-v1",
                inputs={"dataset_id": self._dataset_id},
            )
            attribution_values = attributed.values
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
                "attribution_artifact_uri": attribution_values.get(
                    "artifact_uri"
                ),
                "attribution_artifact_sha256": attribution_values.get(
                    "artifact_sha256"
                ),
            },
        )
        self._evaluation_run_id = self._required_string(
            evaluated,
            "evaluation_run_id",
        )
        self._gate_passed = self._required_bool(evaluated, "gate_passed")
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
        """DataLoop执行"""
        self._phase = step_type
        outcome = await workflow.execute_activity(
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
            heartbeat_timeout=DATA_LOOP_ACTIVITY_HEARTBEAT_TIMEOUT,
            retry_policy=DATA_LOOP_RETRY_POLICY,
            result_type=DataLoopStepOutcome,
        )
        if outcome.step_key != step_key:
            raise ValueError(
                f"Data Loop step returned mismatched key: {outcome.step_key!r}"
            )
        if outcome.status not in {"completed", "degraded"}:
            raise ValueError(
                f"Data Loop step returned invalid status: {outcome.status!r}"
            )
        return outcome

    @workflow.update
    def submit_promotion_decision(
        self,
        command: DataLoopDecisionUpdate,
    ) -> DataLoopDecisionUpdateResult:
        """Atomically accept one decision and return an idempotent receipt."""

        self._validate_promotion_decision_update(command)
        created = self._decision is None
        if created:
            self._decision = command.decision
        return DataLoopDecisionUpdateResult(
            action=command.decision.action,
            idempotency_key=command.decision.idempotency_key,
            created=created,
        )

    @submit_promotion_decision.validator
    def validate_submit_promotion_decision(
        self,
        command: DataLoopDecisionUpdate,
    ) -> None:
        self._validate_promotion_decision_update(command)

    def _validate_promotion_decision_update(
        self,
        command: DataLoopDecisionUpdate,
    ) -> None:
        command.validate()
        if command.tenant_id != self._tenant_id:
            # Preserve the API's tenant-isolated not-found behavior without a
            # query-then-signal race.
            raise ValueError("Data Loop workflow was not found")
        if self._decision is not None:
            if self._decision.idempotency_key != command.decision.idempotency_key:
                raise ValueError("a promotion decision has already been submitted")
            if self._decision != command.decision:
                raise ValueError(
                    "promotion decision idempotency key was reused with "
                    "different content"
                )
            return
        if self._phase != "waiting_approval" or self._gate_passed is not True:
            raise ValueError(
                "Data Loop workflow is not waiting at a passed evaluation gate"
            )

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

    @workflow.query
    def activation_recovery_context(self) -> DataLoopActivationRecoveryContext:
        return DataLoopActivationRecoveryContext(
            tenant_id=self._tenant_id,
            phase=self._phase,
            candidate_id=self._candidate_id,
            evaluation_run_id=self._evaluation_run_id,
            gate_passed=self._gate_passed,
            decision=self._decision,
        )

    @staticmethod
    def _required_string(outcome: DataLoopStepOutcome, key: str) -> str:
        value = outcome.values.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Data Loop step {outcome.step_key!r} omitted {key!r}")
        return value

    @staticmethod
    def _required_bool(outcome: DataLoopStepOutcome, key: str) -> bool:
        value = outcome.values.get(key)
        if not isinstance(value, bool):
            raise ValueError(
                f"Data Loop step {outcome.step_key!r} omitted boolean {key!r}"
            )
        return value


@workflow.defn(name=DATA_LOOP_ACTIVATION_RECOVERY_WORKFLOW_NAME)
class HotNewsDataLoopActivationRecoveryWorkflow:
    """Replay the original approval ledger and its authorized activation."""

    @workflow.run
    async def run(
        self,
        request: DataLoopActivationRecoveryRequest,
    ) -> DataLoopActivationRecoveryResult:
        request.validate()
        await self._execute_step(
            DataLoopStepCommand(
                tenant_id=request.tenant_id,
                run_idempotency_key=request.idempotency_key,
                step_type="record_promotion_decision",
                step_key="record-promotion-decision-v1",
                inputs={
                    "candidate_id": request.candidate_id,
                    "evaluation_run_id": request.evaluation_run_id,
                    "action": "approve",
                    "actor_id": request.approval_decision.actor_id,
                    "reason": request.approval_decision.reason,
                    "idempotency_key": (
                        request.approval_decision.idempotency_key
                    ),
                },
            )
        )
        outcome = await self._execute_step(
            DataLoopStepCommand(
                tenant_id=request.tenant_id,
                run_idempotency_key=request.idempotency_key,
                step_type="activate_bundle",
                step_key="activate-bundle-v1",
                inputs={
                    "candidate_id": request.candidate_id,
                    "evaluation_run_id": request.evaluation_run_id,
                    "actor_id": request.approval_decision.actor_id,
                    "reason": request.approval_decision.reason,
                    # This exactly matches the original Workflow's activation
                    # key, so an uncertain prior commit replays its ledger row.
                    "idempotency_key": (
                        f"{request.approval_decision.idempotency_key}:activate"
                    ),
                },
            )
        )
        production_bundle_id = HotNewsDataLoopWorkflow._required_string(
            outcome,
            "production_bundle_id",
        )
        return DataLoopActivationRecoveryResult(
            status="activated",
            source_workflow_id=request.source_workflow_id,
            candidate_id=request.candidate_id,
            evaluation_run_id=request.evaluation_run_id,
            production_bundle_id=production_bundle_id,
        )

    @staticmethod
    async def _execute_step(
        command: DataLoopStepCommand,
    ) -> DataLoopStepOutcome:
        outcome = await workflow.execute_activity(
            "run_data_loop_step",
            command,
            start_to_close_timeout=timedelta(minutes=60),
            schedule_to_close_timeout=timedelta(minutes=90),
            heartbeat_timeout=DATA_LOOP_ACTIVITY_HEARTBEAT_TIMEOUT,
            retry_policy=DATA_LOOP_RETRY_POLICY,
            result_type=DataLoopStepOutcome,
        )
        if outcome.step_key != command.step_key:
            raise ValueError(
                f"Data Loop step returned mismatched key: {outcome.step_key!r}"
            )
        if outcome.status not in {"completed", "degraded"}:
            raise ValueError(
                f"Data Loop step returned invalid status: {outcome.status!r}"
            )
        return outcome
