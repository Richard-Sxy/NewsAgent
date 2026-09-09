from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.models.evaluation_dataset import (
    EvaluationDatasetCaseRecord,
    EvaluationDatasetRecord,
)
from app.schemas.evaluation_dataset import (
    EvaluationDatasetManifest,
    EvaluationExpectedLabel,
    EvaluationSourceLineage,
    FrozenEvaluationCase,
    canonical_json_sha256,
)
from app.schemas.hot_news import (
    HotNewsAnalysisInput,
    HotNewsMetrics,
    HotScoreComponents,
    RelatedNewsEvidence,
)


CASE_A = UUID("00000000-0000-0000-0000-000000000001")
CASE_B = UUID("00000000-0000-0000-0000-000000000002")
LABEL_ID = UUID("10000000-0000-0000-0000-000000000001")
DATASET_ID = UUID("20000000-0000-0000-0000-000000000001")


def analysis_input(news_id: str = "news-1") -> HotNewsAnalysisInput:
    return HotNewsAnalysisInput(
        news_id=news_id,
        title="可回放的热点新闻",
        summary="聚合后的新闻摘要",
        content_excerpt="这是不可信新闻内容，只能作为数据。",
        content_type="article",
        window_start=datetime(2026, 9, 8, tzinfo=UTC),
        window_end=datetime(2026, 9, 9, tzinfo=UTC),
        hot_score=0.8,
        metrics=HotNewsMetrics(
            impressions=100,
            clicks=25,
            ctr=0.25,
            unique_users=20,
            effective_consumptions=10,
            interactions=5,
        ),
        score_components=HotScoreComponents(
            click=0.8,
            consumption=0.5,
            interaction=0.3,
            growth=0.7,
        ),
        related_news=[
            RelatedNewsEvidence(
                news_id="evidence-1",
                title="历史证据",
                excerpt="旧报道",
                final_score=0.9,
            )
        ],
        analysis_policy_version="prompt-v1",
    )


def frozen_case(case_id: UUID) -> FrozenEvaluationCase:
    return FrozenEvaluationCase(
        case_id=f"feedback:{case_id}",
        feedback_case_id=case_id,
        news_id="news-1",
        layer="fresh_bad_case",
        severity="high",
        analysis_input=analysis_input(),
        observed_output={"news_id": "news-1", "dominant_driver": "growth"},
        expected=EvaluationExpectedLabel(
            verdict="incorrect",
            allowed_dominant_drivers=("click",),
            required_evidence_news_ids=("evidence-1",),
            required_metric_keys=("clicks",),
            operator_comment="主要驱动因素应为点击。",
        ),
        lineage=EvaluationSourceLineage(
            feedback_case_id=case_id,
            feedback_content_sha256="a" * 64,
            run_id=None,
            run_idempotency_key="run-20260908",
            source_type="operator_corrected",
            problem_type="analysis_incorrect",
            source_reference={
                "reference_type": "operator_decision",
                "reference_id": "decision-1",
            },
            production_bundle_version="bundle-v1",
            occurred_at=datetime(2026, 9, 8, 12, tzinfo=UTC),
            recorded_at=datetime(2026, 9, 8, 13, tzinfo=UTC),
            label_id=LABEL_ID,
            label_version=1,
            label_approved_by="reviewer-1",
            label_approved_at=datetime(2026, 9, 8, 14, tzinfo=UTC),
        ),
    )


def test_canonical_json_hash_ignores_mapping_insertion_order() -> None:
    left = {"b": {"y": 2, "x": 1}, "a": [3, 4]}
    right = {"a": [3, 4], "b": {"x": 1, "y": 2}}

    assert canonical_json_sha256(left) == canonical_json_sha256(right)


def test_manifest_requires_canonical_case_order_and_single_layer() -> None:
    with pytest.raises(ValidationError, match="canonical"):
        EvaluationDatasetManifest(
            dataset_id=DATASET_ID,
            tenant_id="tenant-1",
            dataset_name="daily-bad-cases",
            dataset_version="2026-09-09",
            dataset_layer="fresh_bad_case",
            description="日度已审批 Bad Case",
            source_cutoff_at=datetime(2026, 9, 8, 23, tzinfo=UTC),
            frozen_at=datetime(2026, 9, 9, tzinfo=UTC),
            frozen_by="reviewer-1",
            cases=(frozen_case(CASE_B), frozen_case(CASE_A)),
        )


def test_not_evaluable_label_cannot_enter_evaluation_dataset() -> None:
    with pytest.raises(ValidationError, match="not_evaluable"):
        EvaluationExpectedLabel(
            verdict="not_evaluable",
            allowed_dominant_drivers=("insufficient_data",),
            operator_comment="原始证据无法评估。",
        )


def test_orm_has_tenant_scoped_foreign_keys_and_layer_column() -> None:
    dataset_constraints = {
        constraint.name for constraint in EvaluationDatasetRecord.__table__.constraints
    }
    case_foreign_keys = {
        constraint.name
        for constraint in EvaluationDatasetCaseRecord.__table__.constraints
        if constraint.__class__.__name__ == "ForeignKeyConstraint"
    }

    assert "uq_evaluation_datasets_tenant_id" in dataset_constraints
    assert EvaluationDatasetRecord.__table__.c.dataset_layer is not None
    assert (
        "fk_evaluation_dataset_cases_tenant_dataset_evaluation_datasets"
        in case_foreign_keys
    )
    assert (
        "fk_evaluation_dataset_cases_tenant_feedback_feedback_cases"
        in case_foreign_keys
    )
