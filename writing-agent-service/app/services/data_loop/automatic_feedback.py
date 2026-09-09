"""Automatic, bounded Feedback Case collection for the online hot-news path.

The collector deliberately persists only the trusted analysis input, a
validated output (when one exists), aggregate model metadata, and stable
references.  Raw model output and raw user behaviour never enter the loop.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.db.session import Database
from app.domain.errors import HotNewsAnalysisAttemptError
from app.schemas.analysis_feedback import (
    CollectAnalysisFeedbackCommand,
    FeedbackSourceReference,
)
from app.schemas.hot_news import HotNewsAnalysisInput
from app.schemas.hot_news_memory import HotNewsAnalysisMemory
from app.services.data_loop.feedback_collector import AnalysisFeedbackCollector
from app.services.hot_news_orchestration import (
    AnalyzedHotNews,
    HotNewsRunRequest,
    HotNewsRunResult,
)
from app.services.hot_news_run_store import PostgresHotNewsRunStore


@dataclass(frozen=True, slots=True)
class AutomaticFeedbackPolicy:
    """Deterministic automatic-case rules shipped with a Production Bundle."""

    version: str = "automatic-feedback-v1"
    low_confidence_threshold: float = 0.60
    retrieval_gap_hot_score_threshold: float = 0.70

    def validate(self) -> None:
        if not self.version.strip():
            raise ValueError("automatic feedback policy version cannot be empty")
        for name in (
            "low_confidence_threshold",
            "retrieval_gap_hot_score_threshold",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0, 1]")


class AutomaticHotNewsFeedbackSink:
    """Bridges successful/failed online runs into idempotent Feedback Cases."""

    def __init__(
        self,
        *,
        database: Database,
        run_store: PostgresHotNewsRunStore,
        collector: AnalysisFeedbackCollector | None = None,
        policy: AutomaticFeedbackPolicy | None = None,
    ) -> None:
        self._database = database
        self._run_store = run_store
        self._collector = collector or AnalysisFeedbackCollector()
        self._policy = policy or AutomaticFeedbackPolicy()
        self._policy.validate()

    async def collect_completed_run(
        self,
        *,
        result: HotNewsRunResult,
        run_id: str,
    ) -> None:
        resolved_run_id = UUID(run_id)
        completed_at = datetime.now(UTC)
        memories = tuple(
            self._memory_from_analyzed(
                result=result,
                analyzed=item,
                run_id=resolved_run_id,
                completed_at=completed_at,
            )
            for item in result.analyzed_news
        )
        await self._collect_memories(
            memories=memories,
            run_idempotency_key=result.idempotency_key,
        )

    async def collect_persisted_run(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        run_id: str,
    ) -> None:
        # Validate the control-plane identifier even though retrieval is scoped
        # by tenant + idempotency key.  This catches corrupted replay outcomes.
        expected_run_id = UUID(run_id)
        memories = await self._run_store.list_analysis_memories(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
        )
        if any(memory.run_id != expected_run_id for memory in memories):
            raise ValueError("persisted feedback repair resolved a different run")
        await self._collect_memories(
            memories=memories,
            run_idempotency_key=idempotency_key,
        )

    async def collect_analysis_failure(
        self,
        *,
        request: HotNewsRunRequest,
        error: HotNewsAnalysisAttemptError,
    ) -> None:
        analysis_input = error.analysis_input
        if not isinstance(analysis_input, HotNewsAnalysisInput):
            raise TypeError("analysis failure omitted a valid trusted input snapshot")

        now = datetime.now(UTC)
        reference_id = self._stable_key(
            request.tenant_id,
            request.idempotency_key,
            analysis_input.news_id,
            error.request_id or "no-request-id",
            self._policy.version,
        )
        command = CollectAnalysisFeedbackCommand(
            run_id=None,
            run_idempotency_key=request.idempotency_key,
            news_id=analysis_input.news_id,
            source_type="validation_failed",
            problem_type="schema_violation",
            severity="high",
            production_bundle_version=request.production_bundle_version,
            analysis_input_snapshot=analysis_input,
            analysis_output_snapshot=None,
            source_reference=FeedbackSourceReference(
                reference_type="validation_attempt",
                reference_id=reference_id,
                request_id=error.request_id,
                diagnostic_code="strict_output_validation_failed",
                diagnostic_summary=(
                    "Model output failed the strict schema or deterministic "
                    "business validator; raw output was intentionally discarded."
                ),
            ),
            occurred_at=now,
            idempotency_key=f"feedback:validation:{reference_id}",
        )
        async with self._database.session() as session:
            await self._collector.collect(
                session,
                tenant_id=request.tenant_id,
                command=command,
                recorded_at=now,
            )

    async def _collect_memories(
        self,
        *,
        memories: tuple[HotNewsAnalysisMemory, ...],
        run_idempotency_key: str,
    ) -> None:
        if not memories:
            return
        now = datetime.now(UTC)
        async with self._database.session() as session:
            for memory in memories:
                confidence = memory.analysis_report.overall_confidence
                if confidence < self._policy.low_confidence_threshold:
                    await self._collector.collect_from_analysis_memory(
                        session,
                        tenant_id=memory.tenant_id,
                        memory=memory,
                        run_idempotency_key=run_idempotency_key,
                        source_type="low_confidence",
                        problem_type="low_confidence",
                        severity=("high" if confidence < 0.35 else "medium"),
                        source_reference=FeedbackSourceReference(
                            reference_type="analysis_run",
                            reference_id=str(memory.run_id),
                            request_id=memory.fastgpt_request_id,
                            diagnostic_code="confidence_below_threshold",
                            diagnostic_summary=(
                                "Validated output confidence was below the "
                                "versioned automatic-feedback threshold."
                            ),
                        ),
                        occurred_at=memory.validated_at,
                        idempotency_key=self._case_idempotency_key(
                            memory=memory,
                            source_type="low-confidence",
                        ),
                        recorded_at=now,
                    )

                if (
                    memory.analysis_input.hot_score
                    >= self._policy.retrieval_gap_hot_score_threshold
                    and not memory.analysis_input.related_news
                ):
                    await self._collector.collect_from_analysis_memory(
                        session,
                        tenant_id=memory.tenant_id,
                        memory=memory,
                        run_idempotency_key=run_idempotency_key,
                        source_type="retrieval_error",
                        problem_type="retrieval_miss",
                        severity="medium",
                        source_reference=FeedbackSourceReference(
                            reference_type="retrieval_attempt",
                            reference_id=(
                                f"{memory.run_id}:{memory.news_id}"
                            ),
                            request_id=memory.fastgpt_request_id,
                            diagnostic_code="high_score_without_related_evidence",
                            diagnostic_summary=(
                                "A high-score item reached analysis without any "
                                "related-news evidence."
                            ),
                        ),
                        occurred_at=memory.validated_at,
                        idempotency_key=self._case_idempotency_key(
                            memory=memory,
                            source_type="retrieval-gap",
                        ),
                        recorded_at=now,
                    )

    def _case_idempotency_key(
        self,
        *,
        memory: HotNewsAnalysisMemory,
        source_type: str,
    ) -> str:
        digest = self._stable_key(
            memory.tenant_id,
            str(memory.run_id),
            memory.news_id,
            source_type,
            self._policy.version,
        )
        return f"feedback:auto:{digest}"

    @staticmethod
    def _memory_from_analyzed(
        *,
        result: HotNewsRunResult,
        analyzed: AnalyzedHotNews,
        run_id: UUID,
        completed_at: datetime,
    ) -> HotNewsAnalysisMemory:
        request = result.request
        return HotNewsAnalysisMemory(
            run_id=run_id,
            tenant_id=request.tenant_id,
            news_id=analyzed.news_id,
            rank=analyzed.rank,
            production_bundle_version=request.production_bundle_version,
            workflow_version=request.workflow_version,
            payload_schema_version="2.0",
            analysis_input=analyzed.analysis_input,
            analysis_report=analyzed.analysis.value,
            fastgpt_request_id=analyzed.analysis.request_id,
            usage=analyzed.analysis.usage or {},
            captured_at=analyzed.captured_at,
            validated_at=analyzed.validated_at,
            completed_at=completed_at,
        )

    @staticmethod
    def _stable_key(*parts: str) -> str:
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
