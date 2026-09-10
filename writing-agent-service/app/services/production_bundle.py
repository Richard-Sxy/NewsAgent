"""候选配置评测、人工审批、激活和回滚的纯领域规则。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.production_bundle import (
    ActivateCandidateCommand,
    ApproveCandidateCommand,
    CandidateEvaluationRun,
    ConfigurationCandidate,
    EvaluationGateDecision,
    ProductionBundle,
    ProductionBundleSpec,
    PromotionDecision,
    ProposeConfigurationCandidateCommand,
    RecordCandidateEvaluationCommand,
    RejectCandidateCommand,
    RollbackProductionBundleCommand,
)
from app.services.data_loop.evaluation_gate import EvaluationGate
from app.repositories.production_bundle import (
    PostgresProductionBundleRepository,
)


class ProductionBundleRuleViolation(ValueError):
    """候选、审批或生产版本不满足领域约束。"""


class ProductionBundleNotFoundError(LookupError):
    """租户作用域内的 Bundle、Candidate 或 Evaluation 不存在。"""


class ProductionBundleConflictError(RuntimeError):
    """幂等内容、状态或乐观锁发生冲突。"""


class ProductionBundlePersistenceError(RuntimeError):
    """生产 Bundle 账本持久化失败。"""

    retryable = True


ASSET_SPEC_FIELDS = {
    "metric_definition": "metric_definition_version",
    "hot_score_policy": "hot_score_policy_version",
    "reranker_policy": "reranker_policy_version",
    "analysis_prompt": "analysis_prompt_version",
    "fastgpt_app": "fastgpt_app_id",
    "model": "model_version",
    "output_schema": "output_schema_version",
    "validator": "validator_version",
    "memory_resolver_policy": "memory_resolver_policy_version",
}


def canonical_sha256(value: BaseModel | dict[str, Any]) -> str:
    """对结构化快照使用稳定 JSON 编码计算内容哈希。"""

    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def command_fingerprint(command: BaseModel) -> str:
    payload = command.model_dump(mode="json", exclude={"idempotency_key"})
    return canonical_sha256(payload)


def bundle_spec_sha256(spec: ProductionBundleSpec) -> str:
    return canonical_sha256(spec)


@dataclass(frozen=True, slots=True)
class CandidateEvaluationResult:
    candidate: ConfigurationCandidate
    evaluation_run: CandidateEvaluationRun


@dataclass(frozen=True, slots=True)
class CandidateReviewResult:
    candidate: ConfigurationCandidate
    decision: PromotionDecision


@dataclass(frozen=True, slots=True)
class CandidateActivationResult:
    candidate: ConfigurationCandidate
    previous_bundle: ProductionBundle
    activated_bundle: ProductionBundle
    decision: PromotionDecision


@dataclass(frozen=True, slots=True)
class BundleRollbackResult:
    previous_bundle: ProductionBundle
    activated_bundle: ProductionBundle
    decision: PromotionDecision


@dataclass(frozen=True, slots=True)
class CandidateWriteOutcome:
    candidate: ConfigurationCandidate
    created: bool


@dataclass(frozen=True, slots=True)
class BundleWriteOutcome:
    bundle: ProductionBundle
    created: bool


@dataclass(frozen=True, slots=True)
class EvaluationWriteOutcome:
    candidate: ConfigurationCandidate
    evaluation_run: CandidateEvaluationRun
    created: bool


@dataclass(frozen=True, slots=True)
class ReviewWriteOutcome:
    candidate: ConfigurationCandidate
    decision: PromotionDecision
    created: bool


@dataclass(frozen=True, slots=True)
class ActivationWriteOutcome:
    candidate: ConfigurationCandidate
    previous_bundle: ProductionBundle
    activated_bundle: ProductionBundle
    decision: PromotionDecision
    created: bool


@dataclass(frozen=True, slots=True)
class RollbackWriteOutcome:
    previous_bundle: ProductionBundle
    activated_bundle: ProductionBundle
    decision: PromotionDecision
    created: bool


class ProductionBundleDomainService:
    """构造应在同一数据库事务中持久化的状态变化。"""

    def __init__(self, gate: EvaluationGate | None = None) -> None:
        self._gate = gate or EvaluationGate()

    def propose_candidate(
        self,
        *,
        base_bundle: ProductionBundle,
        command: ProposeConfigurationCandidateCommand,
        now: datetime,
        candidate_id: UUID | None = None,
    ) -> ConfigurationCandidate:
        self._require_aware(now)
        if (
            base_bundle.tenant_id != command.tenant_id
            or base_bundle.id != command.base_bundle_id
        ):
            raise ProductionBundleRuleViolation(
                "base production bundle is not available"
            )
        if base_bundle.status != "active":
            raise ProductionBundleRuleViolation(
                "candidate must be based on the active production bundle"
            )
        if command.candidate_version == base_bundle.bundle_version:
            raise ProductionBundleRuleViolation(
                "candidate version must differ from base bundle version"
            )
        self._validate_structured_diff(
            base=base_bundle.spec,
            proposed=command.proposed_spec,
            changes=command.structured_diff,
        )

        content = {
            "tenant_id": command.tenant_id,
            "base_bundle_id": str(command.base_bundle_id),
            "candidate_version": command.candidate_version,
            "proposed_spec": command.proposed_spec.model_dump(mode="json"),
            "structured_diff": [
                change.model_dump(mode="json")
                for change in command.structured_diff
            ],
        }
        return ConfigurationCandidate(
            id=candidate_id or uuid4(),
            tenant_id=command.tenant_id,
            base_bundle_id=command.base_bundle_id,
            candidate_version=command.candidate_version,
            proposed_spec=command.proposed_spec,
            structured_diff=command.structured_diff,
            content_sha256=canonical_sha256(content),
            status="pending_evaluation",
            proposed_by=command.proposed_by,
            proposal_reason=command.proposal_reason,
            created_at=now,
        )

    def record_evaluation(
        self,
        *,
        candidate: ConfigurationCandidate,
        command: RecordCandidateEvaluationCommand,
        now: datetime,
        evaluation_run_id: UUID | None = None,
    ) -> CandidateEvaluationResult:
        self._require_aware(now)
        self._validate_candidate_command_scope(
            candidate=candidate,
            tenant_id=command.tenant_id,
            candidate_id=command.candidate_id,
            expected_revision=command.expected_candidate_revision,
        )
        if candidate.base_bundle_id != command.base_bundle_id:
            raise ProductionBundleRuleViolation(
                "evaluation base bundle does not match candidate"
            )
        if candidate.status not in {
            "pending_evaluation",
            "evaluation_failed",
        }:
            raise ProductionBundleRuleViolation(
                "candidate is not available for evaluation"
            )
        if command.completed_at > now:
            raise ProductionBundleRuleViolation(
                "evaluation completion cannot be in the future"
            )

        gate_decision = self._gate.evaluate(
            metrics=command.suite_metrics,
            policy=command.gate_policy,
        )
        run = CandidateEvaluationRun(
            id=evaluation_run_id or uuid4(),
            tenant_id=command.tenant_id,
            candidate_id=candidate.id,
            base_bundle_id=candidate.base_bundle_id,
            previous_experiment_candidate_id=(
                command.previous_experiment_candidate_id
            ),
            suite_metrics=command.suite_metrics,
            gate_policy=command.gate_policy,
            gate_decision=gate_decision,
            evaluator_version=command.evaluator_version,
            status="completed",
            artifact_uri=command.artifact_uri,
            artifact_sha256=command.artifact_sha256,
            started_at=command.started_at,
            completed_at=command.completed_at,
        )
        updated_candidate = candidate.model_copy(
            update={
                "status": (
                    "evaluation_passed"
                    if gate_decision.passed
                    else "evaluation_failed"
                ),
                "revision": candidate.revision + 1,
            }
        )
        return CandidateEvaluationResult(
            candidate=updated_candidate,
            evaluation_run=run,
        )

    def approve_candidate(
        self,
        *,
        candidate: ConfigurationCandidate,
        evaluation_run: CandidateEvaluationRun,
        command: ApproveCandidateCommand,
        now: datetime,
        decision_id: UUID | None = None,
    ) -> CandidateReviewResult:
        self._require_aware(now)
        self._validate_candidate_command_scope(
            candidate=candidate,
            tenant_id=command.tenant_id,
            candidate_id=command.candidate_id,
            expected_revision=command.expected_candidate_revision,
        )
        self._require_passed_evaluation(
            candidate=candidate,
            evaluation_run=evaluation_run,
            evaluation_run_id=command.evaluation_run_id,
        )
        if evaluation_run.completed_at > now:
            raise ProductionBundleRuleViolation(
                "evaluation cannot be approved before it completes"
            )
        if candidate.status != "evaluation_passed":
            raise ProductionBundleRuleViolation(
                "only an evaluation-passed candidate can be approved"
            )
        if command.approved_by == candidate.proposed_by:
            raise ProductionBundleRuleViolation(
                "candidate proposer cannot approve their own candidate"
            )

        approved = candidate.model_copy(
            update={
                "status": "approved",
                "approved_evaluation_run_id": evaluation_run.id,
                "revision": candidate.revision + 1,
            }
        )
        decision = self._decision(
            decision_id=decision_id,
            tenant_id=command.tenant_id,
            action="approve",
            actor_id=command.approved_by,
            reason=command.reason,
            idempotency_key=command.idempotency_key,
            fingerprint=command_fingerprint(command),
            now=now,
            candidate_id=candidate.id,
            evaluation_run_id=evaluation_run.id,
        )
        return CandidateReviewResult(candidate=approved, decision=decision)

    def reject_candidate(
        self,
        *,
        candidate: ConfigurationCandidate,
        command: RejectCandidateCommand,
        now: datetime,
        decision_id: UUID | None = None,
    ) -> CandidateReviewResult:
        self._require_aware(now)
        self._validate_candidate_command_scope(
            candidate=candidate,
            tenant_id=command.tenant_id,
            candidate_id=command.candidate_id,
            expected_revision=command.expected_candidate_revision,
        )
        if candidate.status not in {
            "pending_evaluation",
            "evaluation_passed",
            "evaluation_failed",
        }:
            raise ProductionBundleRuleViolation(
                "candidate is not available for rejection"
            )
        rejected = candidate.model_copy(
            update={
                "status": "rejected",
                "revision": candidate.revision + 1,
            }
        )
        decision = self._decision(
            decision_id=decision_id,
            tenant_id=command.tenant_id,
            action="reject",
            actor_id=command.rejected_by,
            reason=command.reason,
            idempotency_key=command.idempotency_key,
            fingerprint=command_fingerprint(command),
            now=now,
            candidate_id=candidate.id,
        )
        return CandidateReviewResult(candidate=rejected, decision=decision)

    def activate_candidate(
        self,
        *,
        candidate: ConfigurationCandidate,
        evaluation_run: CandidateEvaluationRun,
        active_bundle: ProductionBundle,
        command: ActivateCandidateCommand,
        now: datetime,
        bundle_id: UUID | None = None,
        decision_id: UUID | None = None,
    ) -> CandidateActivationResult:
        self._require_aware(now)
        self._validate_candidate_command_scope(
            candidate=candidate,
            tenant_id=command.tenant_id,
            candidate_id=command.candidate_id,
            expected_revision=command.expected_candidate_revision,
        )
        self._require_passed_evaluation(
            candidate=candidate,
            evaluation_run=evaluation_run,
            evaluation_run_id=command.evaluation_run_id,
        )
        if evaluation_run.completed_at > now:
            raise ProductionBundleRuleViolation(
                "evaluation cannot be activated before it completes"
            )
        if candidate.status != "approved":
            raise ProductionBundleRuleViolation(
                "candidate must be approved before activation"
            )
        if candidate.approved_evaluation_run_id != evaluation_run.id:
            raise ProductionBundleRuleViolation(
                "activation must use the approved evaluation run"
            )
        if (
            active_bundle.tenant_id != command.tenant_id
            or active_bundle.id != candidate.base_bundle_id
            or active_bundle.status != "active"
        ):
            raise ProductionBundleRuleViolation(
                "candidate base bundle is no longer the active version"
            )

        deactivated = active_bundle.model_copy(
            update={
                "status": "inactive",
                "deactivated_at": now,
                "revision": active_bundle.revision + 1,
            }
        )
        activated = ProductionBundle(
            id=bundle_id or uuid4(),
            tenant_id=command.tenant_id,
            bundle_version=candidate.candidate_version,
            spec=candidate.proposed_spec,
            content_sha256=bundle_spec_sha256(candidate.proposed_spec),
            status="active",
            derived_from_bundle_id=active_bundle.id,
            source_candidate_id=candidate.id,
            created_by=candidate.proposed_by,
            created_at=now,
            activated_by=command.activated_by,
            activated_at=now,
        )
        activated_candidate = candidate.model_copy(
            update={
                "status": "activated",
                "revision": candidate.revision + 1,
            }
        )
        decision = self._decision(
            decision_id=decision_id,
            tenant_id=command.tenant_id,
            action="activate",
            actor_id=command.activated_by,
            reason=command.reason,
            idempotency_key=command.idempotency_key,
            fingerprint=command_fingerprint(command),
            now=now,
            candidate_id=candidate.id,
            evaluation_run_id=evaluation_run.id,
            from_bundle_id=active_bundle.id,
            to_bundle_id=activated.id,
        )
        return CandidateActivationResult(
            candidate=activated_candidate,
            previous_bundle=deactivated,
            activated_bundle=activated,
            decision=decision,
        )

    def rollback_bundle(
        self,
        *,
        active_bundle: ProductionBundle,
        target_bundle: ProductionBundle,
        command: RollbackProductionBundleCommand,
        now: datetime,
        decision_id: UUID | None = None,
    ) -> BundleRollbackResult:
        self._require_aware(now)
        if (
            active_bundle.tenant_id != command.tenant_id
            or target_bundle.tenant_id != command.tenant_id
            or target_bundle.id != command.target_bundle_id
        ):
            raise ProductionBundleRuleViolation(
                "production bundle is not available"
            )
        if active_bundle.status != "active" or target_bundle.status != "inactive":
            raise ProductionBundleRuleViolation(
                "rollback requires one active and one inactive bundle"
            )
        if active_bundle.id == target_bundle.id:
            raise ProductionBundleRuleViolation(
                "rollback target must differ from active bundle"
            )

        deactivated = active_bundle.model_copy(
            update={
                "status": "inactive",
                "deactivated_at": now,
                "revision": active_bundle.revision + 1,
            }
        )
        activated = target_bundle.model_copy(
            update={
                "status": "active",
                "activated_by": command.rolled_back_by,
                "activated_at": now,
                "deactivated_at": None,
                "revision": target_bundle.revision + 1,
            }
        )
        decision = self._decision(
            decision_id=decision_id,
            tenant_id=command.tenant_id,
            action="rollback",
            actor_id=command.rolled_back_by,
            reason=command.reason,
            idempotency_key=command.idempotency_key,
            fingerprint=command_fingerprint(command),
            now=now,
            from_bundle_id=active_bundle.id,
            to_bundle_id=target_bundle.id,
        )
        return BundleRollbackResult(
            previous_bundle=deactivated,
            activated_bundle=activated,
            decision=decision,
        )

    @staticmethod
    def _validate_structured_diff(
        *,
        base: ProductionBundleSpec,
        proposed: ProductionBundleSpec,
        changes: tuple,
    ) -> None:
        base_values = base.model_dump()
        proposed_values = proposed.model_dump()
        actual_changed_assets = {
            asset
            for asset, field_name in ASSET_SPEC_FIELDS.items()
            if base_values[field_name] != proposed_values[field_name]
        }
        declared_assets = {change.asset for change in changes}
        if actual_changed_assets != declared_assets:
            raise ProductionBundleRuleViolation(
                "structured diff does not match the proposed bundle snapshot"
            )
        for change in changes:
            field_name = ASSET_SPEC_FIELDS[change.asset]
            if (
                change.before_version != base_values[field_name]
                or change.after_version != proposed_values[field_name]
            ):
                raise ProductionBundleRuleViolation(
                    f"structured diff values do not match asset {change.asset}"
                )

    @staticmethod
    def _validate_candidate_command_scope(
        *,
        candidate: ConfigurationCandidate,
        tenant_id: str,
        candidate_id: UUID,
        expected_revision: int,
    ) -> None:
        if candidate.tenant_id != tenant_id or candidate.id != candidate_id:
            raise ProductionBundleRuleViolation(
                "configuration candidate is not available"
            )
        if candidate.revision != expected_revision:
            raise ProductionBundleRuleViolation(
                "configuration candidate revision conflict"
            )

    @staticmethod
    def _require_passed_evaluation(
        *,
        candidate: ConfigurationCandidate,
        evaluation_run: CandidateEvaluationRun,
        evaluation_run_id: UUID,
    ) -> None:
        if (
            evaluation_run.tenant_id != candidate.tenant_id
            or evaluation_run.id != evaluation_run_id
            or evaluation_run.candidate_id != candidate.id
            or evaluation_run.base_bundle_id != candidate.base_bundle_id
        ):
            raise ProductionBundleRuleViolation(
                "candidate evaluation run is not available"
            )
        if (
            evaluation_run.status != "completed"
            or not evaluation_run.gate_decision.passed
        ):
            raise ProductionBundleRuleViolation(
                "candidate evaluation gate has not passed"
            )

    @staticmethod
    def _decision(
        *,
        decision_id: UUID | None,
        tenant_id: str,
        action: str,
        actor_id: str,
        reason: str,
        idempotency_key: str,
        fingerprint: str,
        now: datetime,
        candidate_id: UUID | None = None,
        evaluation_run_id: UUID | None = None,
        from_bundle_id: UUID | None = None,
        to_bundle_id: UUID | None = None,
    ) -> PromotionDecision:
        return PromotionDecision(
            id=decision_id or uuid4(),
            tenant_id=tenant_id,
            action=action,
            candidate_id=candidate_id,
            evaluation_run_id=evaluation_run_id,
            from_bundle_id=from_bundle_id,
            to_bundle_id=to_bundle_id,
            actor_id=actor_id,
            reason=reason,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            created_at=now,
        )

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ProductionBundleRuleViolation("now must be timezone-aware")


class ProductionBundleApplicationService:
    """Transactional application layer for the immutable bundle ledger.

    The caller owns the ``AsyncSession`` transaction.  Every mutating method
    checks tenant scope, idempotency fingerprints and compare-and-swap state.
    """

    def __init__(
        self,
        domain: ProductionBundleDomainService | None = None,
    ) -> None:
        self._domain = domain or ProductionBundleDomainService()

    async def bootstrap_initial_bundle(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        bundle_version: str,
        spec: ProductionBundleSpec,
        actor_id: str,
        now: datetime,
    ) -> BundleWriteOutcome:
        """Create the first active bundle; later changes must use promotion."""

        ProductionBundleDomainService._require_aware(now)
        tenant_id = tenant_id.strip()
        bundle_version = bundle_version.strip()
        actor_id = actor_id.strip()
        if not tenant_id or not bundle_version or not actor_id:
            raise ProductionBundleRuleViolation(
                "tenant, bundle version and actor are required"
            )
        repository = PostgresProductionBundleRepository(session)
        try:
            active = await repository.get_active_bundle_for_update(
                tenant_id=tenant_id
            )
            if active is not None:
                if (
                    active.bundle_version == bundle_version
                    and active.spec == spec
                ):
                    return BundleWriteOutcome(active, False)
                raise ProductionBundleConflictError(
                    "an active production bundle already exists"
                )
            bundle = ProductionBundle(
                id=uuid4(),
                tenant_id=tenant_id,
                bundle_version=bundle_version,
                spec=spec,
                content_sha256=bundle_spec_sha256(spec),
                status="active",
                created_by=actor_id,
                created_at=now,
                activated_by=actor_id,
                activated_at=now,
            )
            await repository.insert_bundle(bundle=bundle)
            return BundleWriteOutcome(bundle, True)
        except self._known_errors():
            raise
        except IntegrityError as exc:
            raise ProductionBundleConflictError(
                "initial bundle conflicts with the production ledger"
            ) from exc
        except SQLAlchemyError as exc:
            raise ProductionBundlePersistenceError(
                "failed to persist initial production bundle"
            ) from exc

    async def propose_candidate(
        self,
        session: AsyncSession,
        *,
        command: ProposeConfigurationCandidateCommand,
        now: datetime,
    ) -> CandidateWriteOutcome:
        repository = PostgresProductionBundleRepository(session)
        fingerprint = command_fingerprint(command)
        try:
            replay = await repository.get_candidate_by_idempotency_key(
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
            )
            if replay is not None:
                candidate, stored_fingerprint = replay
                self._require_fingerprint(stored_fingerprint, fingerprint)
                return CandidateWriteOutcome(candidate, False)

            base = await repository.get_bundle_for_update(
                tenant_id=command.tenant_id,
                bundle_id=command.base_bundle_id,
            )
            if base is None:
                raise ProductionBundleNotFoundError(
                    "base production bundle is not available"
                )
            candidate = self._domain.propose_candidate(
                base_bundle=base,
                command=command,
                now=now,
            )
            inserted = await repository.insert_candidate(
                candidate=candidate,
                idempotency_key=command.idempotency_key,
                request_fingerprint=fingerprint,
            )
            if inserted:
                return CandidateWriteOutcome(candidate, True)
            replay = await repository.get_candidate_by_idempotency_key(
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
            )
            if replay is None:
                raise ProductionBundleConflictError(
                    "candidate version or idempotency key is already occupied"
                )
            existing, stored_fingerprint = replay
            self._require_fingerprint(stored_fingerprint, fingerprint)
            return CandidateWriteOutcome(existing, False)
        except self._known_errors():
            raise
        except IntegrityError as exc:
            raise ProductionBundleConflictError(
                "candidate conflicts with the production bundle ledger"
            ) from exc
        except SQLAlchemyError as exc:
            raise ProductionBundlePersistenceError(
                "failed to persist configuration candidate"
            ) from exc

    async def record_evaluation(
        self,
        session: AsyncSession,
        *,
        command: RecordCandidateEvaluationCommand,
        now: datetime,
    ) -> EvaluationWriteOutcome:
        repository = PostgresProductionBundleRepository(session)
        fingerprint = command_fingerprint(command)
        try:
            replay = await repository.get_evaluation_by_idempotency_key(
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
            )
            if replay is not None:
                run, stored_fingerprint = replay
                self._require_fingerprint(stored_fingerprint, fingerprint)
                candidate = await repository.get_candidate_for_update(
                    tenant_id=command.tenant_id,
                    candidate_id=run.candidate_id,
                )
                if candidate is None:
                    raise ProductionBundleNotFoundError(
                        "configuration candidate is not available"
                    )
                return EvaluationWriteOutcome(candidate, run, False)

            candidate = await repository.get_candidate_for_update(
                tenant_id=command.tenant_id,
                candidate_id=command.candidate_id,
            )
            if candidate is None:
                raise ProductionBundleNotFoundError(
                    "configuration candidate is not available"
                )
            active = await repository.get_active_bundle_for_update(
                tenant_id=command.tenant_id,
            )
            if active is None or active.id != candidate.base_bundle_id:
                raise ProductionBundleRuleViolation(
                    "candidate base is no longer the active production bundle"
                )
            if command.previous_experiment_candidate_id is not None:
                previous = (
                    await repository.get_latest_evaluated_candidate_for_update(
                        tenant_id=command.tenant_id,
                        base_bundle_id=candidate.base_bundle_id,
                        exclude_candidate_id=candidate.id,
                    )
                )
                if (
                    previous is None
                    or previous.id
                    != command.previous_experiment_candidate_id
                ):
                    raise ProductionBundleRuleViolation(
                        "previous experiment is not the latest eligible "
                        "server-selected candidate"
                    )
            expected_layers = {
                command.suite_metrics.golden.dataset_id: "golden",
                command.suite_metrics.fresh_bad_case.dataset_id: (
                    "fresh_bad_case"
                ),
                command.suite_metrics.high_risk_regression.dataset_id: (
                    "high_risk_regression"
                ),
            }
            layers = await repository.get_frozen_dataset_layers_for_update(
                tenant_id=command.tenant_id,
                dataset_ids=expected_layers,
            )
            if layers != expected_layers:
                raise ProductionBundleRuleViolation(
                    "evaluation requires three correctly layered frozen datasets"
                )
            result = self._domain.record_evaluation(
                candidate=candidate,
                command=command,
                now=now,
            )
            inserted = await repository.insert_evaluation(
                evaluation_run=result.evaluation_run,
                idempotency_key=command.idempotency_key,
                request_fingerprint=fingerprint,
            )
            if not inserted:
                replay = await repository.get_evaluation_by_idempotency_key(
                    tenant_id=command.tenant_id,
                    idempotency_key=command.idempotency_key,
                )
                if replay is None:
                    raise ProductionBundleConflictError(
                        "evaluation idempotency key is occupied"
                    )
                existing, stored_fingerprint = replay
                self._require_fingerprint(stored_fingerprint, fingerprint)
                return EvaluationWriteOutcome(candidate, existing, False)
            if not await repository.update_candidate(
                previous=candidate,
                updated=result.candidate,
            ):
                raise ProductionBundleConflictError(
                    "configuration candidate revision changed during evaluation"
                )
            return EvaluationWriteOutcome(
                result.candidate,
                result.evaluation_run,
                True,
            )
        except self._known_errors():
            raise
        except IntegrityError as exc:
            raise ProductionBundleConflictError(
                "evaluation conflicts with the production bundle ledger"
            ) from exc
        except SQLAlchemyError as exc:
            raise ProductionBundlePersistenceError(
                "failed to persist candidate evaluation"
            ) from exc

    async def approve_candidate(
        self,
        session: AsyncSession,
        *,
        command: ApproveCandidateCommand,
        now: datetime,
    ) -> ReviewWriteOutcome:
        return await self._review_candidate(
            session,
            command=command,
            now=now,
            action="approve",
        )

    async def reject_candidate(
        self,
        session: AsyncSession,
        *,
        command: RejectCandidateCommand,
        now: datetime,
    ) -> ReviewWriteOutcome:
        return await self._review_candidate(
            session,
            command=command,
            now=now,
            action="reject",
        )

    async def _review_candidate(
        self,
        session: AsyncSession,
        *,
        command: ApproveCandidateCommand | RejectCandidateCommand,
        now: datetime,
        action: str,
    ) -> ReviewWriteOutcome:
        repository = PostgresProductionBundleRepository(session)
        fingerprint = command_fingerprint(command)
        try:
            replay = await self._decision_replay(
                repository,
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
                fingerprint=fingerprint,
                action=action,
            )
            if replay is not None:
                candidate = await repository.get_candidate_for_update(
                    tenant_id=command.tenant_id,
                    candidate_id=command.candidate_id,
                )
                if candidate is None:
                    raise ProductionBundleNotFoundError(
                        "configuration candidate is not available"
                    )
                return ReviewWriteOutcome(candidate, replay, False)

            candidate = await repository.get_candidate_for_update(
                tenant_id=command.tenant_id,
                candidate_id=command.candidate_id,
            )
            if candidate is None:
                raise ProductionBundleNotFoundError(
                    "configuration candidate is not available"
                )
            # Recheck after acquiring the candidate lock to make concurrent
            # retries converge on the first committed decision.
            replay = await self._decision_replay(
                repository,
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
                fingerprint=fingerprint,
                action=action,
            )
            if replay is not None:
                return ReviewWriteOutcome(candidate, replay, False)

            if action == "approve":
                assert isinstance(command, ApproveCandidateCommand)
                evaluation = await repository.get_evaluation_for_update(
                    tenant_id=command.tenant_id,
                    evaluation_run_id=command.evaluation_run_id,
                )
                if evaluation is None:
                    raise ProductionBundleNotFoundError(
                        "candidate evaluation run is not available"
                    )
                result = self._domain.approve_candidate(
                    candidate=candidate,
                    evaluation_run=evaluation,
                    command=command,
                    now=now,
                )
            else:
                assert isinstance(command, RejectCandidateCommand)
                result = self._domain.reject_candidate(
                    candidate=candidate,
                    command=command,
                    now=now,
                )
            if not await repository.update_candidate(
                previous=candidate,
                updated=result.candidate,
            ):
                raise ProductionBundleConflictError(
                    "configuration candidate revision changed during review"
                )
            if not await repository.insert_decision(decision=result.decision):
                raise ProductionBundleConflictError(
                    "promotion decision idempotency key is occupied"
                )
            return ReviewWriteOutcome(
                result.candidate,
                result.decision,
                True,
            )
        except self._known_errors():
            raise
        except IntegrityError as exc:
            raise ProductionBundleConflictError(
                "review conflicts with the production bundle ledger"
            ) from exc
        except SQLAlchemyError as exc:
            raise ProductionBundlePersistenceError(
                "failed to persist candidate review"
            ) from exc

    async def activate_candidate(
        self,
        session: AsyncSession,
        *,
        command: ActivateCandidateCommand,
        now: datetime,
    ) -> ActivationWriteOutcome:
        repository = PostgresProductionBundleRepository(session)
        fingerprint = command_fingerprint(command)
        try:
            replay = await self._decision_replay(
                repository,
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
                fingerprint=fingerprint,
                action="activate",
            )
            if replay is not None:
                return await self._activation_replay(repository, replay)

            candidate = await repository.get_candidate_for_update(
                tenant_id=command.tenant_id,
                candidate_id=command.candidate_id,
            )
            if candidate is None:
                raise ProductionBundleNotFoundError(
                    "configuration candidate is not available"
                )
            evaluation = await repository.get_evaluation_for_update(
                tenant_id=command.tenant_id,
                evaluation_run_id=command.evaluation_run_id,
            )
            if evaluation is None:
                raise ProductionBundleNotFoundError(
                    "candidate evaluation run is not available"
                )
            active = await repository.get_active_bundle_for_update(
                tenant_id=command.tenant_id,
            )
            if active is None:
                raise ProductionBundleNotFoundError(
                    "active production bundle is not available"
                )
            replay = await self._decision_replay(
                repository,
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
                fingerprint=fingerprint,
                action="activate",
            )
            if replay is not None:
                return await self._activation_replay(repository, replay)

            result = self._domain.activate_candidate(
                candidate=candidate,
                evaluation_run=evaluation,
                active_bundle=active,
                command=command,
                now=now,
            )
            if not await repository.update_bundle_lifecycle(
                previous=active,
                updated=result.previous_bundle,
            ):
                raise ProductionBundleConflictError(
                    "active bundle changed during activation"
                )
            await repository.insert_bundle(bundle=result.activated_bundle)
            if not await repository.update_candidate(
                previous=candidate,
                updated=result.candidate,
            ):
                raise ProductionBundleConflictError(
                    "configuration candidate changed during activation"
                )
            if not await repository.insert_decision(decision=result.decision):
                raise ProductionBundleConflictError(
                    "activation idempotency key is occupied"
                )
            return ActivationWriteOutcome(
                result.candidate,
                result.previous_bundle,
                result.activated_bundle,
                result.decision,
                True,
            )
        except self._known_errors():
            raise
        except IntegrityError as exc:
            raise ProductionBundleConflictError(
                "activation conflicts with the production bundle ledger"
            ) from exc
        except SQLAlchemyError as exc:
            raise ProductionBundlePersistenceError(
                "failed to activate production bundle"
            ) from exc

    async def rollback_bundle(
        self,
        session: AsyncSession,
        *,
        command: RollbackProductionBundleCommand,
        now: datetime,
    ) -> RollbackWriteOutcome:
        repository = PostgresProductionBundleRepository(session)
        fingerprint = command_fingerprint(command)
        try:
            replay = await self._decision_replay(
                repository,
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
                fingerprint=fingerprint,
                action="rollback",
            )
            if replay is not None:
                return await self._rollback_replay(repository, replay)
            active = await repository.get_active_bundle_for_update(
                tenant_id=command.tenant_id,
            )
            target = await repository.get_bundle_for_update(
                tenant_id=command.tenant_id,
                bundle_id=command.target_bundle_id,
            )
            if active is None or target is None:
                raise ProductionBundleNotFoundError(
                    "production bundle is not available"
                )
            replay = await self._decision_replay(
                repository,
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
                fingerprint=fingerprint,
                action="rollback",
            )
            if replay is not None:
                return await self._rollback_replay(repository, replay)
            result = self._domain.rollback_bundle(
                active_bundle=active,
                target_bundle=target,
                command=command,
                now=now,
            )
            if not await repository.update_bundle_lifecycle(
                previous=active,
                updated=result.previous_bundle,
            ):
                raise ProductionBundleConflictError(
                    "active bundle changed during rollback"
                )
            if not await repository.update_bundle_lifecycle(
                previous=target,
                updated=result.activated_bundle,
            ):
                raise ProductionBundleConflictError(
                    "rollback target changed during activation"
                )
            if not await repository.insert_decision(decision=result.decision):
                raise ProductionBundleConflictError(
                    "rollback idempotency key is occupied"
                )
            return RollbackWriteOutcome(
                result.previous_bundle,
                result.activated_bundle,
                result.decision,
                True,
            )
        except self._known_errors():
            raise
        except IntegrityError as exc:
            raise ProductionBundleConflictError(
                "rollback conflicts with the production bundle ledger"
            ) from exc
        except SQLAlchemyError as exc:
            raise ProductionBundlePersistenceError(
                "failed to rollback production bundle"
            ) from exc

    @staticmethod
    async def _decision_replay(
        repository: PostgresProductionBundleRepository,
        *,
        tenant_id: str,
        idempotency_key: str,
        fingerprint: str,
        action: str,
    ) -> PromotionDecision | None:
        decision = await repository.get_decision_by_idempotency_key(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
        )
        if decision is None:
            return None
        ProductionBundleApplicationService._require_fingerprint(
            decision.request_fingerprint,
            fingerprint,
        )
        if decision.action != action:
            raise ProductionBundleConflictError(
                "idempotency key was reused for another promotion action"
            )
        return decision

    @staticmethod
    async def _activation_replay(
        repository: PostgresProductionBundleRepository,
        decision: PromotionDecision,
    ) -> ActivationWriteOutcome:
        if (
            decision.candidate_id is None
            or decision.from_bundle_id is None
            or decision.to_bundle_id is None
        ):
            raise ProductionBundleConflictError(
                "stored activation decision is incomplete"
            )
        candidate = await repository.get_candidate_for_update(
            tenant_id=decision.tenant_id,
            candidate_id=decision.candidate_id,
        )
        previous = await repository.get_bundle_for_update(
            tenant_id=decision.tenant_id,
            bundle_id=decision.from_bundle_id,
        )
        activated = await repository.get_bundle_for_update(
            tenant_id=decision.tenant_id,
            bundle_id=decision.to_bundle_id,
        )
        if candidate is None or previous is None or activated is None:
            raise ProductionBundlePersistenceError(
                "stored activation ledger is incomplete"
            )
        return ActivationWriteOutcome(
            candidate,
            previous,
            activated,
            decision,
            False,
        )

    @staticmethod
    async def _rollback_replay(
        repository: PostgresProductionBundleRepository,
        decision: PromotionDecision,
    ) -> RollbackWriteOutcome:
        if decision.from_bundle_id is None or decision.to_bundle_id is None:
            raise ProductionBundleConflictError(
                "stored rollback decision is incomplete"
            )
        previous = await repository.get_bundle_for_update(
            tenant_id=decision.tenant_id,
            bundle_id=decision.from_bundle_id,
        )
        activated = await repository.get_bundle_for_update(
            tenant_id=decision.tenant_id,
            bundle_id=decision.to_bundle_id,
        )
        if previous is None or activated is None:
            raise ProductionBundlePersistenceError(
                "stored rollback ledger is incomplete"
            )
        return RollbackWriteOutcome(
            previous,
            activated,
            decision,
            False,
        )

    @staticmethod
    def _require_fingerprint(stored: str, expected: str) -> None:
        if stored != expected:
            raise ProductionBundleConflictError(
                "idempotency key was reused with different business content"
            )

    @staticmethod
    def _known_errors() -> tuple[type[Exception], ...]:
        return (
            ProductionBundleRuleViolation,
            ProductionBundleNotFoundError,
            ProductionBundleConflictError,
            ProductionBundlePersistenceError,
        )
