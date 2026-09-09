"""评测数据集 PostgreSQL Repository 与领域 Port。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analysis_feedback import (
    AnalysisFeedbackCaseRecord,
    AnalysisFeedbackLabelRecord,
)
from app.models.evaluation_dataset import (
    EvaluationDatasetCaseRecord,
    EvaluationDatasetRecord,
)
from app.schemas.evaluation_dataset import (
    ApprovedFeedbackSnapshot,
    EvaluationDatasetArtifactReceipt,
    EvaluationDatasetCaseIndex,
    EvaluationDatasetManifest,
    EvaluationDatasetReference,
    EvaluationExpectedLabel,
    EvaluationSourceLineage,
)


class EvaluationDatasetRepositoryError(RuntimeError):
    """评测数据集持久化失败。"""

    retryable = True


class EvaluationDatasetConflictError(ValueError):
    """数据集版本或幂等键已被不同业务请求占用。"""


class FeedbackSnapshotCorruptedError(RuntimeError):
    """已审批反馈无法转换为严格的冻结快照。"""

    retryable = False


class EvaluationDatasetRepository(Protocol):
    async def get_by_idempotency_key(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
    ) -> EvaluationDatasetReference | None: ...

    async def get_by_id(
        self,
        *,
        tenant_id: str,
        dataset_id: UUID,
    ) -> EvaluationDatasetReference | None: ...

    async def get_by_name_version(
        self,
        *,
        tenant_id: str,
        dataset_name: str,
        dataset_version: str,
    ) -> EvaluationDatasetReference | None: ...

    async def list_case_indexes(
        self,
        *,
        tenant_id: str,
        dataset_id: UUID,
    ) -> tuple[EvaluationDatasetCaseIndex, ...]: ...

    async def list_approved_feedback_snapshots(
        self,
        *,
        tenant_id: str,
        feedback_case_ids: Sequence[UUID],
        source_cutoff_at: datetime,
    ) -> tuple[ApprovedFeedbackSnapshot, ...]: ...

    async def save_frozen_dataset(
        self,
        *,
        manifest: EvaluationDatasetManifest,
        artifact: EvaluationDatasetArtifactReceipt,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> tuple[EvaluationDatasetReference, bool]: ...


class PostgresEvaluationDatasetRepository:
    """使用调用方提供的同一 AsyncSession 完成原子索引写入。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_idempotency_key(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
    ) -> EvaluationDatasetReference | None:
        try:
            result = await self._session.execute(
                select(EvaluationDatasetRecord).where(
                    EvaluationDatasetRecord.tenant_id == tenant_id,
                    EvaluationDatasetRecord.idempotency_key == idempotency_key,
                )
            )
        except SQLAlchemyError as exc:
            raise EvaluationDatasetRepositoryError(
                "failed to read evaluation dataset by idempotency key"
            ) from exc
        record = result.scalar_one_or_none()
        return None if record is None else self._to_reference(record)

    async def get_by_id(
        self,
        *,
        tenant_id: str,
        dataset_id: UUID,
    ) -> EvaluationDatasetReference | None:
        try:
            result = await self._session.execute(
                select(EvaluationDatasetRecord).where(
                    EvaluationDatasetRecord.tenant_id == tenant_id,
                    EvaluationDatasetRecord.id == dataset_id,
                )
            )
        except SQLAlchemyError as exc:
            raise EvaluationDatasetRepositoryError(
                "failed to read evaluation dataset"
            ) from exc
        record = result.scalar_one_or_none()
        return None if record is None else self._to_reference(record)

    async def get_by_name_version(
        self,
        *,
        tenant_id: str,
        dataset_name: str,
        dataset_version: str,
    ) -> EvaluationDatasetReference | None:
        try:
            result = await self._session.execute(
                select(EvaluationDatasetRecord).where(
                    EvaluationDatasetRecord.tenant_id == tenant_id,
                    EvaluationDatasetRecord.dataset_name == dataset_name,
                    EvaluationDatasetRecord.dataset_version == dataset_version,
                )
            )
        except SQLAlchemyError as exc:
            raise EvaluationDatasetRepositoryError(
                "failed to read evaluation dataset version"
            ) from exc
        record = result.scalar_one_or_none()
        return None if record is None else self._to_reference(record)

    async def list_case_indexes(
        self,
        *,
        tenant_id: str,
        dataset_id: UUID,
    ) -> tuple[EvaluationDatasetCaseIndex, ...]:
        try:
            result = await self._session.execute(
                select(EvaluationDatasetCaseRecord)
                .where(
                    EvaluationDatasetCaseRecord.tenant_id == tenant_id,
                    EvaluationDatasetCaseRecord.dataset_id == dataset_id,
                )
                .order_by(EvaluationDatasetCaseRecord.position)
            )
        except SQLAlchemyError as exc:
            raise EvaluationDatasetRepositoryError(
                "failed to read evaluation dataset case indexes"
            ) from exc
        return tuple(
            EvaluationDatasetCaseIndex(
                dataset_id=record.dataset_id,
                tenant_id=record.tenant_id,
                feedback_case_id=record.feedback_case_id,
                position=record.position,
                case_id=record.case_id,
                news_id=record.news_id,
                dataset_layer=record.dataset_layer,
                severity=record.severity,
                label_version=record.label_version,
                case_content_sha256=record.case_content_sha256,
            )
            for record in result.scalars().all()
        )

    async def list_approved_feedback_snapshots(
        self,
        *,
        tenant_id: str,
        feedback_case_ids: Sequence[UUID],
        source_cutoff_at: datetime,
    ) -> tuple[ApprovedFeedbackSnapshot, ...]:
        requested_ids = tuple(dict.fromkeys(feedback_case_ids))
        if not requested_ids:
            return ()

        latest_approved_version = (
            select(func.max(AnalysisFeedbackLabelRecord.label_version))
            .where(
                AnalysisFeedbackLabelRecord.tenant_id
                == AnalysisFeedbackCaseRecord.tenant_id,
                AnalysisFeedbackLabelRecord.feedback_case_id
                == AnalysisFeedbackCaseRecord.id,
                AnalysisFeedbackLabelRecord.approval_status == "approved",
            )
            .correlate(AnalysisFeedbackCaseRecord)
            .scalar_subquery()
        )
        statement = (
            select(
                AnalysisFeedbackCaseRecord,
                AnalysisFeedbackLabelRecord,
            )
            .join(
                AnalysisFeedbackLabelRecord,
                (
                    AnalysisFeedbackLabelRecord.tenant_id
                    == AnalysisFeedbackCaseRecord.tenant_id
                )
                & (
                    AnalysisFeedbackLabelRecord.feedback_case_id
                    == AnalysisFeedbackCaseRecord.id
                ),
            )
            .where(
                AnalysisFeedbackCaseRecord.tenant_id == tenant_id,
                AnalysisFeedbackCaseRecord.id.in_(requested_ids),
                AnalysisFeedbackCaseRecord.status.in_(("labeled", "frozen")),
                AnalysisFeedbackCaseRecord.recorded_at <= source_cutoff_at,
                AnalysisFeedbackLabelRecord.approval_status == "approved",
                AnalysisFeedbackLabelRecord.label_version
                == latest_approved_version,
            )
            .order_by(AnalysisFeedbackCaseRecord.id)
            .with_for_update()
        )
        try:
            result = await self._session.execute(statement)
        except SQLAlchemyError as exc:
            raise EvaluationDatasetRepositoryError(
                "failed to read approved feedback snapshots"
            ) from exc

        snapshots: list[ApprovedFeedbackSnapshot] = []
        for case, label in result.all():
            try:
                snapshots.append(self._to_approved_snapshot(case, label))
            except (TypeError, ValueError) as exc:
                raise FeedbackSnapshotCorruptedError(
                    f"feedback case {case.id} is not freezeable: {exc}"
                ) from exc
        return tuple(snapshots)

    async def save_frozen_dataset(
        self,
        *,
        manifest: EvaluationDatasetManifest,
        artifact: EvaluationDatasetArtifactReceipt,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> tuple[EvaluationDatasetReference, bool]:
        try:
            return await self._save_frozen_dataset(
                manifest=manifest,
                artifact=artifact,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
        except (EvaluationDatasetConflictError, EvaluationDatasetRepositoryError):
            raise
        except SQLAlchemyError as exc:
            raise EvaluationDatasetRepositoryError(
                "failed to persist frozen evaluation dataset"
            ) from exc

    async def _save_frozen_dataset(
        self,
        *,
        manifest: EvaluationDatasetManifest,
        artifact: EvaluationDatasetArtifactReceipt,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> tuple[EvaluationDatasetReference, bool]:
        if artifact.content_sha256 != manifest.content_sha256:
            raise EvaluationDatasetRepositoryError(
                "artifact checksum does not match dataset manifest"
            )
        values = {
            "id": manifest.dataset_id,
            "tenant_id": manifest.tenant_id,
            "dataset_name": manifest.dataset_name,
            "dataset_version": manifest.dataset_version,
            "dataset_layer": manifest.dataset_layer,
            "status": manifest.status,
            "schema_version": manifest.schema_version,
            "description": manifest.description,
            "source_cutoff_at": manifest.source_cutoff_at,
            "frozen_at": manifest.frozen_at,
            "frozen_by": manifest.frozen_by,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
            "content_sha256": manifest.content_sha256,
            "artifact_uri": artifact.storage_uri,
            "artifact_size": artifact.content_size,
            "case_count": len(manifest.cases),
            "layer_counts": {manifest.dataset_layer: len(manifest.cases)},
        }
        statement = (
            insert(EvaluationDatasetRecord)
            .values(**values)
            # 同时覆盖幂等键与 name/version 唯一约束，之后
            # 通过查询区分“幂等重放”和“版本被占用”。
            .on_conflict_do_nothing()
            .returning(EvaluationDatasetRecord)
        )
        result = await self._session.execute(statement)
        inserted = result.scalar_one_or_none()

        if inserted is None:
            existing = await self.get_by_idempotency_key(
                tenant_id=manifest.tenant_id,
                idempotency_key=idempotency_key,
            )
            if existing is None:
                version_result = await self._session.execute(
                    select(EvaluationDatasetRecord).where(
                        EvaluationDatasetRecord.tenant_id == manifest.tenant_id,
                        EvaluationDatasetRecord.dataset_name
                        == manifest.dataset_name,
                        EvaluationDatasetRecord.dataset_version
                        == manifest.dataset_version,
                    )
                )
                occupied = version_result.scalar_one_or_none()
                if occupied is not None:
                    raise EvaluationDatasetConflictError(
                        "dataset name/version is already frozen"
                    )
                raise EvaluationDatasetRepositoryError(
                    "dataset insert conflicted but no existing row was found"
                )
            if existing.request_fingerprint != request_fingerprint:
                raise EvaluationDatasetConflictError(
                    "idempotency key was reused with different freeze content"
                )
            return existing, False

        case_rows = [
            {
                "id": uuid4(),
                "tenant_id": manifest.tenant_id,
                "dataset_id": manifest.dataset_id,
                "feedback_case_id": case.feedback_case_id,
                "position": position,
                "case_id": case.case_id,
                "news_id": case.news_id,
                "dataset_layer": case.layer,
                "severity": case.severity,
                "label_version": case.lineage.label_version,
                "analysis_input_snapshot": case.analysis_input.model_dump(
                    mode="json"
                ),
                "analysis_output_snapshot": case.observed_output,
                "expected_label": case.expected.model_dump(mode="json"),
                "source_lineage": case.lineage.model_dump(mode="json"),
                "case_content_sha256": case.content_sha256,
                "created_at": manifest.frozen_at,
            }
            for position, case in enumerate(manifest.cases)
        ]
        await self._session.execute(
            insert(EvaluationDatasetCaseRecord),
            case_rows,
        )
        await self._session.execute(
            update(AnalysisFeedbackCaseRecord)
            .where(
                AnalysisFeedbackCaseRecord.tenant_id == manifest.tenant_id,
                AnalysisFeedbackCaseRecord.id.in_(
                    [case.feedback_case_id for case in manifest.cases]
                ),
                AnalysisFeedbackCaseRecord.status == "labeled",
            )
            .values(status="frozen")
        )
        return self._to_reference(inserted), True

    @staticmethod
    def _to_approved_snapshot(
        case: AnalysisFeedbackCaseRecord,
        label: AnalysisFeedbackLabelRecord,
    ) -> ApprovedFeedbackSnapshot:
        if label.approved_at is None or label.approved_by is None:
            raise ValueError("approved label is missing approval metadata")
        allowed_drivers = tuple(label.allowed_dominant_drivers)
        if (
            not allowed_drivers
            and label.verdict == "correct"
            and case.analysis_output_snapshot is not None
        ):
            observed_driver = case.analysis_output_snapshot.get(
                "dominant_driver"
            )
            if isinstance(observed_driver, str) and observed_driver:
                # “correct”是人工判断；此时可将已确认的观测
                # 结果转为最小期望标签，不是让模型自标注。
                allowed_drivers = (observed_driver,)
        return ApprovedFeedbackSnapshot(
            tenant_id=case.tenant_id,
            feedback_case_id=case.id,
            news_id=case.news_id,
            severity=case.severity,
            analysis_input_snapshot=case.analysis_input_snapshot,
            analysis_output_snapshot=case.analysis_output_snapshot,
            expected=EvaluationExpectedLabel(
                verdict=label.verdict,
                allowed_dominant_drivers=allowed_drivers,
                required_evidence_news_ids=tuple(
                    label.required_evidence_news_ids
                ),
                forbidden_evidence_news_ids=tuple(
                    label.forbidden_evidence_news_ids
                ),
                required_metric_keys=tuple(label.required_metric_keys),
                must_state_limitation=label.must_state_limitation,
                operator_comment=label.operator_comment,
            ),
            lineage=EvaluationSourceLineage(
                feedback_case_id=case.id,
                feedback_content_sha256=case.content_sha256,
                run_id=case.run_id,
                run_idempotency_key=case.run_idempotency_key,
                source_type=case.source_type,
                problem_type=case.problem_type,
                source_reference=case.source_reference,
                production_bundle_version=case.production_bundle_version,
                occurred_at=case.occurred_at,
                recorded_at=case.recorded_at,
                label_id=label.id,
                label_version=label.label_version,
                label_approved_by=label.approved_by,
                label_approved_at=label.approved_at,
            ),
        )

    @staticmethod
    def _to_reference(record: EvaluationDatasetRecord) -> EvaluationDatasetReference:
        return EvaluationDatasetReference(
            dataset_id=record.id,
            tenant_id=record.tenant_id,
            dataset_name=record.dataset_name,
            dataset_version=record.dataset_version,
            dataset_layer=record.dataset_layer,
            status=record.status,
            schema_version=record.schema_version,
            description=record.description,
            source_cutoff_at=record.source_cutoff_at,
            frozen_at=record.frozen_at,
            frozen_by=record.frozen_by,
            idempotency_key=record.idempotency_key,
            request_fingerprint=record.request_fingerprint,
            content_sha256=record.content_sha256,
            artifact_uri=record.artifact_uri,
            artifact_size=record.artifact_size,
            case_count=record.case_count,
        )
