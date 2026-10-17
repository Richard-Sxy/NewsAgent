"""Conversation feedback reuses atomic operator decisions and reviewed Case boundaries."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import Insert

import app.conversation.feedback_hooks as hook_module
import app.services.data_loop.feedback_collector as collector_module
from app.conversation.feedback_hooks import (
    ConversationFeedbackHooks, DRAFT_REASON_PREFIX, FEEDBACK_CONFIRMATION_NOTICE,
)
from app.models.hot_news_decision import HotNewsDecision
from app.repositories.analysis_feedback import PostgresAnalysisFeedbackRepository
from app.services.data_loop.feedback_collector import AnalysisFeedbackCollector, AnalysisFeedbackPersistenceError
from app.services.hot_news_decision import (
    HotNewsDecisionConflictError, HotNewsDecisionService, HotNewsDecisionTargetNotFoundError,
)
from tests.test_hot_news_writing_handoff import make_memory


NOW = datetime.now(timezone.utc)
USER = "22222222-2222-4222-8222-222222222222"


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class DecisionDatabase:
    """Execute the existing service's decision insert/replay select in isolation."""

    def __init__(self):
        self.rows = []
        self.commits = 0
        self.active_sessions = []

    @asynccontextmanager
    async def session(self):
        self.active_sessions.append(self)
        try:
            yield self
            self.commits += 1
        finally:
            self.active_sessions.pop()

    async def execute(self, statement):
        if isinstance(statement, Insert):
            assert statement.table.name == "hot_news_decisions"
            values = statement.compile().params
            if any(row.tenant_id == values["tenant_id"] and row.idempotency_key == values["idempotency_key"]
                   for row in self.rows):
                return Result(None)
            decision = HotNewsDecision(**values, created_at=NOW, updated_at=NOW)
            self.rows.append(decision)
            return Result(decision)
        matches = [row for row in self.rows if all(
            getattr(row, clause.left.key) == clause.right.value for clause in statement._where_criteria)]
        assert len(matches) <= 1
        return Result(matches[0] if matches else None)


def feedback_harness(monkeypatch):
    memory = make_memory()
    store = SimpleNamespace(get_analysis_memory=AsyncMock(return_value=memory))
    database = DecisionDatabase()
    repository = create_autospec(PostgresAnalysisFeedbackRepository, instance=True)
    cases = {}
    async def belongs_to_run(**scope):
        assert scope == {"tenant_id": memory.tenant_id, "run_id": memory.run_id,
            "run_idempotency_key": memory.run_idempotency_key, "news_id": memory.news_id,
            "production_bundle_version": memory.production_bundle_version}
        return True
    async def read_case(*, tenant_id, idempotency_key):
        return cases.get((tenant_id, idempotency_key))
    async def insert_case(*, case):
        assert database.active_sessions, "Decision and feedback must share the application transaction"
        cases[(case.tenant_id, case.idempotency_key)] = case
        return True
    repository.analysis_run_contains_news.side_effect = belongs_to_run
    repository.get_case_by_idempotency_key.side_effect = read_case
    repository.insert_case.side_effect = insert_case
    monkeypatch.setattr(collector_module, "PostgresAnalysisFeedbackRepository", lambda session: repository)
    monkeypatch.setattr(hook_module, "PostgresAnalysisFeedbackRepository", lambda session: repository)
    collector = AnalysisFeedbackCollector()
    decisions = HotNewsDecisionService(database=database, memory_store=store, feedback_collector=collector)
    hooks = ConversationFeedbackHooks(database=database, run_store=store, decisions=decisions)
    proposal = {"run_id": str(memory.run_id), "news_id": memory.news_id,
                "issue_type": "unsupported_claim", "expected_label_draft": "缺少足够证据，应明确限制，不能断言因果关系"}
    identity = dict(tenant_id=memory.tenant_id, user_id=USER, conversation_id=uuid4(), request_id=uuid4())
    return hooks, memory, store, database, repository, cases, proposal, identity


@pytest.mark.asyncio
async def test_feedback_prepare_uses_server_snapshot_and_discloses_decision_without_writing(monkeypatch):
    hooks, memory, store, database, repository, cases, proposal, identity = feedback_harness(monkeypatch)
    result = await hooks.prepare(tenant_id=identity["tenant_id"], proposal=proposal)
    assert result["suggested"] == proposal
    fields = result["confirmation_fields"]
    assert fields["run_id"] == str(memory.run_id) and fields["news_id"] == memory.news_id
    assert fields["production_bundle_version"] == memory.production_bundle_version
    assert fields["expected_label_draft"] == proposal["expected_label_draft"]
    assert fields["source_action"] == "reject_analysis" and fields["notice"] == FEEDBACK_CONFIRMATION_NOTICE
    assert "驳回" in fields["notice"] and "草稿" in fields["notice"]
    assert result["receipt"]["entity_type"] == "feedback_case"
    assert result["receipt"]["status"] == "not_landed" and result["receipt"]["entity_id"] is None
    store.get_analysis_memory.assert_awaited_once_with(
        tenant_id=identity["tenant_id"], run_id=memory.run_id, news_id=memory.news_id)
    assert not database.rows and not cases and database.commits == 0
    repository.insert_case.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_feedback_persists_full_analysis_snapshot_then_receipt_not_label(monkeypatch):
    hooks, memory, _, database, repository, cases, proposal, identity = feedback_harness(monkeypatch)
    callbacks = []
    async def bound(result):
        assert database.commits >= 1 and len(database.rows) == len(cases) == 1
        callbacks.append(result)
    result = await hooks.start(**identity, proposal=proposal, on_bound=bound)
    case = next(iter(cases.values()))
    decision = database.rows[0]
    assert callbacks == [result] and result["case_id"] == str(case.id)
    assert case.source_type == "operator_rejected" and case.problem_type == proposal["issue_type"]
    assert case.analysis_input_snapshot == memory.analysis_input
    assert case.analysis_output_snapshot == memory.analysis_report
    assert case.run_id == memory.run_id and case.run_idempotency_key == memory.run_idempotency_key
    assert case.production_bundle_version == memory.production_bundle_version
    assert case.source_reference.reference_id == str(decision.id)
    assert case.source_reference.operator_feedback.category == "analysis_error"
    assert case.source_reference.diagnostic_summary == DRAFT_REASON_PREFIX + proposal["expected_label_draft"]
    assert decision.operator_id == USER and decision.decision_type == "rejected"
    assert str(identity["conversation_id"]) in decision.idempotency_key
    assert str(identity["request_id"]) in decision.idempotency_key and len(decision.idempotency_key) <= 128
    assert result["receipt"]["status"] == "pending_approval"
    assert result["receipt"]["version"] == case.content_sha256
    assert result["receipt"]["deep_link"] == f"/data-loop?case_id={case.id}"
    assert result["receipt"]["pending_gate"] == "feedback_label_review"
    assert result["label_submitted"] is False and result["label_approved"] is False
    assert case.status == "needs_label" and decision.correction_payload == {}
    repository.insert_label.assert_not_awaited()
    repository.approve_label.assert_not_awaited()


@pytest.mark.asyncio
async def test_feedback_request_replay_does_not_duplicate_decision_or_case_and_changes_conflict(monkeypatch):
    hooks, _, _, database, _, cases, proposal, identity = feedback_harness(monkeypatch)
    first = await hooks.start(**identity, proposal=proposal)
    replay = await hooks.start(**identity, proposal=proposal)
    assert replay["case_id"] == first["case_id"] and replay["decision_id"] == first["decision_id"]
    assert first["created"] is True and replay["created"] is False
    assert len(database.rows) == len(cases) == 1
    with pytest.raises(HotNewsDecisionConflictError):
        await hooks.start(**identity, proposal={**proposal, "expected_label_draft": "另一份标签草稿"})
    with pytest.raises(HotNewsDecisionConflictError):
        await hooks.start(**identity, proposal={**proposal, "issue_type": "metric_mismatch"})
    assert len(database.rows) == len(cases) == 1


@pytest.mark.asyncio
async def test_maximum_draft_is_preserved_without_overflowing_existing_decision_reason(monkeypatch):
    hooks, _, _, database, _, cases, proposal, identity = feedback_harness(monkeypatch)
    proposal["expected_label_draft"] = "草" * 96
    result = await hooks.start(**identity, proposal=proposal)
    assert len(database.rows[0].reason) <= 128
    assert result["expected_label_draft"] == proposal["expected_label_draft"]
    assert next(iter(cases.values())).source_reference.diagnostic_summary.endswith(proposal["expected_label_draft"])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"tenant_id": "another-tenant"}, {"production_bundle_version": "model-injected-version"},
    {"expected_label_draft": "x" * 97}, {"expected_label_draft": "  "},
    {"issue_type": "approve_label"}, {"approved_by": USER}, {"sql": "select 1"},
])
async def test_feedback_model_arguments_are_strict_and_cannot_set_source_or_approval(monkeypatch, change):
    hooks, _, store, database, repository, _, proposal, identity = feedback_harness(monkeypatch)
    with pytest.raises(ValidationError):
        await hooks.prepare(tenant_id=identity["tenant_id"], proposal={**proposal, **change})
    assert not database.rows and not store.get_analysis_memory.await_count
    repository.insert_case.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["tenant", "run", "news", "input", "report", "missing"])
async def test_feedback_source_identity_is_checked_before_any_write(monkeypatch, mismatch):
    hooks, memory, store, database, repository, _, proposal, identity = feedback_harness(monkeypatch)
    changes = {
        "tenant": {"tenant_id": str(uuid4())}, "run": {"run_id": uuid4()},
        "news": {"news_id": "foreign-news"},
        "input": {"analysis_input": memory.analysis_input.model_copy(update={"news_id": "foreign-news"})},
        "report": {"analysis_report": memory.analysis_report.model_copy(update={"news_id": "foreign-news"})},
    }
    store.get_analysis_memory.return_value = None if mismatch == "missing" else memory.model_copy(update=changes[mismatch])
    with pytest.raises(HotNewsDecisionTargetNotFoundError):
        await hooks.prepare(tenant_id=identity["tenant_id"], proposal=proposal)
    with pytest.raises(HotNewsDecisionTargetNotFoundError):
        await hooks.start(**identity, proposal=proposal)
    assert not database.rows
    repository.insert_case.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_tenant_lookup_cannot_emit_foreign_case_receipt(monkeypatch):
    hooks, _, _, database, repository, _, proposal, _ = feedback_harness(monkeypatch)
    with pytest.raises(HotNewsDecisionTargetNotFoundError):
        await hooks.prepare(tenant_id=str(uuid4()), proposal=proposal)
    assert not database.rows
    repository.insert_case.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_committed_case_fails_without_a_fabricated_case_id(monkeypatch):
    hooks, _, _, database, repository, _, proposal, identity = feedback_harness(monkeypatch)
    calls = 0
    original_read = repository.get_case_by_idempotency_key.side_effect
    async def missing_after_commit(**kwargs):
        nonlocal calls
        calls += 1
        return None if calls > 1 else await original_read(**kwargs)
    repository.get_case_by_idempotency_key.side_effect = missing_after_commit
    callback = AsyncMock()
    with pytest.raises(AnalysisFeedbackPersistenceError, match="无法读取"):
        await hooks.start(**identity, proposal=proposal, on_bound=callback)
    assert len(database.rows) == 1 and database.commits >= 1
    callback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["bundle", "issue", "input", "report", "source", "draft"])
async def test_adapter_rejects_case_projection_that_loses_original_snapshot_binding(monkeypatch, mismatch):
    hooks, memory, _, _, repository, _, proposal, identity = feedback_harness(monkeypatch)
    original_read = repository.get_case_by_idempotency_key.side_effect
    async def corrupt_read(**kwargs):
        case = await original_read(**kwargs)
        if case is None:
            return None
        changes = {
            "bundle": {"production_bundle_version": "different-bundle"},
            "issue": {"problem_type": "other"},
            "input": {"analysis_input_snapshot": memory.analysis_input.model_copy(update={"title": "changed title"})},
            "report": {"analysis_output_snapshot": memory.analysis_report.model_copy(update={"trend_assessment": "changed report"})},
            "source": {"source_reference": case.source_reference.model_copy(update={"reference_id": str(uuid4())})},
            "draft": {"source_reference": case.source_reference.model_copy(update={"diagnostic_summary": "changed draft"})},
        }
        return case.model_copy(update=changes[mismatch])
    repository.get_case_by_idempotency_key.side_effect = corrupt_read
    callback = AsyncMock()
    with pytest.raises(AnalysisFeedbackPersistenceError, match="来源不一致"):
        await hooks.start(**identity, proposal=proposal, on_bound=callback)
    callback.assert_not_awaited()

