"""Production Bundle 领域的 PostgreSQL Repository。"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evaluation_dataset import EvaluationDatasetRecord
from app.models.production_bundle import (
    CandidateEvaluationRunRecord,
    ConfigurationCandidateRecord,
    ProductionBundleRecord,
    PromotionDecisionRecord,
)
from app.schemas.production_bundle import (
    CandidateEvaluationRun,
    ConfigurationCandidate,
    ConfigurationDiff,
    EvaluationGateDecision,
    EvaluationGatePolicy,
    EvaluationSuiteMetrics,
    ProductionBundle,
    ProductionBundleSpec,
    PromotionDecision,
)


class ProductionBundleDataCorruptedError(RuntimeError):
    """数据库快照不能通过严格领域 Schema 校验。"""


class PostgresProductionBundleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active_bundle(
        self,
        *,
        tenant_id: str,
    ) -> ProductionBundle | None:
        """Read the active runtime snapshot without taking a lifecycle lock."""

        statement = select(ProductionBundleRecord).where(
            ProductionBundleRecord.tenant_id == tenant_id,
            ProductionBundleRecord.status == "active",
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_bundle_domain(record)

    async def get_active_bundle_for_update(
        self,
        *,
        tenant_id: str,
    ) -> ProductionBundle | None:
        statement = (
            select(ProductionBundleRecord)
            .where(
                ProductionBundleRecord.tenant_id == tenant_id,
                ProductionBundleRecord.status == "active",
            )
            .with_for_update()
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_bundle_domain(record)

    async def get_bundle_for_update(
        self,
        *,
        tenant_id: str,
        bundle_id: UUID,
    ) -> ProductionBundle | None:
        statement = (
            select(ProductionBundleRecord)
            .where(
                ProductionBundleRecord.tenant_id == tenant_id,
                ProductionBundleRecord.id == bundle_id,
            )
            .with_for_update()
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_bundle_domain(record)

    async def get_bundle(
        self,
        *,
        tenant_id: str,
        bundle_id: UUID,
    ) -> ProductionBundle | None:
        """Read one immutable Bundle snapshot without taking a row lock."""

        statement = select(ProductionBundleRecord).where(
            ProductionBundleRecord.tenant_id == tenant_id,
            ProductionBundleRecord.id == bundle_id,
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_bundle_domain(record)

    async def get_candidate_for_update(
        self,
        *,
        tenant_id: str,
        candidate_id: UUID,
    ) -> ConfigurationCandidate | None:
        statement = (
            select(ConfigurationCandidateRecord)
            .where(
                ConfigurationCandidateRecord.tenant_id == tenant_id,
                ConfigurationCandidateRecord.id == candidate_id,
            )
            .with_for_update()
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_candidate_domain(record)

    async def get_candidate(
        self,
        *,
        tenant_id: str,
        candidate_id: UUID,
    ) -> ConfigurationCandidate | None:
        """Read a candidate for the control plane without a lifecycle lock."""

        statement = select(ConfigurationCandidateRecord).where(
            ConfigurationCandidateRecord.tenant_id == tenant_id,
            ConfigurationCandidateRecord.id == candidate_id,
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_candidate_domain(record)

    async def get_latest_evaluated_candidate_for_update(
        self,
        *,
        tenant_id: str,
        base_bundle_id: UUID,
        exclude_candidate_id: UUID,
    ) -> ConfigurationCandidate | None:
        """Resolve the server-authoritative previous experiment baseline."""

        statement = (
            select(ConfigurationCandidateRecord)
            .where(
                ConfigurationCandidateRecord.tenant_id == tenant_id,
                ConfigurationCandidateRecord.base_bundle_id == base_bundle_id,
                ConfigurationCandidateRecord.id != exclude_candidate_id,
                ConfigurationCandidateRecord.status.in_(
                    ("evaluation_passed", "approved", "activated")
                ),
            )
            .order_by(
                ConfigurationCandidateRecord.created_at.desc(),
                ConfigurationCandidateRecord.id.desc(),
            )
            .limit(1)
            .with_for_update()
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_candidate_domain(record)

    async def get_candidate_by_idempotency_key(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
    ) -> tuple[ConfigurationCandidate, str] | None:
        statement = select(ConfigurationCandidateRecord).where(
            ConfigurationCandidateRecord.tenant_id == tenant_id,
            ConfigurationCandidateRecord.idempotency_key == idempotency_key,
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        if record is None:
            return None
        return self.to_candidate_domain(record), record.request_fingerprint

    async def get_evaluation_for_update(
        self,
        *,
        tenant_id: str,
        evaluation_run_id: UUID,
    ) -> CandidateEvaluationRun | None:
        statement = (
            select(CandidateEvaluationRunRecord)
            .where(
                CandidateEvaluationRunRecord.tenant_id == tenant_id,
                CandidateEvaluationRunRecord.id == evaluation_run_id,
            )
            .with_for_update()
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_evaluation_domain(record)

    async def get_evaluation(
        self,
        *,
        tenant_id: str,
        evaluation_run_id: UUID,
    ) -> CandidateEvaluationRun | None:
        statement = select(CandidateEvaluationRunRecord).where(
            CandidateEvaluationRunRecord.tenant_id == tenant_id,
            CandidateEvaluationRunRecord.id == evaluation_run_id,
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_evaluation_domain(record)

    async def get_evaluation_by_idempotency_key(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
    ) -> tuple[CandidateEvaluationRun, str] | None:
        statement = select(CandidateEvaluationRunRecord).where(
            CandidateEvaluationRunRecord.tenant_id == tenant_id,
            CandidateEvaluationRunRecord.idempotency_key == idempotency_key,
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        if record is None:
            return None
        return self.to_evaluation_domain(record), record.request_fingerprint

    async def get_decision_by_idempotency_key(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
    ) -> PromotionDecision | None:
        statement = select(PromotionDecisionRecord).where(
            PromotionDecisionRecord.tenant_id == tenant_id,
            PromotionDecisionRecord.idempotency_key == idempotency_key,
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_decision_domain(record)

    async def get_frozen_dataset_layers_for_update(
        self,
        *,
        tenant_id: str,
        dataset_ids: Iterable[UUID],
    ) -> dict[UUID, str]:
        ids = tuple(dataset_ids)
        statement = (
            select(
                EvaluationDatasetRecord.id,
                EvaluationDatasetRecord.dataset_layer,
                EvaluationDatasetRecord.status,
            )
            .where(
                EvaluationDatasetRecord.tenant_id == tenant_id,
                EvaluationDatasetRecord.id.in_(ids),
            )
            .with_for_update()
        )
        result = await self._session.execute(statement)
        rows = result.all()
        return {
            row.id: row.dataset_layer
            for row in rows
            if row.status == "frozen"
        }

    async def insert_candidate(
        self,
        *,
        candidate: ConfigurationCandidate,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> bool:
        statement = (
            insert(ConfigurationCandidateRecord)
            .values(
                **self.candidate_values(
                    candidate,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                )
            )
            .on_conflict_do_nothing(
                constraint="uq_configuration_candidates_tenant_idempotency"
            )
            .returning(ConfigurationCandidateRecord.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def insert_evaluation(
        self,
        *,
        evaluation_run: CandidateEvaluationRun,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> bool:
        statement = (
            insert(CandidateEvaluationRunRecord)
            .values(
                **self.evaluation_values(
                    evaluation_run,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                )
            )
            .on_conflict_do_nothing(
                constraint="uq_candidate_evaluation_runs_tenant_idempotency"
            )
            .returning(CandidateEvaluationRunRecord.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def insert_bundle(self, *, bundle: ProductionBundle) -> None:
        self._session.add(ProductionBundleRecord(**self.bundle_values(bundle)))
        await self._session.flush()

    async def insert_decision(self, *, decision: PromotionDecision) -> bool:
        statement = (
            insert(PromotionDecisionRecord)
            .values(**self.decision_values(decision))
            .on_conflict_do_nothing(
                constraint="uq_promotion_decisions_tenant_idempotency"
            )
            .returning(PromotionDecisionRecord.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def update_candidate(
        self,
        *,
        previous: ConfigurationCandidate,
        updated: ConfigurationCandidate,
    ) -> bool:
        statement = (
            update(ConfigurationCandidateRecord)
            .where(
                ConfigurationCandidateRecord.tenant_id == previous.tenant_id,
                ConfigurationCandidateRecord.id == previous.id,
                ConfigurationCandidateRecord.status == previous.status,
                ConfigurationCandidateRecord.revision == previous.revision,
            )
            .values(
                status=updated.status,
                revision=updated.revision,
                approved_evaluation_run_id=(
                    updated.approved_evaluation_run_id
                ),
            )
            .returning(ConfigurationCandidateRecord.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def update_bundle_lifecycle(
        self,
        *,
        previous: ProductionBundle,
        updated: ProductionBundle,
    ) -> bool:
        statement = (
            update(ProductionBundleRecord)
            .where(
                ProductionBundleRecord.tenant_id == previous.tenant_id,
                ProductionBundleRecord.id == previous.id,
                ProductionBundleRecord.status == previous.status,
                ProductionBundleRecord.revision == previous.revision,
            )
            .values(
                status=updated.status,
                activated_by=updated.activated_by,
                activated_at=updated.activated_at,
                deactivated_at=updated.deactivated_at,
                revision=updated.revision,
            )
            .returning(ProductionBundleRecord.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    @staticmethod
    def to_bundle_domain(record: ProductionBundleRecord) -> ProductionBundle:
        try:
            return ProductionBundle(
                id=record.id,
                tenant_id=record.tenant_id,
                bundle_version=record.bundle_version,
                spec=ProductionBundleSpec(
                    metric_definition_version=(
                        record.metric_definition_version
                    ),
                    hot_score_policy_version=record.hot_score_policy_version,
                    reranker_policy_version=record.reranker_policy_version,
                    analysis_prompt_version=record.analysis_prompt_version,
                    fastgpt_app_id=record.fastgpt_app_id,
                    model_version=record.model_version,
                    output_schema_version=record.output_schema_version,
                    validator_version=record.validator_version,
                    memory_resolver_policy_version=(
                        record.memory_resolver_policy_version
                    ),
                ),
                content_sha256=record.content_sha256,
                status=record.status,
                derived_from_bundle_id=record.derived_from_bundle_id,
                source_candidate_id=record.source_candidate_id,
                created_by=record.created_by,
                created_at=record.created_at,
                activated_by=record.activated_by,
                activated_at=record.activated_at,
                deactivated_at=record.deactivated_at,
                revision=record.revision,
            )
        except ValidationError as exc:
            raise ProductionBundleDataCorruptedError(
                "production bundle record failed schema validation"
            ) from exc

    @staticmethod
    def to_candidate_domain(
        record: ConfigurationCandidateRecord,
    ) -> ConfigurationCandidate:
        try:
            return ConfigurationCandidate(
                id=record.id,
                tenant_id=record.tenant_id,
                base_bundle_id=record.base_bundle_id,
                candidate_version=record.candidate_version,
                proposed_spec=ProductionBundleSpec.model_validate(
                    record.proposed_spec
                ),
                structured_diff=tuple(
                    ConfigurationDiff.model_validate(value)
                    for value in record.structured_diff
                ),
                content_sha256=record.content_sha256,
                status=record.status,
                proposed_by=record.proposed_by,
                proposal_reason=record.proposal_reason,
                created_at=record.created_at,
                revision=record.revision,
                approved_evaluation_run_id=(
                    record.approved_evaluation_run_id
                ),
            )
        except ValidationError as exc:
            raise ProductionBundleDataCorruptedError(
                "configuration candidate record failed schema validation"
            ) from exc

    @staticmethod
    def to_evaluation_domain(
        record: CandidateEvaluationRunRecord,
    ) -> CandidateEvaluationRun:
        try:
            return CandidateEvaluationRun(
                id=record.id,
                tenant_id=record.tenant_id,
                candidate_id=record.candidate_id,
                base_bundle_id=record.base_bundle_id,
                previous_experiment_candidate_id=(
                    record.previous_experiment_candidate_id
                ),
                suite_metrics=EvaluationSuiteMetrics.model_validate(
                    record.suite_metrics
                ),
                gate_policy=EvaluationGatePolicy.model_validate(
                    record.gate_policy
                ),
                gate_decision=EvaluationGateDecision.model_validate(
                    record.gate_decision
                ),
                evaluator_version=record.evaluator_version,
                status=record.status,
                artifact_uri=record.artifact_uri,
                artifact_sha256=record.artifact_sha256,
                error_type=record.error_type,
                error_message=record.error_message,
                started_at=record.started_at,
                completed_at=record.completed_at,
            )
        except ValidationError as exc:
            raise ProductionBundleDataCorruptedError(
                "candidate evaluation record failed schema validation"
            ) from exc

    @staticmethod
    def to_decision_domain(
        record: PromotionDecisionRecord,
    ) -> PromotionDecision:
        try:
            return PromotionDecision(
                id=record.id,
                tenant_id=record.tenant_id,
                action=record.action,
                candidate_id=record.candidate_id,
                evaluation_run_id=record.evaluation_run_id,
                from_bundle_id=record.from_bundle_id,
                to_bundle_id=record.to_bundle_id,
                actor_id=record.actor_id,
                reason=record.reason,
                idempotency_key=record.idempotency_key,
                request_fingerprint=record.request_fingerprint,
                created_at=record.created_at,
            )
        except ValidationError as exc:
            raise ProductionBundleDataCorruptedError(
                "promotion decision record failed schema validation"
            ) from exc

    @staticmethod
    def bundle_values(bundle: ProductionBundle) -> dict:
        return {
            "id": bundle.id,
            "tenant_id": bundle.tenant_id,
            "bundle_version": bundle.bundle_version,
            **bundle.spec.model_dump(mode="python"),
            "content_sha256": bundle.content_sha256,
            "status": bundle.status,
            "derived_from_bundle_id": bundle.derived_from_bundle_id,
            "source_candidate_id": bundle.source_candidate_id,
            "created_by": bundle.created_by,
            "created_at": bundle.created_at,
            "updated_at": bundle.created_at,
            "activated_by": bundle.activated_by,
            "activated_at": bundle.activated_at,
            "deactivated_at": bundle.deactivated_at,
            "revision": bundle.revision,
        }

    @staticmethod
    def candidate_values(
        candidate: ConfigurationCandidate,
        *,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> dict:
        return {
            "id": candidate.id,
            "tenant_id": candidate.tenant_id,
            "base_bundle_id": candidate.base_bundle_id,
            "candidate_version": candidate.candidate_version,
            "proposed_spec": candidate.proposed_spec.model_dump(mode="json"),
            "structured_diff": [
                change.model_dump(mode="json")
                for change in candidate.structured_diff
            ],
            "content_sha256": candidate.content_sha256,
            "status": candidate.status,
            "proposed_by": candidate.proposed_by,
            "proposal_reason": candidate.proposal_reason,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
            "approved_evaluation_run_id": (
                candidate.approved_evaluation_run_id
            ),
            "revision": candidate.revision,
            "created_at": candidate.created_at,
            "updated_at": candidate.created_at,
        }

    @staticmethod
    def evaluation_values(
        run: CandidateEvaluationRun,
        *,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> dict:
        return {
            "id": run.id,
            "tenant_id": run.tenant_id,
            "candidate_id": run.candidate_id,
            "base_bundle_id": run.base_bundle_id,
            "previous_experiment_candidate_id": (
                run.previous_experiment_candidate_id
            ),
            "golden_dataset_id": run.suite_metrics.golden.dataset_id,
            "fresh_bad_case_dataset_id": (
                run.suite_metrics.fresh_bad_case.dataset_id
            ),
            "high_risk_dataset_id": (
                run.suite_metrics.high_risk_regression.dataset_id
            ),
            "suite_metrics": run.suite_metrics.model_dump(mode="json"),
            "gate_policy": run.gate_policy.model_dump(mode="json"),
            "gate_decision": run.gate_decision.model_dump(mode="json"),
            "evaluator_version": run.evaluator_version,
            "status": run.status,
            "artifact_uri": run.artifact_uri,
            "artifact_sha256": run.artifact_sha256,
            "error_type": run.error_type,
            "error_message": run.error_message,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
            "created_at": run.completed_at,
            "updated_at": run.completed_at,
        }

    @staticmethod
    def decision_values(decision: PromotionDecision) -> dict:
        return {
            "id": decision.id,
            "tenant_id": decision.tenant_id,
            "action": decision.action,
            "candidate_id": decision.candidate_id,
            "evaluation_run_id": decision.evaluation_run_id,
            "from_bundle_id": decision.from_bundle_id,
            "to_bundle_id": decision.to_bundle_id,
            "actor_id": decision.actor_id,
            "reason": decision.reason,
            "idempotency_key": decision.idempotency_key,
            "request_fingerprint": decision.request_fingerprint,
            "created_at": decision.created_at,
            "updated_at": decision.created_at,
        }
