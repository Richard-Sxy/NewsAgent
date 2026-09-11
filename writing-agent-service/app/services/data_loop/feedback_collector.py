"""Feedback Case 收集、标签版本化与人工批准服务。"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.hot_news_decision import HotNewsDecision
from app.repositories.analysis_feedback import (
    PostgresAnalysisFeedbackRepository,
)
from app.schemas.analysis_feedback import (
    AnalysisFeedbackCase,
    AnalysisFeedbackLabel,
    ApproveAnalysisFeedbackLabelCommand,
    CollectAnalysisFeedbackCommand,
    FeedbackProblemType,
    FeedbackSeverity,
    FeedbackSource,
    FeedbackSourceReference,
    FeedbackStatus,
    PublicationOutcome,
    RecordPublicationOutcomeCommand,
    RequestChangesAnalysisFeedbackLabelCommand,
    SubmitAnalysisFeedbackLabelCommand,
)
from app.schemas.hot_news_memory import HotNewsAnalysisMemory


class AnalysisFeedbackTargetNotFoundError(LookupError):
    """Case、Label 或运营决策不存在于当前租户作用域。"""


class AnalysisFeedbackConflictError(RuntimeError):
    """幂等键、标签版本或状态发生冲突。"""


class AnalysisFeedbackPersistenceError(RuntimeError):
    """Feedback 持久化发生可重试错误。"""

    retryable = True


@dataclass(frozen=True, slots=True)
class FeedbackCaseWriteOutcome:
    case: AnalysisFeedbackCase
    created: bool


@dataclass(frozen=True, slots=True)
class FeedbackLabelWriteOutcome:
    label: AnalysisFeedbackLabel
    created: bool


@dataclass(frozen=True, slots=True)
class PublicationOutcomeWriteOutcome:
    outcome: PublicationOutcome
    created: bool


def build_feedback_content_sha256(
    command: CollectAnalysisFeedbackCommand,
    *,
    tenant_id: str,
) -> str:
    """对不可变业务内容做稳定哈希，用于幂等冲突检测。"""

    payload = command.model_dump(
        mode="json",
        exclude={"idempotency_key"},
    )
    payload["tenant_id"] = tenant_id
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_publication_outcome_sha256(
    command: RecordPublicationOutcomeCommand,
    *,
    tenant_id: str,
) -> str:
    """对聚合发布效果的不可变内容计算稳定哈希。"""

    payload = command.model_dump(
        mode="json",
        exclude={"idempotency_key"},
    )
    payload["tenant_id"] = tenant_id
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AnalysisFeedbackCollector:
    """Data Loop 反馈写入的唯一应用层入口。"""

    async def collect(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        command: CollectAnalysisFeedbackCommand,
        recorded_at: datetime,
        feedback_case_id: UUID | None = None,
    ) -> FeedbackCaseWriteOutcome:
        tenant_id = self._validate_identity(tenant_id, "tenant_id")
        self._validate_aware_datetime(recorded_at, "recorded_at")
        content_sha256 = build_feedback_content_sha256(
            command,
            tenant_id=tenant_id,
        )
        repository = PostgresAnalysisFeedbackRepository(session)

        try:
            if command.run_id is not None and not (
                await repository.analysis_run_contains_news(
                    tenant_id=tenant_id,
                    run_id=command.run_id,
                    run_idempotency_key=command.run_idempotency_key,
                    news_id=command.news_id,
                    production_bundle_version=(
                        command.production_bundle_version
                    ),
                )
            ):
                raise AnalysisFeedbackTargetNotFoundError(
                    "热点分析运行或新闻不存在"
                )
            existing = await repository.get_case_by_idempotency_key(
                tenant_id=tenant_id,
                idempotency_key=command.idempotency_key,
            )
            if existing is not None:
                self._ensure_same_case(existing, content_sha256)
                return FeedbackCaseWriteOutcome(existing, False)

            feedback_case = AnalysisFeedbackCase(
                id=feedback_case_id or uuid4(),
                tenant_id=tenant_id,
                status="needs_label",
                recorded_at=recorded_at,
                content_sha256=content_sha256,
                **command.model_dump(),
            )
            inserted = await repository.insert_case(case=feedback_case)
            if inserted:
                return FeedbackCaseWriteOutcome(feedback_case, True)

            existing = await repository.get_case_by_idempotency_key(
                tenant_id=tenant_id,
                idempotency_key=command.idempotency_key,
            )
            if existing is None:
                raise AnalysisFeedbackPersistenceError(
                    "Feedback Case 写入冲突，但无法读取已有记录"
                )
            self._ensure_same_case(existing, content_sha256)
            return FeedbackCaseWriteOutcome(existing, False)
        except (
            AnalysisFeedbackTargetNotFoundError,
            AnalysisFeedbackConflictError,
            AnalysisFeedbackPersistenceError,
        ):
            raise
        except IntegrityError as exc:
            raise AnalysisFeedbackConflictError(
                "Feedback Case 写入发生数据库约束冲突"
            ) from exc
        except SQLAlchemyError as exc:
            raise AnalysisFeedbackPersistenceError(
                "Feedback Case 持久化失败"
            ) from exc

    async def collect_from_analysis_memory(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        memory: HotNewsAnalysisMemory,
        run_idempotency_key: str,
        source_type: FeedbackSource,
        problem_type: FeedbackProblemType,
        severity: FeedbackSeverity,
        source_reference: FeedbackSourceReference,
        occurred_at: datetime,
        idempotency_key: str,
        recorded_at: datetime,
        feedback_case_id: UUID | None = None,
    ) -> FeedbackCaseWriteOutcome:
        """从已校验的热点运行快照收集低置信/检索等反馈。"""

        normalized_tenant_id = self._validate_identity(
            tenant_id, "tenant_id"
        )
        if memory.tenant_id != normalized_tenant_id:
            raise AnalysisFeedbackTargetNotFoundError(
                "热点分析记忆不存在"
            )
        command = CollectAnalysisFeedbackCommand(
            run_id=memory.run_id,
            run_idempotency_key=run_idempotency_key,
            news_id=memory.news_id,
            source_type=source_type,
            problem_type=problem_type,
            severity=severity,
            production_bundle_version=memory.production_bundle_version,
            analysis_input_snapshot=memory.analysis_input,
            analysis_output_snapshot=memory.analysis_report,
            source_reference=source_reference,
            occurred_at=occurred_at,
            idempotency_key=idempotency_key,
        )
        return await self.collect(
            session,
            tenant_id=normalized_tenant_id,
            command=command,
            recorded_at=recorded_at,
            feedback_case_id=feedback_case_id,
        )

    async def collect_from_operator_decision(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        memory: HotNewsAnalysisMemory,
        run_idempotency_key: str,
        decision: HotNewsDecision,
        problem_type: FeedbackProblemType,
        severity: FeedbackSeverity,
        occurred_at: datetime,
        recorded_at: datetime,
        idempotency_key: str | None = None,
        feedback_case_id: UUID | None = None,
    ) -> FeedbackCaseWriteOutcome:
        """
        从拒绝/纠正决策构造 Case。

        ``correction_payload`` 故意不会进入 Case 或标签；纠正内容
        必须另行通过 ``SubmitAnalysisFeedbackLabelCommand`` 验证。
        """

        normalized_tenant_id = self._validate_identity(
            tenant_id, "tenant_id"
        )
        if (
            memory.tenant_id != normalized_tenant_id
            or decision.tenant_id != normalized_tenant_id
            or decision.run_id != memory.run_id
            or decision.news_id != memory.news_id
        ):
            raise AnalysisFeedbackTargetNotFoundError(
                "运营决策或热点分析记忆不存在"
            )
        if decision.decision_type not in {"rejected", "corrected"}:
            raise ValueError(
                "only rejected or corrected decisions create feedback cases"
            )
        if decision.id is None:
            raise ValueError("persisted operator decision requires id")

        source_type: FeedbackSource = (
            "operator_rejected"
            if decision.decision_type == "rejected"
            else "operator_corrected"
        )
        return await self.collect_from_analysis_memory(
            session,
            tenant_id=normalized_tenant_id,
            memory=memory,
            run_idempotency_key=run_idempotency_key,
            source_type=source_type,
            problem_type=problem_type,
            severity=severity,
            source_reference=FeedbackSourceReference(
                reference_type="operator_decision",
                reference_id=str(decision.id),
                request_id=memory.fastgpt_request_id,
                diagnostic_code=decision.decision_type,
                diagnostic_summary=decision.reason.strip()[:1000],
            ),
            occurred_at=occurred_at,
            idempotency_key=(
                idempotency_key
                or f"feedback:operator-decision:{decision.id}"
            ),
            recorded_at=recorded_at,
            feedback_case_id=feedback_case_id,
        )

    async def record_publication_outcome(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        command: RecordPublicationOutcomeCommand,
        recorded_at: datetime,
        outcome_id: UUID | None = None,
    ) -> PublicationOutcomeWriteOutcome:
        """幂等记录窗口级聚合发布效果。"""

        tenant_id = self._validate_identity(tenant_id, "tenant_id")
        self._validate_aware_datetime(recorded_at, "recorded_at")
        content_sha256 = build_publication_outcome_sha256(
            command,
            tenant_id=tenant_id,
        )
        repository = PostgresAnalysisFeedbackRepository(session)

        try:
            if not await repository.analysis_run_contains_news(
                tenant_id=tenant_id,
                run_id=command.run_id,
                run_idempotency_key=command.run_idempotency_key,
                news_id=command.news_id,
            ):
                raise AnalysisFeedbackTargetNotFoundError(
                    "热点分析运行或新闻不存在"
                )
            existing = (
                await repository.get_publication_outcome_by_idempotency_key(
                    tenant_id=tenant_id,
                    idempotency_key=command.idempotency_key,
                )
            )
            if existing is not None:
                self._ensure_same_publication_outcome(
                    existing, content_sha256
                )
                return PublicationOutcomeWriteOutcome(existing, False)

            outcome = PublicationOutcome(
                id=outcome_id or uuid4(),
                tenant_id=tenant_id,
                recorded_at=recorded_at,
                content_sha256=content_sha256,
                **command.model_dump(),
            )
            inserted = await repository.insert_publication_outcome(
                outcome=outcome
            )
            if inserted:
                return PublicationOutcomeWriteOutcome(outcome, True)

            existing = (
                await repository.get_publication_outcome_by_idempotency_key(
                    tenant_id=tenant_id,
                    idempotency_key=command.idempotency_key,
                )
            )
            if existing is None:
                raise AnalysisFeedbackPersistenceError(
                    "Publication Outcome 写入冲突，但无法读取记录"
                )
            self._ensure_same_publication_outcome(
                existing, content_sha256
            )
            return PublicationOutcomeWriteOutcome(existing, False)
        except (
            AnalysisFeedbackTargetNotFoundError,
            AnalysisFeedbackConflictError,
            AnalysisFeedbackPersistenceError,
        ):
            raise
        except IntegrityError as exc:
            raise AnalysisFeedbackConflictError(
                "Publication Outcome 写入发生数据库约束冲突"
            ) from exc
        except SQLAlchemyError as exc:
            raise AnalysisFeedbackPersistenceError(
                "Publication Outcome 持久化失败"
            ) from exc

    async def collect_from_publication_outcome(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        memory: HotNewsAnalysisMemory,
        outcome: PublicationOutcome,
        severity: FeedbackSeverity,
        recorded_at: datetime,
        idempotency_key: str | None = None,
        feedback_case_id: UUID | None = None,
    ) -> FeedbackCaseWriteOutcome:
        """将确定性规则识别的发布后异常转为 Feedback Case。"""

        normalized_tenant_id = self._validate_identity(
            tenant_id, "tenant_id"
        )
        if (
            memory.tenant_id != normalized_tenant_id
            or outcome.tenant_id != normalized_tenant_id
            or outcome.run_id != memory.run_id
            or outcome.news_id != memory.news_id
        ):
            raise AnalysisFeedbackTargetNotFoundError(
                "发布效果或热点分析记忆不存在"
            )
        return await self.collect_from_analysis_memory(
            session,
            tenant_id=normalized_tenant_id,
            memory=memory,
            run_idempotency_key=outcome.run_idempotency_key,
            source_type="post_publish_outcome",
            problem_type="outcome_underperformance",
            severity=severity,
            source_reference=FeedbackSourceReference(
                reference_type="publication_outcome",
                reference_id=str(outcome.id),
            ),
            occurred_at=outcome.window_end,
            idempotency_key=(
                idempotency_key
                or f"feedback:publication-outcome:{outcome.id}"
            ),
            recorded_at=recorded_at,
            feedback_case_id=feedback_case_id,
        )

    async def list_cases(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        statuses: Sequence[FeedbackStatus] | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[AnalysisFeedbackCase]:
        tenant_id = self._validate_identity(tenant_id, "tenant_id")
        self._validate_pagination(offset=offset, limit=limit)
        repository = PostgresAnalysisFeedbackRepository(session)
        try:
            return await repository.list_cases(
                tenant_id=tenant_id,
                statuses=statuses,
                offset=offset,
                limit=limit,
            )
        except SQLAlchemyError as exc:
            raise AnalysisFeedbackPersistenceError(
                "查询 Feedback Case 失败"
            ) from exc

    async def list_labels(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        feedback_case_id: UUID,
    ) -> list[AnalysisFeedbackLabel]:
        tenant_id = self._validate_identity(tenant_id, "tenant_id")
        repository = PostgresAnalysisFeedbackRepository(session)
        try:
            feedback_case = await repository.get_case(
                tenant_id=tenant_id,
                feedback_case_id=feedback_case_id,
            )
            if feedback_case is None:
                raise AnalysisFeedbackTargetNotFoundError(
                    "Feedback Case 不存在"
                )
            return await repository.list_labels(
                tenant_id=tenant_id,
                feedback_case_id=feedback_case_id,
            )
        except AnalysisFeedbackTargetNotFoundError:
            raise
        except SQLAlchemyError as exc:
            raise AnalysisFeedbackPersistenceError(
                "查询 Feedback Label 失败"
            ) from exc

    async def submit_label(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        command: SubmitAnalysisFeedbackLabelCommand,
        recorded_at: datetime,
        label_id: UUID | None = None,
    ) -> FeedbackLabelWriteOutcome:
        tenant_id = self._validate_identity(tenant_id, "tenant_id")
        self._validate_aware_datetime(recorded_at, "recorded_at")
        repository = PostgresAnalysisFeedbackRepository(session)

        try:
            existing = await repository.get_label_by_idempotency_key(
                tenant_id=tenant_id,
                idempotency_key=command.idempotency_key,
            )
            if existing is not None:
                self._ensure_same_submitted_label(existing, command)
                return FeedbackLabelWriteOutcome(existing, False)

            feedback_case = await repository.get_case_for_update(
                tenant_id=tenant_id,
                feedback_case_id=command.feedback_case_id,
            )
            if feedback_case is None:
                raise AnalysisFeedbackTargetNotFoundError(
                    "Feedback Case 不存在"
                )
            # Linearize concurrent retries on the case row, then recheck the
            # idempotency ledger. Server-generated timestamps are not business
            # input and must not make a retry conflict.
            existing = await repository.get_label_by_idempotency_key(
                tenant_id=tenant_id,
                idempotency_key=command.idempotency_key,
            )
            if existing is not None:
                self._ensure_same_submitted_label(existing, command)
                return FeedbackLabelWriteOutcome(existing, False)
            if feedback_case.status in {"excluded", "frozen"}:
                raise AnalysisFeedbackConflictError(
                    "当前 Feedback Case 状态不允许修改标签"
                )
            available_evidence_ids = {
                item.news_id
                for item in (
                    feedback_case.analysis_input_snapshot.related_news
                )
            }
            missing_required_evidence = set(
                command.required_evidence_news_ids
            ) - available_evidence_ids
            if missing_required_evidence:
                raise ValueError(
                    "required evidence is absent from analysis input: "
                    f"{sorted(missing_required_evidence)}"
                )

            previous = await repository.get_latest_label_for_update(
                tenant_id=tenant_id,
                feedback_case_id=command.feedback_case_id,
            )
            self._validate_previous_label_version(previous, command)
            next_version = 1 if previous is None else previous.label_version + 1
            label = AnalysisFeedbackLabel(
                id=label_id or uuid4(),
                tenant_id=tenant_id,
                feedback_case_id=command.feedback_case_id,
                label_version=next_version,
                approval_status="pending",
                labeled_by=command.labeled_by,
                labeled_at=command.labeled_at,
                approved_by=None,
                approved_at=None,
                approval_idempotency_key=None,
                idempotency_key=command.idempotency_key,
                recorded_at=recorded_at,
                **command.model_dump(
                    exclude={
                        "feedback_case_id",
                        "expected_previous_version",
                        "labeled_by",
                        "labeled_at",
                        "idempotency_key",
                    }
                ),
            )
            inserted = await repository.insert_label(label=label)
            if not inserted:
                concurrent = await repository.get_label_by_idempotency_key(
                    tenant_id=tenant_id,
                    idempotency_key=command.idempotency_key,
                )
                if concurrent is None:
                    raise AnalysisFeedbackPersistenceError(
                        "Feedback Label 写入冲突，但无法读取已有记录"
                    )
                self._ensure_same_submitted_label(concurrent, command)
                return FeedbackLabelWriteOutcome(concurrent, False)

            if previous is not None:
                superseded = await repository.mark_label_superseded(
                    tenant_id=tenant_id,
                    feedback_case_id=command.feedback_case_id,
                    label_id=previous.id,
                )
                if not superseded:
                    raise AnalysisFeedbackConflictError(
                        "上一个 Feedback Label 版本已发生变化"
                    )

            status_changed = await repository.update_case_status(
                tenant_id=tenant_id,
                feedback_case_id=command.feedback_case_id,
                expected_statuses=(feedback_case.status,),
                status="needs_label",
            )
            if not status_changed:
                raise AnalysisFeedbackConflictError(
                    "Feedback Case 状态已发生变化"
                )
            return FeedbackLabelWriteOutcome(label, True)
        except (
            AnalysisFeedbackTargetNotFoundError,
            AnalysisFeedbackConflictError,
            AnalysisFeedbackPersistenceError,
        ):
            raise
        except IntegrityError as exc:
            raise AnalysisFeedbackConflictError(
                "Feedback Label 写入发生数据库约束冲突"
            ) from exc
        except SQLAlchemyError as exc:
            raise AnalysisFeedbackPersistenceError(
                "Feedback Label 持久化失败"
            ) from exc

    async def approve_label(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        command: ApproveAnalysisFeedbackLabelCommand,
    ) -> FeedbackLabelWriteOutcome:
        tenant_id = self._validate_identity(tenant_id, "tenant_id")
        repository = PostgresAnalysisFeedbackRepository(session)

        try:
            replay = (
                await repository.get_label_by_approval_idempotency_key(
                    tenant_id=tenant_id,
                    idempotency_key=command.idempotency_key,
                )
            )
            if replay is not None:
                self._ensure_same_approval(replay, command)
                return FeedbackLabelWriteOutcome(replay, False)

            feedback_case = await repository.get_case_for_update(
                tenant_id=tenant_id,
                feedback_case_id=command.feedback_case_id,
            )
            if feedback_case is None:
                raise AnalysisFeedbackTargetNotFoundError(
                    "Feedback Case 不存在"
                )
            replay = (
                await repository.get_label_by_approval_idempotency_key(
                    tenant_id=tenant_id,
                    idempotency_key=command.idempotency_key,
                )
            )
            if replay is not None:
                self._ensure_same_approval(replay, command)
                return FeedbackLabelWriteOutcome(replay, False)
            if feedback_case.status in {"excluded", "frozen"}:
                raise AnalysisFeedbackConflictError(
                    "当前 Feedback Case 状态不允许批准标签"
                )

            label = await repository.get_label_for_update(
                tenant_id=tenant_id,
                feedback_case_id=command.feedback_case_id,
                label_id=command.label_id,
            )
            if label is None:
                raise AnalysisFeedbackTargetNotFoundError(
                    "Feedback Label 不存在"
                )
            latest = await repository.get_latest_label_for_update(
                tenant_id=tenant_id,
                feedback_case_id=command.feedback_case_id,
            )
            if latest is None or latest.id != label.id:
                raise AnalysisFeedbackConflictError(
                    "只能批准当前最新标签版本"
                )
            if (
                label.label_version != command.expected_label_version
                or label.approval_status != "pending"
            ):
                raise AnalysisFeedbackConflictError(
                    "Feedback Label 版本或状态已发生变化"
                )
            if command.approved_at < label.labeled_at:
                raise ValueError(
                    "approved_at cannot be earlier than labeled_at"
                )
            if command.approved_by == label.labeled_by:
                raise AnalysisFeedbackConflictError(
                    "Feedback Label submitter cannot approve their own label"
                )

            approved = await repository.approve_label(
                tenant_id=tenant_id,
                feedback_case_id=command.feedback_case_id,
                label_id=command.label_id,
                expected_label_version=command.expected_label_version,
                approved_by=command.approved_by,
                approved_at=command.approved_at,
                review_reason=command.review_reason,
                approval_idempotency_key=command.idempotency_key,
            )
            if not approved:
                raise AnalysisFeedbackConflictError(
                    "Feedback Label 版本或状态已发生变化"
                )
            status_changed = await repository.update_case_status(
                tenant_id=tenant_id,
                feedback_case_id=command.feedback_case_id,
                expected_statuses=("collected", "needs_label"),
                status=(
                    "excluded"
                    if label.verdict == "not_evaluable"
                    else "labeled"
                ),
            )
            if not status_changed:
                raise AnalysisFeedbackConflictError(
                    "Feedback Case 状态已发生变化"
                )

            approved_label = label.model_copy(
                update={
                    "approval_status": "approved",
                    "approved_by": command.approved_by,
                    "approved_at": command.approved_at,
                    "approval_idempotency_key": command.idempotency_key,
                    "reviewed_by": command.approved_by,
                    "reviewed_at": command.approved_at,
                    "review_reason": command.review_reason,
                    "review_idempotency_key": command.idempotency_key,
                }
            )
            return FeedbackLabelWriteOutcome(approved_label, True)
        except (
            AnalysisFeedbackTargetNotFoundError,
            AnalysisFeedbackConflictError,
            AnalysisFeedbackPersistenceError,
        ):
            raise
        except IntegrityError as exc:
            raise AnalysisFeedbackConflictError(
                "Feedback Label 批准发生数据库约束冲突"
            ) from exc
        except SQLAlchemyError as exc:
            raise AnalysisFeedbackPersistenceError(
                "Feedback Label 批准持久化失败"
            ) from exc

    async def request_label_changes(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        command: RequestChangesAnalysisFeedbackLabelCommand,
    ) -> FeedbackLabelWriteOutcome:
        """退回最新标签版本；审核人与标注人必须分离并完整留痕。"""

        tenant_id = self._validate_identity(tenant_id, "tenant_id")
        repository = PostgresAnalysisFeedbackRepository(session)
        try:
            replay = await repository.get_label_by_review_idempotency_key(
                tenant_id=tenant_id,
                idempotency_key=command.idempotency_key,
            )
            if replay is not None:
                self._ensure_same_change_request(replay, command)
                return FeedbackLabelWriteOutcome(replay, False)

            feedback_case = await repository.get_case_for_update(
                tenant_id=tenant_id,
                feedback_case_id=command.feedback_case_id,
            )
            if feedback_case is None:
                raise AnalysisFeedbackTargetNotFoundError(
                    "Feedback Case 不存在"
                )
            if feedback_case.status in {"excluded", "frozen"}:
                raise AnalysisFeedbackConflictError(
                    "当前 Feedback Case 状态不允许退回标签"
                )
            label = await repository.get_label_for_update(
                tenant_id=tenant_id,
                feedback_case_id=command.feedback_case_id,
                label_id=command.label_id,
            )
            if label is None:
                raise AnalysisFeedbackTargetNotFoundError(
                    "Feedback Label 不存在"
                )
            latest = await repository.get_latest_label_for_update(
                tenant_id=tenant_id,
                feedback_case_id=command.feedback_case_id,
            )
            if latest is None or latest.id != label.id:
                raise AnalysisFeedbackConflictError(
                    "只能退回当前最新标签版本"
                )
            if (
                label.label_version != command.expected_label_version
                or label.approval_status != "pending"
            ):
                raise AnalysisFeedbackConflictError(
                    "Feedback Label 版本或状态已发生变化"
                )
            if command.reviewed_at < label.labeled_at:
                raise ValueError(
                    "reviewed_at cannot be earlier than labeled_at"
                )
            if command.reviewed_by == label.labeled_by:
                raise AnalysisFeedbackConflictError(
                    "Feedback Label submitter cannot review their own label"
                )

            changed = await repository.request_label_changes(
                tenant_id=tenant_id,
                feedback_case_id=command.feedback_case_id,
                label_id=command.label_id,
                expected_label_version=command.expected_label_version,
                reviewed_by=command.reviewed_by,
                reviewed_at=command.reviewed_at,
                review_reason=command.review_reason,
                review_idempotency_key=command.idempotency_key,
            )
            if not changed:
                raise AnalysisFeedbackConflictError(
                    "Feedback Label 版本或状态已发生变化"
                )
            status_changed = await repository.update_case_status(
                tenant_id=tenant_id,
                feedback_case_id=command.feedback_case_id,
                expected_statuses=("collected", "needs_label"),
                status="needs_label",
            )
            if not status_changed:
                raise AnalysisFeedbackConflictError(
                    "Feedback Case 状态已发生变化"
                )
            reviewed = label.model_copy(
                update={
                    "approval_status": "rejected",
                    "reviewed_by": command.reviewed_by,
                    "reviewed_at": command.reviewed_at,
                    "review_reason": command.review_reason,
                    "review_idempotency_key": command.idempotency_key,
                }
            )
            return FeedbackLabelWriteOutcome(reviewed, True)
        except (
            AnalysisFeedbackTargetNotFoundError,
            AnalysisFeedbackConflictError,
            AnalysisFeedbackPersistenceError,
        ):
            raise
        except IntegrityError as exc:
            raise AnalysisFeedbackConflictError(
                "Feedback Label 退回发生数据库约束冲突"
            ) from exc
        except SQLAlchemyError as exc:
            raise AnalysisFeedbackPersistenceError(
                "Feedback Label 退回持久化失败"
            ) from exc

    @staticmethod
    def _validate_previous_label_version(
        previous: AnalysisFeedbackLabel | None,
        command: SubmitAnalysisFeedbackLabelCommand,
    ) -> None:
        if previous is None and command.expected_previous_version is not None:
            raise AnalysisFeedbackConflictError(
                "Feedback Label 期望版本不存在"
            )
        if previous is not None and (
            command.expected_previous_version != previous.label_version
        ):
            raise AnalysisFeedbackConflictError(
                "Feedback Label 期望版本已发生变化"
            )

    @staticmethod
    def _ensure_same_case(
        existing: AnalysisFeedbackCase,
        content_sha256: str,
    ) -> None:
        if existing.content_sha256 != content_sha256:
            raise AnalysisFeedbackConflictError(
                "同一个 idempotency_key 对应不同的 Feedback Case"
            )

    @staticmethod
    def _ensure_same_publication_outcome(
        existing: PublicationOutcome,
        content_sha256: str,
    ) -> None:
        if existing.content_sha256 != content_sha256:
            raise AnalysisFeedbackConflictError(
                "同一个 idempotency_key 对应不同的发布效果"
            )

    @staticmethod
    def _ensure_same_submitted_label(
        existing: AnalysisFeedbackLabel,
        command: SubmitAnalysisFeedbackLabelCommand,
    ) -> None:
        expected_version = (
            1
            if command.expected_previous_version is None
            else command.expected_previous_version + 1
        )
        if (
            existing.feedback_case_id != command.feedback_case_id
            or existing.label_version != expected_version
            or existing.verdict != command.verdict
            or existing.allowed_dominant_drivers
            != command.allowed_dominant_drivers
            or existing.required_evidence_news_ids
            != command.required_evidence_news_ids
            or existing.forbidden_evidence_news_ids
            != command.forbidden_evidence_news_ids
            or existing.required_metric_keys != command.required_metric_keys
            or existing.must_state_limitation
            != command.must_state_limitation
            or existing.operator_comment != command.operator_comment
            or existing.labeled_by != command.labeled_by
        ):
            raise AnalysisFeedbackConflictError(
                "同一个 idempotency_key 对应不同的 Feedback Label"
            )

    @staticmethod
    def _ensure_same_approval(
        existing: AnalysisFeedbackLabel,
        command: ApproveAnalysisFeedbackLabelCommand,
    ) -> None:
        if (
            existing.feedback_case_id != command.feedback_case_id
            or existing.id != command.label_id
            or existing.label_version != command.expected_label_version
            or existing.approval_status != "approved"
            or existing.approved_by != command.approved_by
            or existing.review_reason != command.review_reason
        ):
            raise AnalysisFeedbackConflictError(
                "同一个 idempotency_key 对应不同的标签批准内容"
            )

    @staticmethod
    def _ensure_same_change_request(
        existing: AnalysisFeedbackLabel,
        command: RequestChangesAnalysisFeedbackLabelCommand,
    ) -> None:
        if (
            existing.feedback_case_id != command.feedback_case_id
            or existing.id != command.label_id
            or existing.label_version != command.expected_label_version
            or existing.approval_status != "rejected"
            or existing.reviewed_by != command.reviewed_by
            or existing.review_reason != command.review_reason
        ):
            raise AnalysisFeedbackConflictError(
                "同一个 idempotency_key 对应不同的标签退回内容"
            )

    @staticmethod
    def _validate_identity(value: str, field_name: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field_name} 不能为空")
        if len(normalized) > 128:
            raise ValueError(f"{field_name} 超过最大长度")
        return normalized

    @staticmethod
    def _validate_aware_datetime(value: datetime, field_name: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")

    @staticmethod
    def _validate_pagination(*, offset: int, limit: int) -> None:
        if offset < 0:
            raise ValueError("offset must be nonnegative")
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
