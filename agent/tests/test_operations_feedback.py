"""运营决定回流：合成快照、隔离替身，不读取凭证或写真实数据库。"""

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.api.dependencies import (
    DataLoopPermission,
    HotNewsPermission,
    get_hot_news_decision_service,
)
from app.models.analysis_feedback import AnalysisFeedbackCaseRecord
from app.models.hot_news_decision import HotNewsDecision
from app.repositories.analysis_feedback import PostgresAnalysisFeedbackRepository
from app.repositories.evaluation_dataset import PostgresEvaluationDatasetRepository
from app.schemas.hot_news_decision import RecordHotNewsDecisionCommand
from app.schemas.analysis_feedback import FeedbackSourceReference
from app.schemas.operations_feedback import is_fresh_bad_case_eligible
from app.services.data_loop.dataset_freezer import EvaluationDatasetNotReadyError
from app.services.data_loop.feedback_collector import (
    AnalysisFeedbackCollector,
    AnalysisFeedbackConflictError,
    AnalysisFeedbackPersistenceError,
    AnalysisFeedbackTargetNotFoundError,
)
from app.services.hot_news_decision import (
    HotNewsDecisionConflictError,
    HotNewsDecisionService,
)
from tests.test_analysis_feedback_schema import NOW
from tests.test_data_loop_management_api import (
    TENANT_ID as DATA_LOOP_TENANT,
    USER_ID as DATA_LOOP_USER,
    client_with as data_loop_client,
)
from tests.test_feedback_collector import memory, repository_mock
from tests.test_hot_news_api import (
    TENANT_ID as HOT_NEWS_TENANT,
    USER_ID as HOT_NEWS_USER,
    client_with as hot_news_client,
)
from tests.test_hot_news_decision_service import ScalarResult
from tests.test_dataset_freezer import (
    CASE_A, DATASET_ID, FakeArtifactStore, FakeRepository,
    _command, _freeze, _freezer, _snapshot,
)


def feedback(category="editorial_judgment", reason_codes=None):
    return {
        "schema_version": "operations-feedback-v1",
        "category": category,
        "reason_codes": reason_codes or ["low_public_value"],
    }


def decision_command(decision_type="accepted", **overrides):
    values = {
        "run_id": memory().run_id,
        "news_id": "news-1",
        "decision_type": decision_type,
        "reason": "合成运营反馈，仅用于契约测试",
        "idempotency_key": "operations-feedback-test-1",
        "correction_payload": (
            {"dominant_driver": "click"} if decision_type == "corrected" else {}
        ),
    }
    values.update(overrides)
    return RecordHotNewsDecisionCommand(**values)


def persisted_decision(request, **overrides):
    values = {
        "id": UUID(int=30),
        "tenant_id": "tenant-1",
        "run_id": request.run_id,
        "news_id": request.news_id,
        "decision_type": request.decision_type,
        "reason": request.reason,
        "correction_payload": request.correction_payload,
        "operator_feedback": (
            None if request.operator_feedback is None
            else request.operator_feedback.model_dump(mode="json")
        ),
        "operator_id": "operator-1",
        "idempotency_key": request.idempotency_key,
        "supersedes_decision_id": request.supersedes_decision_id,
        "created_at": NOW,
    }
    values.update(overrides)
    return HotNewsDecision(**values)


def decision_service(*results, collector=None):
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[ScalarResult(result) for result in results]),
        committed=False,
        rolled_back=False,
    )

    @asynccontextmanager
    async def open_session():
        try:
            yield session
        except BaseException:
            session.rolled_back = True
            raise
        else:
            session.committed = True

    return HotNewsDecisionService(
        database=SimpleNamespace(session=open_session),
        memory_store=SimpleNamespace(get_analysis_memory=AsyncMock(return_value=memory())),
        feedback_collector=collector,
    ), session


@pytest.mark.parametrize("category", [
    "analysis_error", "editorial_judgment", "strategy_judgment", "positive_example",
])
def test_structured_feedback_accepts_controlled_categories_and_defaults_version(category):
    request = decision_command(operator_feedback={
        "category": category, "reason_codes": ["insufficient_evidence"],
    })
    assert request.operator_feedback.schema_version == "operations-feedback-v1"
    assert request.operator_feedback.category == category


@pytest.mark.parametrize("invalid", [
    feedback(category="unregistered_category"),
    feedback(reason_codes=["unregistered_reason"]),
    {**feedback(), "schema_version": "operations-feedback-v99"},
    {**feedback(), "user_ids": ["raw-user-event-sentinel"]},
    {**feedback(), "reason_codes": []},
    feedback(reason_codes=["low_public_value", "low_public_value"]),
])
def test_structured_feedback_rejects_unknown_contract_and_raw_behavior(invalid):
    with pytest.raises(ValidationError):
        decision_command(operator_feedback=invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["accepted", "rejected", "deferred", "corrected"])
async def test_four_new_decisions_create_unlabeled_cases_in_the_same_session(monkeypatch, action):
    repository = repository_mock(monkeypatch)
    repository.analysis_run_contains_news.return_value = True
    repository.get_case_by_idempotency_key.return_value = None
    repository.insert_case.return_value = True
    request = decision_command(action, operator_feedback=feedback())
    inserted = persisted_decision(request)
    service, session = decision_service(inserted, collector=AnalysisFeedbackCollector())

    result, created = await service.record_decision(
        tenant_id="tenant-1", operator_id="operator-1", command=request,
        feedback_problem_type="unsupported_claim", feedback_severity="high",
    )

    assert result is inserted and created is True
    case = repository.insert_case.await_args.kwargs["case"]
    assert case.status == "needs_label"
    assert case.source_type == f"operator_{action}"
    assert case.problem_type == "other"
    assert case.source_reference.operator_feedback.category == "editorial_judgment"
    assert case.source_reference.reference_id == str(inserted.id)
    assert case.analysis_input_snapshot == memory().analysis_input
    assert case.analysis_output_snapshot == memory().analysis_report
    assert case.occurred_at == inserted.created_at
    assert session.committed is True
    assert not hasattr(case, "verdict")
    repository.insert_label.assert_not_awaited()
    record = AnalysisFeedbackCaseRecord(**PostgresAnalysisFeedbackRepository.to_case_values(case))
    assert PostgresAnalysisFeedbackRepository.to_case_domain(record) == case


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["accepted", "deferred"])
async def test_new_legacy_shaped_positive_or_deferred_defaults_are_other_and_low(action):
    collector = SimpleNamespace(collect_from_operator_decision=AsyncMock())
    request = decision_command(action)
    inserted = persisted_decision(request)
    service, session = decision_service(inserted, collector=collector)

    await service.record_decision(tenant_id="tenant-1", operator_id="operator-1", command=request)

    call = collector.collect_from_operator_decision.await_args
    assert call.args == (session,)
    assert call.kwargs["problem_type"] == "other"
    assert call.kwargs["severity"] == "low"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["accepted", "deferred"])
async def test_historical_positive_or_deferred_replay_does_not_backfill(action):
    collector = SimpleNamespace(collect_from_operator_decision=AsyncMock())
    request = decision_command(action)
    existing = persisted_decision(request)
    service, _ = decision_service(None, existing, collector=collector)

    result, created = await service.record_decision(
        tenant_id="tenant-1", operator_id="operator-1", command=request,
    )

    assert result is existing and created is False
    collector.collect_from_operator_decision.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_positive_feedback_replay_reuses_the_case_and_stable_hash(monkeypatch):
    repository = repository_mock(monkeypatch)
    repository.get_case_by_idempotency_key.return_value = None
    repository.insert_case.return_value = True
    request = decision_command(operator_feedback=feedback(
        category="positive_example", reason_codes=["analysis_supported"],
    ))
    inserted = persisted_decision(request)
    service, _ = decision_service(inserted, None, inserted, collector=AnalysisFeedbackCollector())
    first = await service.record_decision(tenant_id="tenant-1", operator_id="operator-1", command=request)
    first_case = repository.insert_case.await_args.kwargs["case"]
    repository.get_case_by_idempotency_key.return_value = first_case

    second = await service.record_decision(tenant_id="tenant-1", operator_id="operator-1", command=request)

    assert first[1] is True and second[1] is False
    repository.insert_case.assert_awaited_once()
    repository.insert_label.assert_not_awaited()
    assert first_case.status == "needs_label" and first_case.problem_type == "other"


@pytest.mark.asyncio
async def test_changed_feedback_category_conflicts_under_the_same_decision_key():
    request = decision_command(operator_feedback=feedback())
    existing = persisted_decision(request, operator_feedback=feedback(
        category="strategy_judgment", reason_codes=["frequency_budget_exhausted"],
    ))
    collector = SimpleNamespace(collect_from_operator_decision=AsyncMock())
    service, session = decision_service(None, existing, collector=collector)

    with pytest.raises(HotNewsDecisionConflictError):
        await service.record_decision(tenant_id="tenant-1", operator_id="operator-1", command=request)

    assert session.rolled_back is True
    collector.collect_from_operator_decision.assert_not_awaited()


@pytest.mark.asyncio
async def test_feedback_write_failure_aborts_the_decision_transaction():
    request = decision_command(operator_feedback=feedback())
    collector = SimpleNamespace(collect_from_operator_decision=AsyncMock(
        side_effect=RuntimeError("synthetic-feedback-write-failure"),
    ))
    service, session = decision_service(persisted_decision(request), collector=collector)

    with pytest.raises(RuntimeError, match="synthetic-feedback-write-failure"):
        await service.record_decision(tenant_id="tenant-1", operator_id="operator-1", command=request)

    assert session.rolled_back is True and session.committed is False
    assert collector.collect_from_operator_decision.await_args.args == (session,)


@pytest.mark.parametrize("route", ["hot_news", "data_loop"])
@pytest.mark.parametrize("failure_type,status", [
    (AnalysisFeedbackTargetNotFoundError, 404),
    (AnalysisFeedbackConflictError, 409),
    (AnalysisFeedbackPersistenceError, 503),
])
def test_new_accepted_feedback_failures_roll_back_and_keep_public_http_error_contract(
    route, failure_type, status,
):
    request = decision_command(operator_feedback=feedback(
        category="positive_example", reason_codes=["analysis_supported"],
    ))
    collector = SimpleNamespace(collect_from_operator_decision=AsyncMock(
        side_effect=failure_type("synthetic-feedback-error"),
    ))
    service, session = decision_service(persisted_decision(request), collector=collector)
    if route == "hot_news":
        client = hot_news_client(decision_service=service, permission=HotNewsPermission.DECIDE)
        path = "/api/v1/hot-news/decisions"
    else:
        client = data_loop_client({get_hot_news_decision_service: service},
                                  permission=DataLoopPermission.FEEDBACK_WRITE)
        path = "/api/v1/data-loop/operator-decisions"

    response = client.post(path, json=request.model_dump(mode="json"))

    assert response.status_code == status
    assert "synthetic-feedback-error" in response.json()["detail"]
    assert session.rolled_back is True and session.committed is False
    assert collector.collect_from_operator_decision.await_args.args == (session,)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["rejected", "corrected"])
async def test_unclassified_existing_negative_feedback_preserves_legacy_hash(monkeypatch, action):
    repository = repository_mock(monkeypatch)
    repository.get_case_by_idempotency_key.return_value = None
    repository.insert_case.return_value = True
    request = decision_command(action)
    outcome = await AnalysisFeedbackCollector().collect_from_operator_decision(
        object(), tenant_id="tenant-1", memory=memory(),
        run_idempotency_key="hot-news-run-1", decision=persisted_decision(request),
        problem_type="analysis_incorrect", severity="medium", occurred_at=NOW,
        recorded_at=NOW, feedback_case_id=UUID(int=1),
    )
    case = outcome.case
    analysis_memory = memory()
    # Independent pre-change payload, including the original five source fields.
    # No Feedback Case serializer participates in constructing the baseline.
    payload = {
        "tenant_id": "tenant-1",
        "run_id": str(analysis_memory.run_id),
        "run_idempotency_key": "hot-news-run-1",
        "news_id": analysis_memory.news_id,
        "source_type": f"operator_{action}",
        "problem_type": "analysis_incorrect",
        "severity": "medium",
        "production_bundle_version": analysis_memory.production_bundle_version,
        "analysis_input_snapshot": analysis_memory.analysis_input.model_dump(mode="json"),
        "analysis_output_snapshot": analysis_memory.analysis_report.model_dump(mode="json"),
        "source_reference": {
            "reference_type": "operator_decision",
            "reference_id": str(UUID(int=30)),
            "request_id": analysis_memory.model_request_id,
            "diagnostic_code": action,
            "diagnostic_summary": request.reason,
        },
        "occurred_at": "2026-09-09T10:00:00Z",
    }
    assert case.source_reference.model_dump(mode="json") == payload["source_reference"]
    expected = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    assert case.content_sha256 == expected


def test_legacy_reference_serialization_keeps_existing_null_fields():
    reference = FeedbackSourceReference(reference_type="operator_decision", reference_id="decision-1")
    assert reference.model_dump(mode="json") == {
        "reference_type": "operator_decision", "reference_id": "decision-1",
        "request_id": None, "diagnostic_code": None, "diagnostic_summary": None,
    }


@pytest.mark.parametrize("verdict", ["correct", "incorrect", "partially_correct", "not_evaluable"])
@pytest.mark.parametrize("category", [
    "analysis_error", "editorial_judgment", "strategy_judgment", "positive_example",
])
def test_fresh_eligibility_requires_reviewed_error_and_analysis_category(verdict, category):
    assert is_fresh_bad_case_eligible(
        verdict=verdict, source_type="operator_corrected",
        source_reference={"operator_feedback": feedback(category=category)},
    ) is (verdict in {"incorrect", "partially_correct"} and category == "analysis_error")


@pytest.mark.parametrize("source_type, eligible", [
    ("operator_accepted", False), ("operator_deferred", False),
    ("operator_rejected", True), ("operator_corrected", True),
    ("validation_failed", True), ("retrieval_error", True),
])
def test_fresh_legacy_sources_keep_error_semantics(source_type, eligible):
    assert is_fresh_bad_case_eligible(
        verdict="incorrect", source_type=source_type, source_reference={},
    ) is eligible


@pytest.mark.parametrize("route", ["hot_news", "data_loop"])
def test_both_http_entries_preserve_feedback_and_authenticated_identity(route):
    service = SimpleNamespace(record_decision=AsyncMock(return_value=(
        SimpleNamespace(id=uuid4(), decision_type="deferred"), True,
    )))
    if route == "hot_news":
        client = hot_news_client(decision_service=service, permission=HotNewsPermission.DECIDE)
        path, tenant, user = "/api/v1/hot-news/decisions", HOT_NEWS_TENANT, HOT_NEWS_USER
    else:
        client = data_loop_client({get_hot_news_decision_service: service},
                                  permission=DataLoopPermission.FEEDBACK_WRITE)
        path, tenant, user = "/api/v1/data-loop/operator-decisions", DATA_LOOP_TENANT, DATA_LOOP_USER
    request = decision_command("deferred", operator_feedback=feedback(
        category="strategy_judgment", reason_codes=["timing_inappropriate"],
    ))
    response = client.post(path, json=request.model_dump(mode="json"))

    assert response.status_code == 201
    args = service.record_decision.await_args.kwargs
    assert args["tenant_id"] == str(tenant) and args["operator_id"] == str(user)
    assert args["command"].operator_feedback == request.operator_feedback


@pytest.mark.asyncio
async def test_fresh_automatic_selection_excludes_correct_labels_and_non_analysis_categories():
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: []),
    )))
    repository = PostgresEvaluationDatasetRepository(session)
    assert await repository.list_approved_case_ids_for_window(
        tenant_id="tenant-1", occurred_start=NOW - timedelta(hours=1),
        occurred_end=NOW, source_cutoff_at=NOW, limit=20,
    ) == ()
    statement = session.execute.await_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    query = str(compiled)
    bound = compiled.params
    values = list(bound.values())
    assert "feedback_labels.verdict IN" in query
    assert any(set(value) == {"incorrect", "partially_correct"}
               for value in values if isinstance(value, (tuple, list)))
    assert "operator_feedback" in values and "category" in values
    assert "analysis_error" in values
    assert "tenant-1" in values


@pytest.mark.asyncio
async def test_manual_freeze_lookup_still_allows_human_selected_correct_cases():
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
    repository = PostgresEvaluationDatasetRepository(session)
    assert await repository.list_approved_feedback_snapshots(
        tenant_id="tenant-1", feedback_case_ids=[UUID(int=1)], source_cutoff_at=NOW,
    ) == ()
    compiled = session.execute.await_args.args[0].compile(dialect=postgresql.dialect())
    query = str(compiled)
    assert "feedback_labels.verdict IN" not in query
    assert "operator_feedback" not in compiled.params.values()


def positive_snapshot(category="positive_example", verdict="correct"):
    snapshot = _snapshot(CASE_A, "a")
    return snapshot.model_copy(update={
        "expected": snapshot.expected.model_copy(update={"verdict": verdict}),
        "lineage": snapshot.lineage.model_copy(update={
            "source_type": "operator_accepted", "problem_type": "other",
            "source_reference": {
                **snapshot.lineage.source_reference,
                "operator_feedback": feedback(category=category),
            },
        }),
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("category,verdict", [
    ("editorial_judgment", "incorrect"), ("strategy_judgment", "incorrect"),
    ("positive_example", "incorrect"), ("analysis_error", "correct"),
])
async def test_manual_new_fresh_cannot_bypass_error_and_category_rules(category, verdict):
    repo = FakeRepository([positive_snapshot(category=category, verdict=verdict)])
    store = FakeArtifactStore()
    with pytest.raises(EvaluationDatasetNotReadyError, match="cannot enter fresh_bad_case"):
        await _freeze(_freezer(repo, store), _command(ids=(CASE_A,)))
    assert store.put_calls == 0 and repo.saved_manifest is None


@pytest.mark.asyncio
async def test_reviewed_positive_case_can_be_explicitly_frozen_as_golden():
    repo = FakeRepository([positive_snapshot()])
    store = FakeArtifactStore()
    result = await _freeze(_freezer(repo, store), _command(ids=(CASE_A,), dataset_layer="golden"))
    assert result.created is True and result.dataset.dataset_layer == "golden"
    assert repo.saved_manifest.cases[0].expected.verdict == "correct"


@pytest.mark.asyncio
async def test_existing_frozen_fresh_with_old_correct_label_remains_hash_readable():
    repo = FakeRepository([_snapshot(CASE_A, "a")])
    store = FakeArtifactStore()
    freezer = _freezer(repo, store)
    command = _command(ids=(CASE_A,))
    result = await _freeze(freezer, command)
    old_case = repo.saved_manifest.cases[0]
    # Materialize an old-contract frozen artifact. Previously correct labels
    # were accepted into Fresh; read/replay must preserve that immutable evidence.
    manifest = repo.saved_manifest.model_copy(update={"cases": (
        old_case.model_copy(update={"expected": old_case.expected.model_copy(update={"verdict": "correct"})}),
    )})
    receipt = await store.put_json(dataset_id=DATASET_ID, content=manifest.canonical_content)
    repo.saved_manifest = manifest
    repo.references[("tenant-1", command.idempotency_key)] = result.dataset.model_copy(update={
        "content_sha256": manifest.content_sha256, "artifact_uri": receipt.storage_uri,
        "artifact_size": receipt.content_size,
    })

    loaded = await freezer.load_manifest(tenant_id="tenant-1", dataset_id=DATASET_ID)
    assert loaded == manifest and loaded.cases[0].expected.verdict == "correct"
