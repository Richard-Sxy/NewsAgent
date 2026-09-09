"""候选配置评测、人工审批、激活和回滚的纯领域规则。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel

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


class ProductionBundleRuleViolation(ValueError):
    """候选、审批或生产版本不满足领域约束。"""


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
