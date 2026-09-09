"""Production Bundle 生命周期的事务编排与幂等应用服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.production_bundle import (
    PostgresProductionBundleRepository,
)
from app.schemas.production_bundle import (
    ActivateCandidateCommand,
    ApproveCandidateCommand,
    CandidateEvaluationRun,
    ConfigurationCandidate,
    ProductionBundle,
    PromotionDecision,
    ProposeConfigurationCandidateCommand,
    RecordCandidateEvaluationCommand,
    RejectCandidateCommand,
    RollbackProductionBundleCommand,
)
from app.services.production_bundle import (
    ProductionBundleDomainService,
    command_fingerprint,
)


class ProductionBundleTargetNotFoundError(LookupError):
    """资源不存在或不属于可信租户作用域。"""


class ProductionBundleConflictError(RuntimeError):
    """幂等键、状态、版本或并发写入冲突。"""


class ProductionBundleDatasetNotReadyError(ValueError):
    """三类评测数据集不完整、跨租户或尚未冻结。"""


class ProductionBundlePersistenceError(RuntimeError):
    """Bundle 领域事务持久化失败。"""

    retryable = True


@dataclass(frozen=True, slots=True)
class CandidateApplicationOutcome:
    candidate: ConfigurationCandidate
    created: bool


@dataclass(frozen=True, slots=True)
class EvaluationApplicationOutcome:
    evaluation_run: CandidateEvaluationRun
    candidate: ConfigurationCandidate
    created: bool


@dataclass(frozen=True, slots=True)
class PromotionApplicationOutcome:
    decision: PromotionDecision
    candidate: ConfigurationCandidate | None
    active_bundle: ProductionBundle | None
    created: bool


class ProductionBundleApplicationService:
    """在调用方提供的数据库事务中执行候选到上线/回滚链路。"""

    def __init__(
        self,
        domain_service: ProductionBundleDomainService | None = None,
    ) -> None:
        self._domain_service = domain_service or ProductionBundleDomainService()

    async def propose_candidate(
        self,
        session: AsyncSession,
        *,
        command: ProposeConfigurationCandidateCommand,
        now: datetime,
    ) -> CandidateApplicationOutcome:
        repository = PostgresProductionBundleRepository(session)
        fingerprint = command_fingerprint(command)
        try:
            existing = await repository.get_candidate_by_idempotency_key(
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
            )
            if existing is not None:
                return self._replay_candidate(existing, fingerprint)

            base = await repository.get_bundle_for_update(
                tenant_id=command.tenant_id,
                bundle_id=command.base_bundle_id,
            )
            if base is None:
                raise ProductionBundleTargetNotFoundError(
                    "production bundle not found"
                )
            # 与同一个 base 并发创建候选时，在行锁后重新读取幂等记录。
            existing = await repository.get_candidate_by_idempotency_key(
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
            )
            if existing is not None:
                return self._replay_candidate(existing, fingerprint)

            candidate = self._domain_service.propose_candidate(
                base_bundle=base,
                command=command,
                now=now,
            )
            inserted = await repository.insert_candidate(
                candidate=candidate,
                idempotency_key=command.idempotency_key,
                request_fingerprint=fingerprint,
            )
            if not inserted:
                concurrent = (
                    await repository.get_candidate_by_idempotency_key(
                        tenant_id=command.tenant_id,
                        idempotency_key=command.idempotency_key,
                    )
                )
                if concurrent is None:
                    raise ProductionBundlePersistenceError(
                        "candidate insert conflicted but cannot be replayed"
                    )
                return self._replay_candidate(concurrent, fingerprint)
            return CandidateApplicationOutcome(candidate=candidate, created=True)
        except self._business_errors():
            raise
        except IntegrityError as exc:
            raise ProductionBundleConflictError(
                "configuration candidate database conflict"
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
    ) -> EvaluationApplicationOutcome:
        repository = PostgresProductionBundleRepository(session)
        fingerprint = command_fingerprint(command)
        try:
            existing = await repository.get_evaluation_by_idempotency_key(
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
            )
            if existing is not None:
                return await self._replay_evaluation(
                    repository=repository,
                    existing=existing,
                    fingerprint=fingerprint,
                )

            candidate = await repository.get_candidate_for_update(
                tenant_id=command.tenant_id,
                candidate_id=command.candidate_id,
            )
            if candidate is None:
                raise ProductionBundleTargetNotFoundError(
                    "configuration candidate not found"
                )
            existing = await repository.get_evaluation_by_idempotency_key(
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
            )
            if existing is not None:
                return await self._replay_evaluation(
                    repository=repository,
                    existing=existing,
                    fingerprint=fingerprint,
                    candidate=candidate,
                )

            await self._validate_evaluation_datasets(
                repository=repository,
                command=command,
            )
            if command.previous_experiment_candidate_id is not None:
                if command.previous_experiment_candidate_id == candidate.id:
                    raise ProductionBundleConflictError(
                        "candidate cannot be its own previous experiment"
                    )
                previous = await repository.get_candidate_for_update(
                    tenant_id=command.tenant_id,
                    candidate_id=command.previous_experiment_candidate_id,
                )
                if previous is None:
                    raise ProductionBundleTargetNotFoundError(
                        "previous experiment candidate not found"
                    )

            result = self._domain_service.record_evaluation(
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
                concurrent = (
                    await repository.get_evaluation_by_idempotency_key(
                        tenant_id=command.tenant_id,
                        idempotency_key=command.idempotency_key,
                    )
                )
                if concurrent is None:
                    raise ProductionBundlePersistenceError(
                        "evaluation insert conflicted but cannot be replayed"
                    )
                return await self._replay_evaluation(
                    repository=repository,
                    existing=concurrent,
                    fingerprint=fingerprint,
                    candidate=candidate,
                )
            updated = await repository.update_candidate(
                previous=candidate,
                updated=result.candidate,
            )
            if not updated:
                raise ProductionBundleConflictError(
                    "configuration candidate status changed during evaluation"
                )
            return EvaluationApplicationOutcome(
                evaluation_run=result.evaluation_run,
                candidate=result.candidate,
                created=True,
            )
        except self._business_errors():
            raise
        except IntegrityError as exc:
            raise ProductionBundleConflictError(
                "candidate evaluation database conflict"
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
    ) -> PromotionApplicationOutcome:
        repository = PostgresProductionBundleRepository(session)
        return await self._review_candidate(
            repository=repository,
            command=command,
            now=now,
            approve=True,
        )

    async def reject_candidate(
        self,
        session: AsyncSession,
        *,
        command: RejectCandidateCommand,
        now: datetime,
    ) -> PromotionApplicationOutcome:
        repository = PostgresProductionBundleRepository(session)
        return await self._review_candidate(
            repository=repository,
            command=command,
            now=now,
            approve=False,
        )

    async def activate_candidate(
        self,
        session: AsyncSession,
        *,
        command: ActivateCandidateCommand,
        now: datetime,
    ) -> PromotionApplicationOutcome:
        repository = PostgresProductionBundleRepository(session)
        fingerprint = command_fingerprint(command)
        try:
            replay = await self._try_replay_decision(
                repository=repository,
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return replay
            candidate = await repository.get_candidate_for_update(
                tenant_id=command.tenant_id,
                candidate_id=command.candidate_id,
            )
            evaluation = await repository.get_evaluation_for_update(
                tenant_id=command.tenant_id,
                evaluation_run_id=command.evaluation_run_id,
            )
            active_bundle = await repository.get_active_bundle_for_update(
                tenant_id=command.tenant_id
            )
            if candidate is None or evaluation is None or active_bundle is None:
                raise ProductionBundleTargetNotFoundError(
                    "candidate, evaluation, or active bundle not found"
                )
            replay = await self._try_replay_decision(
                repository=repository,
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return replay

            result = self._domain_service.activate_candidate(
                candidate=candidate,
                evaluation_run=evaluation,
                active_bundle=active_bundle,
                command=command,
                now=now,
            )
            if not await repository.update_bundle_lifecycle(
                previous=active_bundle,
                updated=result.previous_bundle,
            ):
                raise ProductionBundleConflictError(
                    "active production bundle changed during activation"
                )
            await repository.insert_bundle(bundle=result.activated_bundle)
            if not await repository.update_candidate(
                previous=candidate,
                updated=result.candidate,
            ):
                raise ProductionBundleConflictError(
                    "candidate changed during activation"
                )
            if not await repository.insert_decision(decision=result.decision):
                raise ProductionBundleConflictError(
                    "activation idempotency key was used concurrently"
                )
            return PromotionApplicationOutcome(
                decision=result.decision,
                candidate=result.candidate,
                active_bundle=result.activated_bundle,
                created=True,
            )
        except self._business_errors():
            raise
        except IntegrityError as exc:
            raise ProductionBundleConflictError(
                "production bundle activation database conflict"
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
    ) -> PromotionApplicationOutcome:
        repository = PostgresProductionBundleRepository(session)
        fingerprint = command_fingerprint(command)
        try:
            replay = await self._try_replay_decision(
                repository=repository,
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return replay
            active_bundle = await repository.get_active_bundle_for_update(
                tenant_id=command.tenant_id
            )
            target_bundle = await repository.get_bundle_for_update(
                tenant_id=command.tenant_id,
                bundle_id=command.target_bundle_id,
            )
            if active_bundle is None or target_bundle is None:
                raise ProductionBundleTargetNotFoundError(
                    "active or target production bundle not found"
                )
            replay = await self._try_replay_decision(
                repository=repository,
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return replay
            result = self._domain_service.rollback_bundle(
                active_bundle=active_bundle,
                target_bundle=target_bundle,
                command=command,
                now=now,
            )
            if not await repository.update_bundle_lifecycle(
                previous=active_bundle,
                updated=result.previous_bundle,
            ):
                raise ProductionBundleConflictError(
                    "active production bundle changed during rollback"
                )
            if not await repository.update_bundle_lifecycle(
                previous=target_bundle,
                updated=result.activated_bundle,
            ):
                raise ProductionBundleConflictError(
                    "rollback target bundle changed concurrently"
                )
            if not await repository.insert_decision(decision=result.decision):
                raise ProductionBundleConflictError(
                    "rollback idempotency key was used concurrently"
                )
            return PromotionApplicationOutcome(
                decision=result.decision,
                candidate=None,
                active_bundle=result.activated_bundle,
                created=True,
            )
        except self._business_errors():
            raise
        except IntegrityError as exc:
            raise ProductionBundleConflictError(
                "production bundle rollback database conflict"
            ) from exc
        except SQLAlchemyError as exc:
            raise ProductionBundlePersistenceError(
                "failed to rollback production bundle"
            ) from exc

    async def _review_candidate(
        self,
        *,
        repository: PostgresProductionBundleRepository,
        command: ApproveCandidateCommand | RejectCandidateCommand,
        now: datetime,
        approve: bool,
    ) -> PromotionApplicationOutcome:
        fingerprint = command_fingerprint(command)
        try:
            replay = await self._try_replay_decision(
                repository=repository,
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return replay
            candidate = await repository.get_candidate_for_update(
                tenant_id=command.tenant_id,
                candidate_id=command.candidate_id,
            )
            if candidate is None:
                raise ProductionBundleTargetNotFoundError(
                    "configuration candidate not found"
                )
            replay = await self._try_replay_decision(
                repository=repository,
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return replay

            if approve:
                assert isinstance(command, ApproveCandidateCommand)
                evaluation = await repository.get_evaluation_for_update(
                    tenant_id=command.tenant_id,
                    evaluation_run_id=command.evaluation_run_id,
                )
                if evaluation is None:
                    raise ProductionBundleTargetNotFoundError(
                        "candidate evaluation run not found"
                    )
                result = self._domain_service.approve_candidate(
                    candidate=candidate,
                    evaluation_run=evaluation,
                    command=command,
                    now=now,
                )
            else:
                assert isinstance(command, RejectCandidateCommand)
                result = self._domain_service.reject_candidate(
                    candidate=candidate,
                    command=command,
                    now=now,
                )

            if not await repository.update_candidate(
                previous=candidate,
                updated=result.candidate,
            ):
                raise ProductionBundleConflictError(
                    "configuration candidate changed during review"
                )
            if not await repository.insert_decision(decision=result.decision):
                raise ProductionBundleConflictError(
                    "review idempotency key was used concurrently"
                )
            return PromotionApplicationOutcome(
                decision=result.decision,
                candidate=result.candidate,
                active_bundle=None,
                created=True,
            )
        except self._business_errors():
            raise
        except IntegrityError as exc:
            raise ProductionBundleConflictError(
                "configuration candidate review database conflict"
            ) from exc
        except SQLAlchemyError as exc:
            raise ProductionBundlePersistenceError(
                "failed to persist candidate review"
            ) from exc

    @staticmethod
    async def _validate_evaluation_datasets(
        *,
        repository: PostgresProductionBundleRepository,
        command: RecordCandidateEvaluationCommand,
    ) -> None:
        expected = {
            command.suite_metrics.golden.dataset_id: "golden",
            command.suite_metrics.fresh_bad_case.dataset_id: (
                "fresh_bad_case"
            ),
            command.suite_metrics.high_risk_regression.dataset_id: (
                "high_risk_regression"
            ),
        }
        actual = await repository.get_frozen_dataset_layers_for_update(
            tenant_id=command.tenant_id,
            dataset_ids=expected,
        )
        if actual != expected:
            raise ProductionBundleDatasetNotReadyError(
                "evaluation requires frozen golden, fresh bad-case, and "
                "high-risk regression datasets in the same tenant"
            )

    @staticmethod
    def _replay_candidate(
        existing: tuple[ConfigurationCandidate, str],
        fingerprint: str,
    ) -> CandidateApplicationOutcome:
        candidate, stored_fingerprint = existing
        if stored_fingerprint != fingerprint:
            raise ProductionBundleConflictError(
                "idempotency key was reused with different candidate content"
            )
        return CandidateApplicationOutcome(candidate=candidate, created=False)

    @staticmethod
    async def _replay_evaluation(
        *,
        repository: PostgresProductionBundleRepository,
        existing: tuple[CandidateEvaluationRun, str],
        fingerprint: str,
        candidate: ConfigurationCandidate | None = None,
    ) -> EvaluationApplicationOutcome:
        evaluation, stored_fingerprint = existing
        if stored_fingerprint != fingerprint:
            raise ProductionBundleConflictError(
                "idempotency key was reused with different evaluation content"
            )
        if candidate is None:
            candidate = await repository.get_candidate_for_update(
                tenant_id=evaluation.tenant_id,
                candidate_id=evaluation.candidate_id,
            )
        if candidate is None:
            raise ProductionBundlePersistenceError(
                "evaluation exists but its candidate cannot be read"
            )
        return EvaluationApplicationOutcome(
            evaluation_run=evaluation,
            candidate=candidate,
            created=False,
        )

    @staticmethod
    async def _try_replay_decision(
        *,
        repository: PostgresProductionBundleRepository,
        tenant_id: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> PromotionApplicationOutcome | None:
        decision = await repository.get_decision_by_idempotency_key(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
        )
        if decision is None:
            return None
        if decision.request_fingerprint != fingerprint:
            raise ProductionBundleConflictError(
                "idempotency key was reused with different promotion content"
            )
        candidate = None
        if decision.candidate_id is not None:
            candidate = await repository.get_candidate_for_update(
                tenant_id=tenant_id,
                candidate_id=decision.candidate_id,
            )
            if candidate is None:
                raise ProductionBundlePersistenceError(
                    "promotion decision exists but candidate cannot be read"
                )
        bundle = None
        if decision.to_bundle_id is not None:
            bundle = await repository.get_bundle_for_update(
                tenant_id=tenant_id,
                bundle_id=decision.to_bundle_id,
            )
            if bundle is None:
                raise ProductionBundlePersistenceError(
                    "promotion decision exists but target bundle cannot be read"
                )
        return PromotionApplicationOutcome(
            decision=decision,
            candidate=candidate,
            active_bundle=bundle,
            created=False,
        )

    @staticmethod
    def _business_errors() -> tuple[type[Exception], ...]:
        # Domain 规则继承 ValueError；显式列出应用错误，同时保留领域异常。
        from app.services.production_bundle import ProductionBundleRuleViolation

        return (
            ProductionBundleTargetNotFoundError,
            ProductionBundleConflictError,
            ProductionBundleDatasetNotReadyError,
            ProductionBundlePersistenceError,
            ProductionBundleRuleViolation,
        )
