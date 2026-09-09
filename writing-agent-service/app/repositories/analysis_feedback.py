"""Data Loop 反馈 Case 与标签的 PostgreSQL Repository。"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analysis_feedback import (
    AnalysisFeedbackCaseRecord,
    AnalysisFeedbackLabelRecord,
    PublicationOutcomeRecord,
)
from app.models.hot_news import HotNewsAnalysisRun
from app.schemas.analysis_feedback import (
    AnalysisFeedbackCase,
    AnalysisFeedbackLabel,
    FeedbackStatus,
    PublicationOutcome,
    PublicationOutcomeMetrics,
)


class AnalysisFeedbackDataCorruptedError(RuntimeError):
    """数据库中的反馈记录无法通过领域 Schema 校验。"""


class PostgresAnalysisFeedbackRepository:
    """只使用调用方传入的事务 Session，不自行提交。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def analysis_run_contains_news(
        self,
        *,
        tenant_id: str,
        run_id: UUID,
        run_idempotency_key: str,
        news_id: str,
        production_bundle_version: str | None = None,
    ) -> bool:
        statement = select(HotNewsAnalysisRun).where(
            HotNewsAnalysisRun.tenant_id == tenant_id,
            HotNewsAnalysisRun.id == run_id,
            HotNewsAnalysisRun.idempotency_key == run_idempotency_key,
            HotNewsAnalysisRun.status == "completed",
        )
        if production_bundle_version is not None:
            statement = statement.where(
                HotNewsAnalysisRun.production_bundle_version
                == production_bundle_version
            )
        result = await self._session.execute(statement)
        run = result.scalar_one_or_none()
        if run is None or not isinstance(run.result_payload, dict):
            return False
        analyzed_news = run.result_payload.get("analyzed_news")
        return isinstance(analyzed_news, list) and any(
            isinstance(item, dict) and item.get("news_id") == news_id
            for item in analyzed_news
        )

    async def get_case_by_idempotency_key(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
    ) -> AnalysisFeedbackCase | None:
        statement = select(AnalysisFeedbackCaseRecord).where(
            AnalysisFeedbackCaseRecord.tenant_id == tenant_id,
            AnalysisFeedbackCaseRecord.idempotency_key == idempotency_key,
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_case_domain(record)

    async def get_case(
        self,
        *,
        tenant_id: str,
        feedback_case_id: UUID,
    ) -> AnalysisFeedbackCase | None:
        statement = select(AnalysisFeedbackCaseRecord).where(
            AnalysisFeedbackCaseRecord.tenant_id == tenant_id,
            AnalysisFeedbackCaseRecord.id == feedback_case_id,
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_case_domain(record)

    async def list_cases(
        self,
        *,
        tenant_id: str,
        statuses: Sequence[FeedbackStatus] | None,
        offset: int,
        limit: int,
    ) -> list[AnalysisFeedbackCase]:
        statement = select(AnalysisFeedbackCaseRecord).where(
            AnalysisFeedbackCaseRecord.tenant_id == tenant_id
        )
        if statuses:
            statement = statement.where(
                AnalysisFeedbackCaseRecord.status.in_(statuses)
            )
        statement = (
            statement.order_by(
                AnalysisFeedbackCaseRecord.recorded_at.desc(),
                AnalysisFeedbackCaseRecord.id,
            )
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(statement)
        return [self.to_case_domain(record) for record in result.scalars()]

    async def get_case_for_update(
        self,
        *,
        tenant_id: str,
        feedback_case_id: UUID,
    ) -> AnalysisFeedbackCase | None:
        statement = (
            select(AnalysisFeedbackCaseRecord)
            .where(
                AnalysisFeedbackCaseRecord.tenant_id == tenant_id,
                AnalysisFeedbackCaseRecord.id == feedback_case_id,
            )
            .with_for_update()
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_case_domain(record)

    async def insert_case(self, *, case: AnalysisFeedbackCase) -> bool:
        statement = (
            insert(AnalysisFeedbackCaseRecord)
            .values(**self.to_case_values(case))
            .on_conflict_do_nothing(
                constraint="uq_feedback_cases_tenant_idempotency_key"
            )
            .returning(AnalysisFeedbackCaseRecord.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def update_case_status(
        self,
        *,
        tenant_id: str,
        feedback_case_id: UUID,
        expected_statuses: Sequence[FeedbackStatus],
        status: FeedbackStatus,
    ) -> bool:
        statement = (
            update(AnalysisFeedbackCaseRecord)
            .where(
                AnalysisFeedbackCaseRecord.tenant_id == tenant_id,
                AnalysisFeedbackCaseRecord.id == feedback_case_id,
                AnalysisFeedbackCaseRecord.status.in_(expected_statuses),
            )
            .values(status=status)
            .returning(AnalysisFeedbackCaseRecord.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def get_label_by_idempotency_key(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
    ) -> AnalysisFeedbackLabel | None:
        statement = select(AnalysisFeedbackLabelRecord).where(
            AnalysisFeedbackLabelRecord.tenant_id == tenant_id,
            AnalysisFeedbackLabelRecord.idempotency_key == idempotency_key,
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_label_domain(record)

    async def get_label_by_approval_idempotency_key(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
    ) -> AnalysisFeedbackLabel | None:
        statement = select(AnalysisFeedbackLabelRecord).where(
            AnalysisFeedbackLabelRecord.tenant_id == tenant_id,
            AnalysisFeedbackLabelRecord.approval_idempotency_key
            == idempotency_key,
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_label_domain(record)

    async def list_labels(
        self,
        *,
        tenant_id: str,
        feedback_case_id: UUID,
    ) -> list[AnalysisFeedbackLabel]:
        statement = (
            select(AnalysisFeedbackLabelRecord)
            .where(
                AnalysisFeedbackLabelRecord.tenant_id == tenant_id,
                AnalysisFeedbackLabelRecord.feedback_case_id
                == feedback_case_id,
            )
            .order_by(AnalysisFeedbackLabelRecord.label_version)
        )
        result = await self._session.execute(statement)
        return [self.to_label_domain(record) for record in result.scalars()]

    async def get_publication_outcome_by_idempotency_key(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
    ) -> PublicationOutcome | None:
        statement = select(PublicationOutcomeRecord).where(
            PublicationOutcomeRecord.tenant_id == tenant_id,
            PublicationOutcomeRecord.idempotency_key == idempotency_key,
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_outcome_domain(record)

    async def get_publication_outcome(
        self,
        *,
        tenant_id: str,
        outcome_id: UUID,
    ) -> PublicationOutcome | None:
        statement = select(PublicationOutcomeRecord).where(
            PublicationOutcomeRecord.tenant_id == tenant_id,
            PublicationOutcomeRecord.id == outcome_id,
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_outcome_domain(record)

    async def insert_publication_outcome(
        self,
        *,
        outcome: PublicationOutcome,
    ) -> bool:
        statement = (
            insert(PublicationOutcomeRecord)
            .values(**self.to_outcome_values(outcome))
            .on_conflict_do_nothing(
                constraint=(
                    "uq_publication_outcomes_tenant_idempotency_key"
                )
            )
            .returning(PublicationOutcomeRecord.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def get_latest_label_for_update(
        self,
        *,
        tenant_id: str,
        feedback_case_id: UUID,
    ) -> AnalysisFeedbackLabel | None:
        statement = (
            select(AnalysisFeedbackLabelRecord)
            .where(
                AnalysisFeedbackLabelRecord.tenant_id == tenant_id,
                AnalysisFeedbackLabelRecord.feedback_case_id
                == feedback_case_id,
            )
            .order_by(AnalysisFeedbackLabelRecord.label_version.desc())
            .limit(1)
            .with_for_update()
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_label_domain(record)

    async def get_label_for_update(
        self,
        *,
        tenant_id: str,
        feedback_case_id: UUID,
        label_id: UUID,
    ) -> AnalysisFeedbackLabel | None:
        statement = (
            select(AnalysisFeedbackLabelRecord)
            .where(
                AnalysisFeedbackLabelRecord.tenant_id == tenant_id,
                AnalysisFeedbackLabelRecord.feedback_case_id
                == feedback_case_id,
                AnalysisFeedbackLabelRecord.id == label_id,
            )
            .with_for_update()
        )
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        return None if record is None else self.to_label_domain(record)

    async def insert_label(self, *, label: AnalysisFeedbackLabel) -> bool:
        statement = (
            insert(AnalysisFeedbackLabelRecord)
            .values(**self.to_label_values(label))
            .on_conflict_do_nothing(
                constraint="uq_feedback_labels_tenant_idempotency_key"
            )
            .returning(AnalysisFeedbackLabelRecord.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def mark_label_superseded(
        self,
        *,
        tenant_id: str,
        feedback_case_id: UUID,
        label_id: UUID,
    ) -> bool:
        statement = (
            update(AnalysisFeedbackLabelRecord)
            .where(
                AnalysisFeedbackLabelRecord.tenant_id == tenant_id,
                AnalysisFeedbackLabelRecord.feedback_case_id
                == feedback_case_id,
                AnalysisFeedbackLabelRecord.id == label_id,
                AnalysisFeedbackLabelRecord.approval_status.in_(
                    ("pending", "approved")
                ),
            )
            .values(approval_status="superseded")
            .returning(AnalysisFeedbackLabelRecord.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def approve_label(
        self,
        *,
        tenant_id: str,
        feedback_case_id: UUID,
        label_id: UUID,
        expected_label_version: int,
        approved_by: str,
        approved_at: datetime,
        approval_idempotency_key: str,
    ) -> bool:
        statement = (
            update(AnalysisFeedbackLabelRecord)
            .where(
                AnalysisFeedbackLabelRecord.tenant_id == tenant_id,
                AnalysisFeedbackLabelRecord.feedback_case_id
                == feedback_case_id,
                AnalysisFeedbackLabelRecord.id == label_id,
                AnalysisFeedbackLabelRecord.label_version
                == expected_label_version,
                AnalysisFeedbackLabelRecord.approval_status == "pending",
            )
            .values(
                approval_status="approved",
                approved_by=approved_by,
                approved_at=approved_at,
                approval_idempotency_key=approval_idempotency_key,
            )
            .returning(AnalysisFeedbackLabelRecord.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    @staticmethod
    def to_case_values(case: AnalysisFeedbackCase) -> dict:
        return {
            "id": case.id,
            "tenant_id": case.tenant_id,
            "run_id": case.run_id,
            "run_idempotency_key": case.run_idempotency_key,
            "news_id": case.news_id,
            "source_type": case.source_type,
            "problem_type": case.problem_type,
            "status": case.status,
            "severity": case.severity,
            "production_bundle_version": case.production_bundle_version,
            "analysis_input_snapshot": (
                case.analysis_input_snapshot.model_dump(mode="json")
            ),
            "analysis_output_snapshot": (
                None
                if case.analysis_output_snapshot is None
                else case.analysis_output_snapshot.model_dump(mode="json")
            ),
            "source_reference": case.source_reference.model_dump(mode="json"),
            "occurred_at": case.occurred_at,
            "recorded_at": case.recorded_at,
            "idempotency_key": case.idempotency_key,
            "content_sha256": case.content_sha256,
        }

    @staticmethod
    def to_label_values(label: AnalysisFeedbackLabel) -> dict:
        return {
            "id": label.id,
            "tenant_id": label.tenant_id,
            "feedback_case_id": label.feedback_case_id,
            "label_version": label.label_version,
            "verdict": label.verdict,
            "allowed_dominant_drivers": list(
                label.allowed_dominant_drivers
            ),
            "required_evidence_news_ids": list(
                label.required_evidence_news_ids
            ),
            "forbidden_evidence_news_ids": list(
                label.forbidden_evidence_news_ids
            ),
            "required_metric_keys": list(label.required_metric_keys),
            "must_state_limitation": label.must_state_limitation,
            "operator_comment": label.operator_comment,
            "approval_status": label.approval_status,
            "labeled_by": label.labeled_by,
            "labeled_at": label.labeled_at,
            "approved_by": label.approved_by,
            "approved_at": label.approved_at,
            "idempotency_key": label.idempotency_key,
            "approval_idempotency_key": (
                label.approval_idempotency_key
            ),
            "recorded_at": label.recorded_at,
        }

    @staticmethod
    def to_outcome_values(outcome: PublicationOutcome) -> dict:
        return {
            "id": outcome.id,
            "tenant_id": outcome.tenant_id,
            "run_id": outcome.run_id,
            "run_idempotency_key": outcome.run_idempotency_key,
            "news_id": outcome.news_id,
            "external_publication_id": outcome.external_publication_id,
            "source_system": outcome.source_system,
            "metric_definition_version": (
                outcome.metric_definition_version
            ),
            "window_start": outcome.window_start,
            "window_end": outcome.window_end,
            "impressions": outcome.metrics.impressions,
            "clicks": outcome.metrics.clicks,
            "unique_users": outcome.metrics.unique_users,
            "effective_consumptions": (
                outcome.metrics.effective_consumptions
            ),
            "interactions": outcome.metrics.interactions,
            "complaints": outcome.metrics.complaints,
            "corrections": outcome.metrics.corrections,
            "recorded_at": outcome.recorded_at,
            "idempotency_key": outcome.idempotency_key,
            "content_sha256": outcome.content_sha256,
        }

    @staticmethod
    def to_case_domain(
        record: AnalysisFeedbackCaseRecord,
    ) -> AnalysisFeedbackCase:
        try:
            return AnalysisFeedbackCase(
                id=record.id,
                tenant_id=record.tenant_id,
                run_id=record.run_id,
                run_idempotency_key=record.run_idempotency_key,
                news_id=record.news_id,
                source_type=record.source_type,
                problem_type=record.problem_type,
                status=record.status,
                severity=record.severity,
                production_bundle_version=(
                    record.production_bundle_version
                ),
                analysis_input_snapshot=record.analysis_input_snapshot,
                analysis_output_snapshot=record.analysis_output_snapshot,
                source_reference=record.source_reference,
                occurred_at=record.occurred_at,
                recorded_at=record.recorded_at,
                idempotency_key=record.idempotency_key,
                content_sha256=record.content_sha256,
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise AnalysisFeedbackDataCorruptedError(
                "Feedback Case 不能通过领域 Schema 校验"
            ) from exc

    @staticmethod
    def to_label_domain(
        record: AnalysisFeedbackLabelRecord,
    ) -> AnalysisFeedbackLabel:
        try:
            return AnalysisFeedbackLabel(
                id=record.id,
                tenant_id=record.tenant_id,
                feedback_case_id=record.feedback_case_id,
                label_version=record.label_version,
                verdict=record.verdict,
                allowed_dominant_drivers=tuple(
                    record.allowed_dominant_drivers
                ),
                required_evidence_news_ids=tuple(
                    record.required_evidence_news_ids
                ),
                forbidden_evidence_news_ids=tuple(
                    record.forbidden_evidence_news_ids
                ),
                required_metric_keys=tuple(record.required_metric_keys),
                must_state_limitation=record.must_state_limitation,
                operator_comment=record.operator_comment,
                approval_status=record.approval_status,
                labeled_by=record.labeled_by,
                labeled_at=record.labeled_at,
                approved_by=record.approved_by,
                approved_at=record.approved_at,
                idempotency_key=record.idempotency_key,
                approval_idempotency_key=(
                    record.approval_idempotency_key
                ),
                recorded_at=record.recorded_at,
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise AnalysisFeedbackDataCorruptedError(
                "Feedback Label 不能通过领域 Schema 校验"
            ) from exc

    @staticmethod
    def to_outcome_domain(
        record: PublicationOutcomeRecord,
    ) -> PublicationOutcome:
        try:
            return PublicationOutcome(
                id=record.id,
                tenant_id=record.tenant_id,
                run_id=record.run_id,
                run_idempotency_key=record.run_idempotency_key,
                news_id=record.news_id,
                external_publication_id=record.external_publication_id,
                source_system=record.source_system,
                metric_definition_version=record.metric_definition_version,
                window_start=record.window_start,
                window_end=record.window_end,
                metrics=PublicationOutcomeMetrics(
                    impressions=record.impressions,
                    clicks=record.clicks,
                    unique_users=record.unique_users,
                    effective_consumptions=(
                        record.effective_consumptions
                    ),
                    interactions=record.interactions,
                    complaints=record.complaints,
                    corrections=record.corrections,
                ),
                recorded_at=record.recorded_at,
                idempotency_key=record.idempotency_key,
                content_sha256=record.content_sha256,
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise AnalysisFeedbackDataCorruptedError(
                "Publication Outcome 不能通过领域 Schema 校验"
            ) from exc
