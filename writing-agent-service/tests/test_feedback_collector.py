from datetime import timedelta
from unittest.mock import create_autospec
from uuid import UUID

import pytest

import app.services.data_loop.feedback_collector as collector_module
from app.models.hot_news_decision import HotNewsDecision
from app.repositories.analysis_feedback import (
    PostgresAnalysisFeedbackRepository,
)
from app.schemas.analysis_feedback import (
    AnalysisFeedbackCase,
    AnalysisFeedbackLabel,
    ApproveAnalysisFeedbackLabelCommand,
    CollectAnalysisFeedbackCommand,
    PublicationOutcomeMetrics,
    RecordPublicationOutcomeCommand,
    SubmitAnalysisFeedbackLabelCommand,
)
from app.schemas.hot_news_memory import HotNewsAnalysisMemory
from app.services.data_loop.feedback_collector import (
    AnalysisFeedbackCollector,
    AnalysisFeedbackConflictError,
    AnalysisFeedbackTargetNotFoundError,
    build_feedback_content_sha256,
)
from tests.test_analysis_feedback_schema import (
    NOW,
    analysis_input,
    analysis_report,
    failed_case_values,
)


def repository_mock(monkeypatch):
    repository = create_autospec(
        PostgresAnalysisFeedbackRepository,
        instance=True,
    )
    monkeypatch.setattr(
        collector_module,
        "PostgresAnalysisFeedbackRepository",
        lambda session: repository,
    )
    return repository


def case_command(**overrides) -> CollectAnalysisFeedbackCommand:
    values = failed_case_values()
    values.update(overrides)
    return CollectAnalysisFeedbackCommand(**values)


def stored_case(
    command: CollectAnalysisFeedbackCommand,
    *,
    content_sha256: str | None = None,
    status: str = "needs_label",
) -> AnalysisFeedbackCase:
    return AnalysisFeedbackCase(
        id=UUID(int=1),
        tenant_id="tenant-1",
        status=status,
        recorded_at=NOW,
        content_sha256=(
            content_sha256
            or build_feedback_content_sha256(
                command, tenant_id="tenant-1"
            )
        ),
        **command.model_dump(),
    )


def memory() -> HotNewsAnalysisMemory:
    return HotNewsAnalysisMemory(
        run_id=UUID(int=10),
        tenant_id="tenant-1",
        news_id="news-1",
        rank=1,
        production_bundle_version="bundle-v1",
        workflow_version="workflow-v1",
        payload_schema_version="2.0",
        analysis_input=analysis_input(),
        analysis_report=analysis_report(),
        fastgpt_request_id="fastgpt-request-1",
        usage={},
        captured_at=NOW - timedelta(minutes=5),
        validated_at=NOW - timedelta(minutes=4),
        completed_at=NOW - timedelta(minutes=3),
    )


def pending_label() -> AnalysisFeedbackLabel:
    return AnalysisFeedbackLabel(
        id=UUID(int=20),
        tenant_id="tenant-1",
        feedback_case_id=UUID(int=1),
        label_version=1,
        verdict="incorrect",
        allowed_dominant_drivers=("click",),
        required_metric_keys=("clicks",),
        operator_comment="应当以点击指标为主",
        approval_status="pending",
        labeled_by="operator-1",
        labeled_at=NOW,
        idempotency_key="feedback-label-1",
        recorded_at=NOW,
    )


@pytest.mark.asyncio
async def test_collect_creates_typed_case_and_stable_hash(monkeypatch) -> None:
    repository = repository_mock(monkeypatch)
    repository.get_case_by_idempotency_key.return_value = None
    repository.insert_case.return_value = True
    command = case_command()

    outcome = await AnalysisFeedbackCollector().collect(
        object(),
        tenant_id=" tenant-1 ",
        command=command,
        recorded_at=NOW,
        feedback_case_id=UUID(int=1),
    )

    assert outcome.created is True
    assert outcome.case.status == "needs_label"
    assert outcome.case.run_id is None
    assert outcome.case.content_sha256 == build_feedback_content_sha256(
        command, tenant_id="tenant-1"
    )
    repository.insert_case.assert_awaited_once_with(case=outcome.case)


@pytest.mark.asyncio
async def test_collect_replays_same_request_and_rejects_changed_content(
    monkeypatch,
) -> None:
    repository = repository_mock(monkeypatch)
    command = case_command()
    existing = stored_case(command)
    repository.get_case_by_idempotency_key.return_value = existing
    collector = AnalysisFeedbackCollector()

    replay = await collector.collect(
        object(),
        tenant_id="tenant-1",
        command=command,
        recorded_at=NOW,
    )
    assert replay.created is False
    assert replay.case is existing

    repository.get_case_by_idempotency_key.return_value = existing.model_copy(
        update={"content_sha256": "b" * 64}
    )
    with pytest.raises(AnalysisFeedbackConflictError, match="idempotency_key"):
        await collector.collect(
            object(),
            tenant_id="tenant-1",
            command=command,
            recorded_at=NOW,
        )


@pytest.mark.asyncio
async def test_operator_decision_builds_case_without_legacy_correction_payload(
    monkeypatch,
) -> None:
    repository = repository_mock(monkeypatch)
    repository.get_case_by_idempotency_key.return_value = None
    repository.insert_case.return_value = True
    analysis_memory = memory()
    decision = HotNewsDecision(
        id=UUID(int=30),
        tenant_id="tenant-1",
        run_id=analysis_memory.run_id,
        news_id=analysis_memory.news_id,
        decision_type="corrected",
        reason="主要驱动因素不准确",
        correction_payload={"raw_untrusted_shape": {"anything": True}},
        operator_id="operator-1",
        idempotency_key="decision-request-1",
    )

    outcome = await AnalysisFeedbackCollector().collect_from_operator_decision(
        object(),
        tenant_id="tenant-1",
        memory=analysis_memory,
        run_idempotency_key="hot-news-run-1",
        decision=decision,
        problem_type="analysis_incorrect",
        severity="high",
        occurred_at=NOW,
        recorded_at=NOW,
        feedback_case_id=UUID(int=1),
    )

    assert outcome.case.source_type == "operator_corrected"
    assert outcome.case.source_reference.reference_id == str(decision.id)
    assert not hasattr(outcome.case, "correction_payload")
    serialized = outcome.case.model_dump(mode="json")
    assert "raw_untrusted_shape" not in str(serialized)


@pytest.mark.asyncio
async def test_operator_decision_is_tenant_scoped(monkeypatch) -> None:
    repository = repository_mock(monkeypatch)
    analysis_memory = memory()
    decision = HotNewsDecision(
        id=UUID(int=30),
        tenant_id="tenant-other",
        run_id=analysis_memory.run_id,
        news_id=analysis_memory.news_id,
        decision_type="rejected",
        reason="分析不可用",
        correction_payload={},
        operator_id="operator-1",
        idempotency_key="decision-request-1",
    )

    with pytest.raises(AnalysisFeedbackTargetNotFoundError):
        await AnalysisFeedbackCollector().collect_from_operator_decision(
            object(),
            tenant_id="tenant-1",
            memory=analysis_memory,
            run_idempotency_key="hot-news-run-1",
            decision=decision,
            problem_type="analysis_incorrect",
            severity="high",
            occurred_at=NOW,
            recorded_at=NOW,
        )
    repository.insert_case.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_and_approve_label_use_version_and_human_gate(
    monkeypatch,
) -> None:
    repository = repository_mock(monkeypatch)
    command = case_command()
    feedback_case = stored_case(command)
    repository.get_label_by_idempotency_key.return_value = None
    repository.get_case_for_update.return_value = feedback_case
    repository.get_latest_label_for_update.return_value = None
    repository.insert_label.return_value = True
    repository.update_case_status.return_value = True
    collector = AnalysisFeedbackCollector()
    submit = SubmitAnalysisFeedbackLabelCommand(
        feedback_case_id=feedback_case.id,
        verdict="incorrect",
        allowed_dominant_drivers=("click",),
        required_metric_keys=("clicks",),
        operator_comment="应当以点击指标为主",
        labeled_by="operator-1",
        labeled_at=NOW,
        idempotency_key="feedback-label-1",
    )

    submitted = await collector.submit_label(
        object(),
        tenant_id="tenant-1",
        command=submit,
        recorded_at=NOW,
        label_id=UUID(int=20),
    )
    assert submitted.label.approval_status == "pending"
    assert submitted.label.label_version == 1

    repository.get_label_by_approval_idempotency_key.return_value = None
    repository.get_label_for_update.return_value = submitted.label
    repository.get_latest_label_for_update.return_value = submitted.label
    repository.approve_label.return_value = True
    approval = ApproveAnalysisFeedbackLabelCommand(
        feedback_case_id=feedback_case.id,
        label_id=submitted.label.id,
        expected_label_version=1,
        approved_by="reviewer-1",
        approved_at=NOW + timedelta(minutes=1),
        idempotency_key="label-approval-1",
    )

    approved = await collector.approve_label(
        object(),
        tenant_id="tenant-1",
        command=approval,
    )
    assert approved.label.approval_status == "approved"
    assert approved.label.approved_by == "reviewer-1"
    repository.update_case_status.assert_awaited_with(
        tenant_id="tenant-1",
        feedback_case_id=feedback_case.id,
        expected_statuses=("collected", "needs_label"),
        status="labeled",
    )


@pytest.mark.asyncio
async def test_record_publication_outcome_is_idempotent_and_aggregate_only(
    monkeypatch,
) -> None:
    repository = repository_mock(monkeypatch)
    repository.get_publication_outcome_by_idempotency_key.return_value = None
    repository.insert_publication_outcome.return_value = True
    command = RecordPublicationOutcomeCommand(
        run_id=UUID(int=10),
        run_idempotency_key="hot-news-run-1",
        news_id="news-1",
        external_publication_id="publication-1",
        source_system="cms-analytics",
        metric_definition_version="metrics-v1",
        window_start=NOW - timedelta(hours=1),
        window_end=NOW,
        metrics=PublicationOutcomeMetrics(
            impressions=500,
            clicks=20,
            unique_users=18,
            effective_consumptions=12,
            interactions=4,
            complaints=1,
        ),
        idempotency_key="publication-outcome-1",
    )

    result = await AnalysisFeedbackCollector().record_publication_outcome(
        object(),
        tenant_id="tenant-1",
        command=command,
        recorded_at=NOW,
        outcome_id=UUID(int=40),
    )

    assert result.created is True
    assert result.outcome.metrics.impressions == 500
    assert not hasattr(result.outcome.metrics, "user_ids")


@pytest.mark.asyncio
async def test_publication_outcome_hides_cross_tenant_run(monkeypatch) -> None:
    repository = repository_mock(monkeypatch)
    repository.analysis_run_contains_news.return_value = False
    command = RecordPublicationOutcomeCommand(
        run_id=UUID(int=10),
        run_idempotency_key="hot-news-run-1",
        news_id="news-1",
        external_publication_id="publication-1",
        source_system="cms-analytics",
        metric_definition_version="metrics-v1",
        window_start=NOW - timedelta(hours=1),
        window_end=NOW,
        metrics=PublicationOutcomeMetrics(
            impressions=500,
            clicks=20,
            unique_users=18,
            effective_consumptions=12,
            interactions=4,
        ),
        idempotency_key="publication-outcome-1",
    )

    with pytest.raises(AnalysisFeedbackTargetNotFoundError):
        await AnalysisFeedbackCollector().record_publication_outcome(
            object(),
            tenant_id="tenant-1",
            command=command,
            recorded_at=NOW,
        )

    repository.analysis_run_contains_news.assert_awaited_once_with(
        tenant_id="tenant-1",
        run_id=command.run_id,
        run_idempotency_key=command.run_idempotency_key,
        news_id=command.news_id,
    )
    repository.insert_publication_outcome.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_methods_forward_tenant_scope(monkeypatch) -> None:
    repository = repository_mock(monkeypatch)
    command = case_command()
    feedback_case = stored_case(command)
    repository.list_cases.return_value = [feedback_case]
    repository.get_case.return_value = feedback_case
    repository.list_labels.return_value = [pending_label()]
    collector = AnalysisFeedbackCollector()

    cases = await collector.list_cases(
        object(), tenant_id="tenant-1", statuses=("needs_label",)
    )
    labels = await collector.list_labels(
        object(), tenant_id="tenant-1", feedback_case_id=feedback_case.id
    )

    assert cases == [feedback_case]
    assert labels[0].feedback_case_id == feedback_case.id
    repository.list_cases.assert_awaited_once_with(
        tenant_id="tenant-1",
        statuses=("needs_label",),
        offset=0,
        limit=100,
    )
