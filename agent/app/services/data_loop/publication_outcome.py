"""Turn trusted aggregate publication outcomes into bounded feedback facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.analysis_feedback import (
    FeedbackSeverity,
    PublicationOutcome,
    RecordPublicationOutcomeCommand,
)
from app.services.data_loop.feedback_collector import (
    AnalysisFeedbackCollector,
    AnalysisFeedbackTargetNotFoundError,
    FeedbackCaseWriteOutcome,
    PublicationOutcomeWriteOutcome,
)
from app.services.hot_news_run_store import PostgresHotNewsRunStore


@dataclass(frozen=True, slots=True)
class PublicationOutcomeFeedbackPolicy:
    """Deterministic aggregate-only underperformance policy."""

    version: str = "publication-outcome-feedback-v1"
    minimum_impressions: int = 100
    minimum_ctr: float = 0.01
    maximum_complaint_rate: float = 0.01
    maximum_corrections: int = 0

    def validate(self) -> None:
        if not self.version.strip():
            raise ValueError("publication outcome policy version cannot be empty")
        if self.minimum_impressions < 1:
            raise ValueError("minimum_impressions must be positive")
        if not 0 <= self.minimum_ctr <= 1:
            raise ValueError("minimum_ctr must be in [0, 1]")
        if not 0 <= self.maximum_complaint_rate <= 1:
            raise ValueError("maximum_complaint_rate must be in [0, 1]")
        if self.maximum_corrections < 0:
            raise ValueError("maximum_corrections cannot be negative")

    def classify(
        self,
        command: RecordPublicationOutcomeCommand,
    ) -> FeedbackSeverity | None:
        metrics = command.metrics
        impressions = metrics.impressions
        ctr = metrics.clicks / impressions if impressions else 0.0
        complaint_rate = (
            metrics.complaints / impressions if impressions else 0.0
        )
        failed = (
            (
                impressions >= self.minimum_impressions
                and ctr < self.minimum_ctr
            )
            or complaint_rate > self.maximum_complaint_rate
            or metrics.corrections > self.maximum_corrections
        )
        if not failed:
            return None
        if complaint_rate >= 0.05 or metrics.corrections >= 3:
            return "critical"
        if complaint_rate >= 0.02 or metrics.corrections > 0:
            return "high"
        return "medium"


@dataclass(frozen=True, slots=True)
class PublicationOutcomeCollectionResult:
    outcome: PublicationOutcome
    outcome_created: bool
    feedback_case: FeedbackCaseWriteOutcome | None


class PublicationOutcomeFeedbackService:
    """Record an aggregate window and its derived case in one transaction."""

    def __init__(
        self,
        *,
        memory_store: PostgresHotNewsRunStore,
        collector: AnalysisFeedbackCollector | None = None,
        policy: PublicationOutcomeFeedbackPolicy | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._collector = collector or AnalysisFeedbackCollector()
        self._policy = policy or PublicationOutcomeFeedbackPolicy()
        self._policy.validate()

    async def record_and_collect(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        command: RecordPublicationOutcomeCommand,
        recorded_at: datetime,
    ) -> PublicationOutcomeCollectionResult:
        memory = await self._memory_store.get_analysis_memory(
            tenant_id=tenant_id,
            run_id=command.run_id,
            news_id=command.news_id,
        )
        if memory is None or (
            memory.run_idempotency_key is not None
            and memory.run_idempotency_key != command.run_idempotency_key
        ):
            raise AnalysisFeedbackTargetNotFoundError(
                "热点分析运行或新闻不存在"
            )

        write: PublicationOutcomeWriteOutcome = (
            await self._collector.record_publication_outcome(
                session,
                tenant_id=tenant_id,
                command=command,
                recorded_at=recorded_at,
            )
        )
        severity = self._policy.classify(command)
        feedback_case = None
        if severity is not None:
            feedback_case = await self._collector.collect_from_publication_outcome(
                session,
                tenant_id=tenant_id,
                memory=memory,
                outcome=write.outcome,
                severity=severity,
                recorded_at=recorded_at,
                idempotency_key=(
                    f"feedback:publication:{write.outcome.id}:"
                    f"{self._policy.version}"
                ),
            )
        return PublicationOutcomeCollectionResult(
            outcome=write.outcome,
            outcome_created=write.created,
            feedback_case=feedback_case,
        )
