from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.schemas.analysis_feedback import (
    AnalysisFeedbackLabel,
    CollectAnalysisFeedbackCommand,
    FeedbackSourceReference,
    PublicationOutcomeMetrics,
    RecordPublicationOutcomeCommand,
    SubmitAnalysisFeedbackLabelCommand,
)
from app.schemas.hot_news import (
    HotNewsAnalysisInput,
    HotNewsAnalysisReport,
    HotNewsMetrics,
    HotScoreComponents,
)


NOW = datetime(2026, 9, 9, 10, tzinfo=timezone.utc)


def analysis_input(news_id: str = "news-1") -> HotNewsAnalysisInput:
    return HotNewsAnalysisInput(
        news_id=news_id,
        title="测试新闻",
        summary="摘要",
        content_excerpt="正文片段",
        content_type="article",
        window_start=NOW - timedelta(hours=1),
        window_end=NOW,
        hot_score=0.8,
        metrics=HotNewsMetrics(
            impressions=100,
            clicks=20,
            ctr=0.2,
            unique_users=18,
            effective_consumptions=12,
            interactions=4,
        ),
        score_components=HotScoreComponents(
            click=0.8,
            consumption=0.6,
            interaction=0.3,
            growth=0.9,
        ),
        analysis_policy_version="analysis-v1",
    )


def analysis_report(news_id: str = "news-1") -> HotNewsAnalysisReport:
    return HotNewsAnalysisReport(
        news_id=news_id,
        trend_assessment="热度上升",
        dominant_driver="growth",
        attention_reasons=[],
        related_contexts=[],
        operation_suggestions=[],
        evidence_news_ids=[],
        applied_memory_ids=[],
        limitations=["无关联新闻证据"],
        overall_confidence=0.55,
    )


def failed_case_values() -> dict:
    return {
        "run_id": None,
        "run_idempotency_key": "hot-news-run-1",
        "news_id": "news-1",
        "source_type": "validation_failed",
        "problem_type": "schema_violation",
        "severity": "high",
        "production_bundle_version": "bundle-v1",
        "analysis_input_snapshot": analysis_input(),
        "analysis_output_snapshot": None,
        "source_reference": FeedbackSourceReference(
            reference_type="validation_attempt",
            reference_id="attempt-1",
            diagnostic_code="schema_invalid",
        ),
        "occurred_at": NOW,
        "idempotency_key": "feedback-case-1",
    }


def test_failed_analysis_case_supports_missing_run_and_output() -> None:
    command = CollectAnalysisFeedbackCommand(**failed_case_values())

    assert command.run_id is None
    assert command.analysis_output_snapshot is None


def test_feedback_case_rejects_raw_behavior_details_and_unknown_fields() -> None:
    values = failed_case_values()
    values["raw_behavior_records"] = [
        {"user_id": "sensitive-user", "event": "click"}
    ]

    with pytest.raises(ValidationError, match="raw_behavior_records"):
        CollectAnalysisFeedbackCommand(**values)


def test_feedback_case_rejects_snapshot_identity_mismatch() -> None:
    values = failed_case_values()
    values["analysis_input_snapshot"] = analysis_input("news-other")

    with pytest.raises(ValidationError, match="must match news_id"):
        CollectAnalysisFeedbackCommand(**values)


def test_operator_feedback_requires_run_output_and_decision_reference() -> None:
    values = failed_case_values()
    values.update(
        source_type="operator_corrected",
        source_reference=FeedbackSourceReference(
            reference_type="analysis_run",
            reference_id="run-1",
        ),
    )

    with pytest.raises(ValidationError, match="requires run_id"):
        CollectAnalysisFeedbackCommand(**values)


def test_label_is_strongly_typed_and_rejects_arbitrary_correction_dict() -> None:
    with pytest.raises(ValidationError, match="correction_payload"):
        SubmitAnalysisFeedbackLabelCommand(
            feedback_case_id=UUID(int=1),
            verdict="incorrect",
            operator_comment="主要驱动因素标注错误",
            labeled_by="operator-1",
            labeled_at=NOW,
            idempotency_key="feedback-label-1",
            correction_payload={"dominant_driver": "click"},
        )


def test_label_rejects_duplicate_or_overlapping_evidence_ids() -> None:
    with pytest.raises(ValidationError, match="cannot overlap"):
        SubmitAnalysisFeedbackLabelCommand(
            feedback_case_id=UUID(int=1),
            verdict="incorrect",
            required_evidence_news_ids=("evidence-1",),
            forbidden_evidence_news_ids=("evidence-1",),
            operator_comment="证据引用错误",
            labeled_by="operator-1",
            labeled_at=NOW,
            idempotency_key="feedback-label-1",
        )


def test_approved_label_requires_complete_human_approval_metadata() -> None:
    with pytest.raises(ValidationError, match="approval metadata"):
        AnalysisFeedbackLabel(
            id=UUID(int=2),
            tenant_id="tenant-1",
            feedback_case_id=UUID(int=1),
            label_version=1,
            verdict="incorrect",
            allowed_dominant_drivers=("click",),
            operator_comment="分析有误",
            approval_status="approved",
            labeled_by="operator-1",
            labeled_at=NOW,
            idempotency_key="feedback-label-1",
            recorded_at=NOW,
        )


def test_approved_label_requires_an_independent_reviewer() -> None:
    with pytest.raises(ValidationError, match="cannot approve their own"):
        AnalysisFeedbackLabel(
            id=UUID(int=2),
            tenant_id="tenant-1",
            feedback_case_id=UUID(int=1),
            label_version=1,
            verdict="incorrect",
            allowed_dominant_drivers=("click",),
            operator_comment="分析有误",
            approval_status="approved",
            labeled_by="operator-1",
            labeled_at=NOW,
            approved_by="operator-1",
            approved_at=NOW,
            approval_idempotency_key="feedback-label-approve-1",
            reviewed_by="operator-1",
            reviewed_at=NOW,
            review_reason="同意标签",
            review_idempotency_key="feedback-label-approve-1",
            idempotency_key="feedback-label-1",
            recorded_at=NOW,
        )


def test_rejected_label_requires_auditable_independent_review() -> None:
    label = AnalysisFeedbackLabel(
        id=UUID(int=2),
        tenant_id="tenant-1",
        feedback_case_id=UUID(int=1),
        label_version=1,
        verdict="incorrect",
        allowed_dominant_drivers=("click",),
        operator_comment="分析有误",
        approval_status="rejected",
        labeled_by="operator-1",
        labeled_at=NOW,
        reviewed_by="reviewer-1",
        reviewed_at=NOW,
        review_reason="证据编号与输入快照不一致，请修订",
        review_idempotency_key="feedback-label-review-1",
        idempotency_key="feedback-label-1",
        recorded_at=NOW,
    )

    assert label.approval_status == "rejected"
    assert label.reviewed_by == "reviewer-1"


def test_publication_outcome_only_accepts_aggregate_metrics() -> None:
    command = RecordPublicationOutcomeCommand(
        run_id=UUID(int=3),
        run_idempotency_key="hot-news-run-1",
        news_id="news-1",
        external_publication_id="publication-1",
        source_system="cms-analytics",
        metric_definition_version="metrics-v1",
        window_start=NOW - timedelta(hours=1),
        window_end=NOW,
        metrics=PublicationOutcomeMetrics(
            impressions=1000,
            clicks=120,
            unique_users=100,
            effective_consumptions=80,
            interactions=12,
            complaints=1,
            corrections=0,
        ),
        idempotency_key="publication-outcome-1",
    )
    assert command.metrics.clicks == 120

    values = command.model_dump()
    values["metrics"]["user_ids"] = ["sensitive-user"]
    with pytest.raises(ValidationError, match="user_ids"):
        RecordPublicationOutcomeCommand(**values)
