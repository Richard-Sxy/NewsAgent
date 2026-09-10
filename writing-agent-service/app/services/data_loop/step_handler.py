"""Concrete application handler for the bounded Data Loop Temporal Activity."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID

from app.db.session import Database
from app.repositories.evaluation_dataset import (
    PostgresEvaluationDatasetRepository,
)
from app.repositories.production_bundle import (
    PostgresProductionBundleRepository,
)
from app.schemas.error_attribution import ErrorAttributionInput
from app.schemas.evaluation_dataset import FreezeEvaluationDatasetCommand
from app.schemas.production_bundle import (
    ActivateCandidateCommand,
    ApproveCandidateCommand,
    CohortGateThreshold,
    EvaluationGatePolicy,
    RecordCandidateEvaluationCommand,
    RejectCandidateCommand,
)
from app.services.agents.error_attribution import ErrorAttributionAgentRunner
from app.services.data_loop.artifacts import DataLoopArtifactStore
from app.services.data_loop.dataset_freezer import (
    EvaluationDatasetArtifactStore,
    EvaluationDatasetFreezer,
)
from app.services.data_loop.offline_replay import HotNewsOfflineReplayService
from app.services.production_bundle import (
    ProductionBundleApplicationService,
    ProductionBundleNotFoundError,
    ProductionBundleRuleViolation,
)
from app.workflows.data_loop_contracts import (
    DataLoopStepCommand,
    DataLoopStepOutcome,
)


DEFAULT_MAX_AUTOMATIC_FREEZE_CASES = 20


class EvaluationGatePolicyRegistry:
    """Code-versioned policy registry; arbitrary request thresholds are refused."""

    def __init__(self) -> None:
        golden = CohortGateThreshold(
            min_case_count=1,
            min_pass_rate=0.90,
            min_evidence_precision=1.0,
            min_metric_coverage=0.90,
            max_critical_failures=0,
        )
        fresh = CohortGateThreshold(
            min_case_count=1,
            min_pass_rate=0.80,
            min_evidence_precision=1.0,
            min_metric_coverage=0.80,
            max_critical_failures=0,
        )
        high_risk = CohortGateThreshold(
            min_case_count=1,
            min_pass_rate=1.0,
            min_evidence_precision=1.0,
            min_metric_coverage=1.0,
            max_critical_failures=0,
        )
        common = {
            "golden": golden,
            "fresh_bad_case": fresh,
            "high_risk_regression": high_risk,
            "max_pass_rate_regression": 0.02,
            "max_evidence_precision_regression": 0.0,
            "max_metric_coverage_regression": 0.02,
        }
        self._policies = {
            "hot-news-gate-v1": EvaluationGatePolicy(
                policy_version="hot-news-gate-v1",
                require_previous_experiment_baseline=False,
                **common,
            ),
            "hot-news-gate-double-baseline-v1": EvaluationGatePolicy(
                policy_version="hot-news-gate-double-baseline-v1",
                require_previous_experiment_baseline=True,
                **common,
            ),
        }

    def get(self, version: str) -> EvaluationGatePolicy:
        try:
            return self._policies[version]
        except KeyError as exc:
            raise ProductionBundleRuleViolation(
                f"unknown evaluation gate policy: {version}"
            ) from exc


class HotNewsDataLoopStepHandler:
    """Dispatch each idempotent Workflow step to concrete domain services."""

    def __init__(
        self,
        *,
        database: Database,
        dataset_artifact_store: EvaluationDatasetArtifactStore,
        data_loop_artifact_store: DataLoopArtifactStore,
        offline_replay: HotNewsOfflineReplayService,
        error_attribution_runner: ErrorAttributionAgentRunner | None,
        bundle_service: ProductionBundleApplicationService | None = None,
        policy_registry: EvaluationGatePolicyRegistry | None = None,
        max_cases_per_cohort: int = DEFAULT_MAX_AUTOMATIC_FREEZE_CASES,
    ) -> None:
        self._database = database
        self._dataset_artifacts = dataset_artifact_store
        self._artifacts = data_loop_artifact_store
        self._offline_replay = offline_replay
        self._attribution_runner = error_attribution_runner
        self._bundles = bundle_service or ProductionBundleApplicationService()
        self._policies = policy_registry or EvaluationGatePolicyRegistry()
        if not 1 <= max_cases_per_cohort <= 100:
            raise ValueError("max_cases_per_cohort must be between 1 and 100")
        self._max_cases_per_cohort = max_cases_per_cohort

    async def execute(
        self,
        command: DataLoopStepCommand,
    ) -> DataLoopStepOutcome:
        if not command.tenant_id.strip() or not command.run_idempotency_key.strip():
            raise ValueError("Data Loop command identity cannot be empty")
        handlers = {
            "freeze_dataset": self._freeze_dataset,
            "attribute_errors": self._attribute_errors,
            "evaluate_candidate": self._evaluate_candidate,
            "record_promotion_decision": self._record_promotion_decision,
            "activate_bundle": self._activate_bundle,
        }
        try:
            handler = handlers[command.step_type]
        except KeyError as exc:
            raise ValueError(
                f"unsupported Data Loop step: {command.step_type}"
            ) from exc
        values = await handler(command)
        return DataLoopStepOutcome(
            step_key=command.step_key,
            status="completed",
            values=values,
        )

    async def _freeze_dataset(self, command: DataLoopStepCommand) -> dict:
        start = self._datetime(command.inputs, "window_start")
        end = self._datetime(command.inputs, "window_end")
        if start >= end:
            raise ValueError("feedback window must be increasing")
        async with self._database.session() as session:
            repository = PostgresEvaluationDatasetRepository(session)
            freeze_key = self._idempotency(
                command, "freeze-fresh-dataset-v1"
            )
            existing = await repository.get_by_idempotency_key(
                tenant_id=command.tenant_id,
                idempotency_key=freeze_key,
            )
            if existing is not None:
                return {
                    "dataset_id": str(existing.dataset_id),
                    "dataset_sha256": existing.content_sha256,
                    "case_count": existing.case_count,
                    "created": False,
                }
            case_ids = await repository.list_approved_case_ids_for_window(
                tenant_id=command.tenant_id,
                occurred_start=start,
                occurred_end=end,
                source_cutoff_at=end,
                limit=self._max_cases_per_cohort + 1,
            )
            if not case_ids:
                raise ProductionBundleRuleViolation(
                    "no approved feedback cases are available in the window"
                )
            if len(case_ids) > self._max_cases_per_cohort:
                raise ProductionBundleRuleViolation(
                    "feedback window exceeds the bounded "
                    f"{self._max_cases_per_cohort}-case dataset; shard it"
                )
            freezer = EvaluationDatasetFreezer(
                repository=repository,
                artifact_store=self._dataset_artifacts,
            )
            result = await freezer.freeze(
                FreezeEvaluationDatasetCommand(
                    tenant_id=command.tenant_id,
                    dataset_name=self._string(command.inputs, "dataset_name"),
                    dataset_version=self._string(
                        command.inputs, "dataset_version"
                    ),
                    dataset_layer="fresh_bad_case",
                    description=(
                        f"Data Loop approved bad cases for {start.isoformat()} "
                        f"through {end.isoformat()}"
                    ),
                    feedback_case_ids=case_ids,
                    source_cutoff_at=end,
                    frozen_by="hot-news-data-loop-workflow",
                    idempotency_key=freeze_key,
                ),
                end_prepare_transaction=session.rollback,
            )
        return {
            "dataset_id": str(result.dataset.dataset_id),
            "dataset_sha256": result.dataset.content_sha256,
            "case_count": result.dataset.case_count,
            "created": result.created,
        }

    async def _attribute_errors(self, command: DataLoopStepCommand) -> dict:
        if self._attribution_runner is None:
            raise ProductionBundleRuleViolation(
                "error attribution Agent is not configured"
            )
        dataset_id = self._uuid(command.inputs, "dataset_id")
        async with self._database.session() as session:
            freezer = EvaluationDatasetFreezer(
                repository=PostgresEvaluationDatasetRepository(session),
                artifact_store=self._dataset_artifacts,
            )
            manifest = await freezer.load_manifest(
                tenant_id=command.tenant_id,
                dataset_id=dataset_id,
            )
        result = await self._attribution_runner.run(
            ErrorAttributionInput(
                dataset_id=manifest.dataset_id,
                dataset_sha256=manifest.content_sha256,
                cases=manifest.cases,
            )
        )
        artifact = await self._artifacts.put_json(
            tenant_id=command.tenant_id,
            artifact_kind="error-attribution",
            logical_id=self._idempotency(command, "attribution-v1"),
            payload={
                "schema_version": "1.0",
                "dataset_id": str(manifest.dataset_id),
                "dataset_sha256": manifest.content_sha256,
                "request_id": result.request_id,
                "usage": result.usage,
                "report": result.value.model_dump(mode="json"),
            },
        )
        return {
            "attribution_count": len(result.value.attributions),
            "artifact_uri": artifact.storage_uri,
            "artifact_sha256": artifact.content_sha256,
        }

    async def _evaluate_candidate(self, command: DataLoopStepCommand) -> dict:
        candidate_id = self._uuid(command.inputs, "candidate_id")
        evaluation_key = self._idempotency(command, "evaluation-v1")

        # Activity retry repair: never repeat model calls after an evaluation
        # ledger entry has committed.
        async with self._database.session() as session:
            bundle_repository = PostgresProductionBundleRepository(session)
            existing = await bundle_repository.get_evaluation_by_idempotency_key(
                tenant_id=command.tenant_id,
                idempotency_key=evaluation_key,
            )
            if existing is not None:
                run, _ = existing
                if run.candidate_id != candidate_id:
                    raise ProductionBundleRuleViolation(
                        "evaluation replay belongs to another candidate"
                    )
                return {
                    "evaluation_run_id": str(run.id),
                    "gate_passed": run.gate_decision.passed,
                    "reason": self._gate_reason(run.gate_decision.failures),
                    "replayed": True,
                }

        fresh_id = self._uuid(command.inputs, "dataset_id")
        golden_id = self._uuid(command.inputs, "golden_dataset_id")
        risk_id = self._uuid(
            command.inputs, "high_risk_regression_dataset_id"
        )
        if len({fresh_id, golden_id, risk_id}) != 3:
            raise ValueError("evaluation dataset ids must be distinct")
        previous_id = self._optional_uuid(
            command.inputs.get("previous_experiment_candidate_id")
        )
        policy = self._policies.get(
            self._string(command.inputs, "evaluation_policy_version")
        )
        if policy.require_previous_experiment_baseline != (
            previous_id is not None
        ):
            raise ProductionBundleRuleViolation(
                "evaluation policy and previous-experiment baseline disagree"
            )

        # Load immutable snapshots, then release the DB transaction before the
        # potentially long external model replay.
        async with self._database.session() as session:
            dataset_repository = PostgresEvaluationDatasetRepository(session)
            freezer = EvaluationDatasetFreezer(
                repository=dataset_repository,
                artifact_store=self._dataset_artifacts,
            )
            manifests = tuple(
                [
                    await freezer.load_manifest(
                        tenant_id=command.tenant_id,
                        dataset_id=dataset_id,
                    )
                    for dataset_id in (golden_id, fresh_id, risk_id)
                ]
            )
            bundle_repository = PostgresProductionBundleRepository(session)
            candidate = await bundle_repository.get_candidate_for_update(
                tenant_id=command.tenant_id,
                candidate_id=candidate_id,
            )
            if candidate is None:
                raise ProductionBundleNotFoundError(
                    "configuration candidate is not available"
                )
            base = await bundle_repository.get_bundle_for_update(
                tenant_id=command.tenant_id,
                bundle_id=candidate.base_bundle_id,
            )
            if base is None:
                raise ProductionBundleNotFoundError(
                    "base production bundle is not available"
                )
            if base.status != "active":
                raise ProductionBundleRuleViolation(
                    "candidate base is no longer the active production bundle"
                )
            previous = None
            if previous_id is not None:
                previous = (
                    await bundle_repository.get_latest_evaluated_candidate_for_update(
                        tenant_id=command.tenant_id,
                        base_bundle_id=candidate.base_bundle_id,
                        exclude_candidate_id=candidate.id,
                    )
                )
                if previous is None:
                    raise ProductionBundleNotFoundError(
                        "no server-authoritative previous experiment is available"
                    )
                if previous.id != previous_id:
                    raise ProductionBundleRuleViolation(
                        "previous experiment must be the latest eligible "
                        "candidate selected by the server"
                    )
                if previous.status not in {
                    "evaluation_passed",
                    "approved",
                    "activated",
                }:
                    raise ProductionBundleNotFoundError(
                        "previous experiment candidate is not available"
                    )

        started_at = datetime.now(UTC)
        replay = await self._offline_replay.evaluate(
            manifests=manifests,
            candidate_spec=candidate.proposed_spec,
            online_baseline_spec=base.spec,
            previous_experiment_spec=(
                None if previous is None else previous.proposed_spec
            ),
        )
        replay.artifact_payload["error_attribution"] = {
            "status": command.inputs.get("attribution_status", "degraded"),
            "artifact_uri": command.inputs.get("attribution_artifact_uri"),
            "artifact_sha256": command.inputs.get(
                "attribution_artifact_sha256"
            ),
        }
        artifact = await self._artifacts.put_json(
            tenant_id=command.tenant_id,
            artifact_kind="candidate-evaluation",
            logical_id=evaluation_key,
            payload=replay.artifact_payload,
        )
        completed_at = datetime.now(UTC)
        record_command = RecordCandidateEvaluationCommand(
            tenant_id=command.tenant_id,
            candidate_id=candidate.id,
            base_bundle_id=candidate.base_bundle_id,
            previous_experiment_candidate_id=previous_id,
            suite_metrics=replay.suite_metrics,
            gate_policy=policy,
            evaluator_version=self._offline_replay.evaluator_version,
            artifact_uri=artifact.storage_uri,
            artifact_sha256=artifact.content_sha256,
            started_at=started_at,
            completed_at=completed_at,
            expected_candidate_revision=candidate.revision,
            idempotency_key=evaluation_key,
        )
        async with self._database.session() as session:
            outcome = await self._bundles.record_evaluation(
                session,
                command=record_command,
                now=completed_at,
            )
        return {
            "evaluation_run_id": str(outcome.evaluation_run.id),
            "gate_passed": outcome.evaluation_run.gate_decision.passed,
            "reason": self._gate_reason(
                outcome.evaluation_run.gate_decision.failures
            ),
            "artifact_uri": outcome.evaluation_run.artifact_uri,
            "artifact_sha256": outcome.evaluation_run.artifact_sha256,
            "replayed": not outcome.created,
        }

    async def _record_promotion_decision(
        self,
        command: DataLoopStepCommand,
    ) -> dict:
        candidate_id = self._uuid(command.inputs, "candidate_id")
        actor_id = self._string(command.inputs, "actor_id")
        reason = self._string(command.inputs, "reason")
        action = self._string(command.inputs, "action")
        requested_key = self._string(command.inputs, "idempotency_key")
        now = datetime.now(UTC)
        decision_key = self._stable_key(
            command.tenant_id,
            str(candidate_id),
            action,
            requested_key,
        )
        async with self._database.session() as session:
            repository = PostgresProductionBundleRepository(session)
            replay = await repository.get_decision_by_idempotency_key(
                tenant_id=command.tenant_id,
                idempotency_key=decision_key,
            )
            if replay is not None:
                if (
                    replay.action != action
                    or replay.candidate_id != candidate_id
                    or replay.actor_id != actor_id
                    or replay.reason != reason
                ):
                    raise ProductionBundleRuleViolation(
                        "promotion decision replay content does not match"
                    )
                return {
                    "decision_id": str(replay.id),
                    "candidate_status": (
                        "approved" if action == "approve" else "rejected"
                    ),
                    "created": False,
                }
            candidate = await repository.get_candidate_for_update(
                tenant_id=command.tenant_id,
                candidate_id=candidate_id,
            )
            if candidate is None:
                raise ProductionBundleNotFoundError(
                    "configuration candidate is not available"
                )
            if action == "approve":
                evaluation_id = self._uuid(
                    command.inputs, "evaluation_run_id"
                )
                outcome = await self._bundles.approve_candidate(
                    session,
                    command=ApproveCandidateCommand(
                        tenant_id=command.tenant_id,
                        candidate_id=candidate_id,
                        evaluation_run_id=evaluation_id,
                        approved_by=actor_id,
                        reason=reason,
                        expected_candidate_revision=candidate.revision,
                        idempotency_key=decision_key,
                    ),
                    now=now,
                )
            elif action == "reject":
                outcome = await self._bundles.reject_candidate(
                    session,
                    command=RejectCandidateCommand(
                        tenant_id=command.tenant_id,
                        candidate_id=candidate_id,
                        rejected_by=actor_id,
                        reason=reason,
                        expected_candidate_revision=candidate.revision,
                        idempotency_key=decision_key,
                    ),
                    now=now,
                )
            else:
                raise ValueError("promotion decision must be approve or reject")
        return {
            "decision_id": str(outcome.decision.id),
            "candidate_status": outcome.candidate.status,
            "candidate_revision": outcome.candidate.revision,
            "created": outcome.created,
        }

    async def _activate_bundle(self, command: DataLoopStepCommand) -> dict:
        candidate_id = self._uuid(command.inputs, "candidate_id")
        evaluation_id = self._uuid(command.inputs, "evaluation_run_id")
        actor_id = self._string(command.inputs, "actor_id")
        reason = self._string(command.inputs, "reason")
        activation_key = self._stable_key(
            command.tenant_id,
            str(candidate_id),
            "activate",
            self._string(command.inputs, "idempotency_key"),
        )
        now = datetime.now(UTC)
        async with self._database.session() as session:
            repository = PostgresProductionBundleRepository(session)
            replay = await repository.get_decision_by_idempotency_key(
                tenant_id=command.tenant_id,
                idempotency_key=activation_key,
            )
            if replay is not None:
                if (
                    replay.action != "activate"
                    or replay.candidate_id != candidate_id
                    or replay.evaluation_run_id != evaluation_id
                    or replay.to_bundle_id is None
                    or replay.actor_id != actor_id
                    or replay.reason != reason
                ):
                    raise ProductionBundleRuleViolation(
                        "activation replay content does not match"
                    )
                bundle = await repository.get_bundle_for_update(
                    tenant_id=command.tenant_id,
                    bundle_id=replay.to_bundle_id,
                )
                if bundle is None:
                    raise ProductionBundleNotFoundError(
                        "activated production bundle is not available"
                    )
                if bundle.status != "active":
                    raise ProductionBundleRuleViolation(
                        "activation replay targets a Bundle that is no longer "
                        "active; a new explicit roll-forward approval is required"
                    )
                return {
                    "production_bundle_id": str(bundle.id),
                    "production_bundle_version": bundle.bundle_version,
                    "created": False,
                }
            candidate = await repository.get_candidate_for_update(
                tenant_id=command.tenant_id,
                candidate_id=candidate_id,
            )
            if candidate is None:
                raise ProductionBundleNotFoundError(
                    "configuration candidate is not available"
                )
            self._offline_replay.ensure_runtime_supported(
                candidate.proposed_spec
            )
            outcome = await self._bundles.activate_candidate(
                session,
                command=ActivateCandidateCommand(
                    tenant_id=command.tenant_id,
                    candidate_id=candidate_id,
                    evaluation_run_id=evaluation_id,
                    activated_by=actor_id,
                    reason=reason,
                    expected_candidate_revision=candidate.revision,
                    idempotency_key=activation_key,
                ),
                now=now,
            )
        return {
            "production_bundle_id": str(outcome.activated_bundle.id),
            "production_bundle_version": (
                outcome.activated_bundle.bundle_version
            ),
            "created": outcome.created,
        }

    @staticmethod
    def _string(values: dict, key: str) -> str:
        value = values.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
        return value.strip()

    @classmethod
    def _uuid(cls, values: dict, key: str) -> UUID:
        try:
            return UUID(cls._string(values, key))
        except ValueError as exc:
            raise ValueError(f"{key} must be a UUID") from exc

    @staticmethod
    def _optional_uuid(value: object) -> UUID | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "previous_experiment_candidate_id must be a UUID or null"
            )
        try:
            return UUID(value)
        except ValueError as exc:
            raise ValueError(
                "previous_experiment_candidate_id must be a UUID"
            ) from exc

    @staticmethod
    def _datetime(values: dict, key: str) -> datetime:
        value = values.get(key)
        if not isinstance(value, str):
            raise ValueError(f"{key} must be an ISO-8601 datetime")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{key} must be an ISO-8601 datetime") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{key} must be timezone-aware")
        return parsed

    @staticmethod
    def _stable_key(*parts: str) -> str:
        digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
        return f"data-loop:{digest}"

    @classmethod
    def _idempotency(
        cls,
        command: DataLoopStepCommand,
        operation: str,
    ) -> str:
        return cls._stable_key(
            command.tenant_id,
            command.run_idempotency_key,
            command.step_key,
            operation,
        )

    @staticmethod
    def _gate_reason(failures: tuple) -> str:
        if not failures:
            return "all deterministic evaluation gates passed"
        return "; ".join(failure.code for failure in failures)[:500]
