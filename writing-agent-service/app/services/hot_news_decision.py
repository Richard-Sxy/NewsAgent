"""处理热点运营决策，并原子写入需要进入 Data Loop 的反馈。"""
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import Database
from app.models.hot_news_decision import HotNewsDecision
from app.schemas.hot_news_decision import RecordHotNewsDecisionCommand
from app.schemas.analysis_feedback import FeedbackProblemType, FeedbackSeverity
from app.schemas.hot_news_memory import HotNewsAnalysisMemory
from app.services.data_loop.feedback_collector import AnalysisFeedbackCollector
from app.services.hot_news_run_store import PostgresHotNewsRunStore


class HotNewsDecisionTargetNotFoundError(LookupError):
    """热点运行、新闻或被替代决策不存在。"""


class HotNewsDecisionConflictError(ValueError):
    """同一幂等键被用于不同决策内容。"""


class HotNewsDecisionPersistenceError(RuntimeError):
    """热点决策持久化失败。"""

    retryable = True


class HotNewsDecisionService:
    def __init__(
        self,
        *,
        database: Database,
        memory_store: PostgresHotNewsRunStore,
        feedback_collector: AnalysisFeedbackCollector | None = None,
    ) -> None:
        self._database = database
        self._memory_store = memory_store
        self._feedback_collector = (
            feedback_collector or AnalysisFeedbackCollector()
        )

    async def record_decision(
        self,
        *,
        tenant_id: str,
        operator_id: str,
        command: RecordHotNewsDecisionCommand,
        feedback_problem_type: FeedbackProblemType = "analysis_incorrect",
        feedback_severity: FeedbackSeverity = "medium",
    ) -> tuple[HotNewsDecision, bool]:
        tenant_id = tenant_id.strip()
        operator_id = operator_id.strip()

        if not tenant_id:
            raise ValueError("tenant_id 不能为空")
        if not operator_id:
            raise ValueError("operator_id 不能为空")

        analysis_memory = await self._memory_store.get_analysis_memory(
            tenant_id=tenant_id,
            run_id=command.run_id,
            news_id=command.news_id,
        )
        if analysis_memory is None:
            raise HotNewsDecisionTargetNotFoundError(
                "热点决策目标不存在"
            )

        values = {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "run_id": command.run_id,
            "news_id": command.news_id,
            "decision_type": command.decision_type,
            "reason": command.reason,
            "correction_payload": command.correction_payload,
            "operator_id": operator_id,
            "idempotency_key": command.idempotency_key,
            "supersedes_decision_id": command.supersedes_decision_id,
        }

        try:
            async with self._database.session() as session:
                if command.supersedes_decision_id is not None:
                    superseded_result = await session.execute(
                        select(HotNewsDecision).where(
                            HotNewsDecision.id
                            == command.supersedes_decision_id,
                            HotNewsDecision.tenant_id == tenant_id,
                            HotNewsDecision.run_id == command.run_id,
                            HotNewsDecision.news_id == command.news_id,
                        )
                    )
                    superseded = superseded_result.scalar_one_or_none()
                    if superseded is None:
                        raise HotNewsDecisionTargetNotFoundError(
                            "热点决策目标不存在"
                        )

                statement = (
                    insert(HotNewsDecision)
                    .values(**values)
                    .on_conflict_do_nothing(
                        constraint=(
                            "uq_hot_news_decisions_"
                            "tenant_idempotency_key"
                        )
                    )
                    .returning(HotNewsDecision)
                )
                insert_result = await session.execute(statement)
                decision = insert_result.scalar_one_or_none()

                if decision is not None:
                    await self._collect_feedback_if_required(
                        session=session,
                        tenant_id=tenant_id,
                        analysis_memory=analysis_memory,
                        decision=decision,
                        problem_type=feedback_problem_type,
                        severity=feedback_severity,
                    )
                    return decision, True

                existing_result = await session.execute(
                    select(HotNewsDecision).where(
                        HotNewsDecision.tenant_id == tenant_id,
                        HotNewsDecision.idempotency_key == command.idempotency_key,
                    )
                )
                existing = existing_result.scalar_one_or_none()

                if existing is None:
                    raise HotNewsDecisionPersistenceError(
                        "热点决策写入冲突，但无法读取已有记录"
                    )

                if not self._same_business_content(
                    existing=existing,
                    tenant_id=tenant_id,
                    operator_id=operator_id,
                    command=command,
                ):
                    raise HotNewsDecisionConflictError(
                        "同一个 idempotency_key 对应不同的决策内容"
                    )

                await self._collect_feedback_if_required(
                    session=session,
                    tenant_id=tenant_id,
                    analysis_memory=analysis_memory,
                    decision=existing,
                    problem_type=feedback_problem_type,
                    severity=feedback_severity,
                )
                return existing, False

        except (
            HotNewsDecisionTargetNotFoundError,
            HotNewsDecisionConflictError,
            HotNewsDecisionPersistenceError,
        ):
            raise
        except SQLAlchemyError as exc:
            raise HotNewsDecisionPersistenceError(
                "保存热点决策失败"
            ) from exc

    async def _collect_feedback_if_required(
        self,
        *,
        session: AsyncSession,
        tenant_id: str,
        analysis_memory: HotNewsAnalysisMemory,
        decision: HotNewsDecision,
        problem_type: FeedbackProblemType,
        severity: FeedbackSeverity,
    ) -> None:
        """Keep decision + feedback in the same database transaction."""

        if decision.decision_type not in {"rejected", "corrected"}:
            return
        now = datetime.now(UTC)
        run_idempotency_key = (
            analysis_memory.run_idempotency_key
            or f"analysis-run:{analysis_memory.run_id}"
        )
        await self._feedback_collector.collect_from_operator_decision(
            session,
            tenant_id=tenant_id,
            memory=analysis_memory,
            run_idempotency_key=run_idempotency_key,
            decision=decision,
            problem_type=problem_type,
            severity=severity,
            occurred_at=now,
            recorded_at=now,
        )

    @staticmethod
    def _same_business_content(
        *,
        existing: HotNewsDecision,
        tenant_id: str,
        operator_id: str,
        command: RecordHotNewsDecisionCommand,
    ) -> bool:
        return (
            existing.tenant_id == tenant_id
            and existing.run_id == command.run_id
            and existing.news_id == command.news_id
            and existing.decision_type == command.decision_type
            and existing.reason == command.reason
            and existing.correction_payload
            == command.correction_payload
            and existing.operator_id == operator_id
            and existing.idempotency_key
            == command.idempotency_key
            and existing.supersedes_decision_id
            == command.supersedes_decision_id
        )
