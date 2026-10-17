import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.sql import operators

import app.services.memory_write_application as writer_module
from app.conversation.memory_hooks import (
    CANDIDATE_TTL,
    MEMORY_POLICY_VERSION,
    SHORT_TERM_TTL,
    ConversationMemoryHooks,
    MemoryHookArguments,
    MemoryHookSourceNotFoundError,
)
from app.conversation.receipts import CapabilityReceipt
from app.models.conversation import ConversationTurnRecord
from app.models.user_memory import LongTermMemoryCandidateRecord, ShortTermUserMemoryRecord
from app.repositories.user_memory import PostgresUserMemoryRepository
from app.schemas.user_memory import UserMemoryScope
from app.services.memory_write_application import MemoryWriteApplicationService, MemoryWriteConflictError


TENANT = str(uuid4())
USER = str(uuid4())
CONVERSATION = uuid4()
REQUEST = uuid4()
NOW = datetime.now(timezone.utc)


class QueryResult:
    def __init__(self, rows):
        self.rows = rows

    def scalar_one_or_none(self):
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None


class Session:
    def __init__(self, database):
        self.database = database

    async def execute(self, statement):
        self.database.queries.append(statement)
        model = statement.column_descriptions[0]["entity"]
        clauses = statement._where_criteria
        assert all(clause.operator is operators.eq for clause in clauses)
        rows = [row for row in self.database.records[model]
                if all(getattr(row, clause.left.key) == clause.right.value for clause in clauses)]
        return QueryResult(rows)

    async def commit(self):
        self.database.events.append("commit")
        self.database.commits += 1


class Database:
    def __init__(self):
        self.records = {
            ConversationTurnRecord: [ConversationTurnRecord(id=uuid4(), tenant_id=TENANT, user_id=USER,
                conversation_id=CONVERSATION, request_id=REQUEST, created_at=NOW)],
            ShortTermUserMemoryRecord: [], LongTermMemoryCandidateRecord: [],
        }
        self.queries = []
        self.events = []
        self.commits = 0

    @asynccontextmanager
    async def session(self):
        yield Session(self)


class Repository:
    """Use the existing writer's idempotency checks against deterministic rows."""
    def __init__(self, session):
        self.database = session.database

    async def get_short_term_by_idempotency_key(self, *, tenant_id, idempotency_key):
        row = next((row for row in self.database.records[ShortTermUserMemoryRecord]
                    if row.tenant_id == tenant_id and row.idempotency_key == idempotency_key), None)
        return PostgresUserMemoryRepository.to_short_term_domain(row) if row else None

    async def get_candidate_by_idempotency_key(self, *, tenant_id, idempotency_key):
        row = next((row for row in self.database.records[LongTermMemoryCandidateRecord]
                    if row.tenant_id == tenant_id and row.idempotency_key == idempotency_key), None)
        return PostgresUserMemoryRepository.to_candidate_domain(row) if row else None

    async def insert_short_term_memory(self, *, memory, idempotency_key):
        if await self.get_short_term_by_idempotency_key(tenant_id=memory.scope.tenant_id, idempotency_key=idempotency_key):
            return False
        self.database.records[ShortTermUserMemoryRecord].append(ShortTermUserMemoryRecord(
            **PostgresUserMemoryRepository.to_short_term_values(memory, idempotency_key=idempotency_key)))
        return True

    async def insert_memory_candidate(self, *, candidate, idempotency_key):
        if await self.get_candidate_by_idempotency_key(tenant_id=candidate.scope.tenant_id, idempotency_key=idempotency_key):
            return False
        self.database.records[LongTermMemoryCandidateRecord].append(LongTermMemoryCandidateRecord(
            **PostgresUserMemoryRepository.to_candidate_values(candidate, idempotency_key=idempotency_key)))
        return True


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(writer_module, "PostgresUserMemoryRepository", Repository)
    database = Database()
    writer = MemoryWriteApplicationService()
    writer.create_short_term = AsyncMock(wraps=writer.create_short_term)
    writer.propose_long_term = AsyncMock(wraps=writer.propose_long_term)
    return ConversationMemoryHooks(database=database, writer=writer), database, writer


async def write(hooks, *, kind="short_term", **overrides):
    arguments = dict(tenant_id=TENANT, user_id=USER, conversation_id=CONVERSATION,
                     request_id=REQUEST, content="我关注科技频道") | overrides
    method = hooks.save_short_term if kind == "short_term" else hooks.propose_long_term
    return await method(**arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["short_term", "long_term_candidate"])
async def test_write_uses_server_owned_source_scope_ttl_and_version(runtime, kind):
    hooks, database, writer = runtime
    bound = []

    async def on_bound(metadata):
        database.events.append("bound")
        assert database.commits == 1
        bound.append(metadata)

    result = await write(hooks, kind=kind, on_bound=on_bound)
    service = writer.create_short_term if kind == "short_term" else writer.propose_long_term
    command = service.await_args.kwargs["command"]
    assert command.scope == UserMemoryScope(tenant_id=TENANT, user_id=USER)
    assert service.await_args.kwargs["now"] == NOW
    assert command.source_refs[0].captured_at == NOW
    assert command.source_refs[0].source_type == "user_message"
    assert str(CONVERSATION) in command.source_refs[0].source_id
    assert str(REQUEST) in command.source_refs[0].source_id
    assert command.origin == "explicit_user" and command.confidence == 1
    assert command.expires_at == NOW + (SHORT_TERM_TTL if kind == "short_term" else CANDIDATE_TTL)
    assert command.idempotency_key == f"{MEMORY_POLICY_VERSION}:{kind}:{CONVERSATION}:{REQUEST}"
    assert len(command.idempotency_key) <= 128
    assert database.events == ["commit", "bound"]
    assert bound == [result]
    receipt = CapabilityReceipt.model_validate(result["receipt"])
    assert receipt.entity_id == result["memory"]["id"] and receipt.version == "1"
    assert receipt.deep_link is None and result["promotion_performed"] is False
    assert result["memory"]["content"] == "我关注科技频道"
    assert result["memory"]["tier"] == kind
    if kind == "short_term":
        assert command.task_id == str(CONVERSATION)
        assert command.content.kind == "temporary_preference"
        assert receipt.status == "landed" and receipt.pending_gate is None
        writer.propose_long_term.assert_not_awaited()
    else:
        assert command.proposed_content.kind == "stable_preference"
        assert receipt.status == "pending_approval" and receipt.pending_gate == "memory_promotion"
        assert result["memory"]["candidate_status"] == "pending" and result["memory"]["not_promoted"] is True
        writer.create_short_term.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,model", [("short_term", ShortTermUserMemoryRecord),
                                        ("long_term_candidate", LongTermMemoryCandidateRecord)])
async def test_same_request_replays_same_content_without_timestamp_conflict(runtime, kind, model):
    hooks, database, _ = runtime
    first = await write(hooks, kind=kind)
    await asyncio.sleep(0.001)
    second = await write(hooks, kind=kind)
    assert first["memory"] == second["memory"]
    assert first["created"] is True and second["created"] is False
    assert len(database.records[model]) == 1
    assert first["snapshot_at"] != second["snapshot_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,model", [("short_term", ShortTermUserMemoryRecord),
                                        ("long_term_candidate", LongTermMemoryCandidateRecord)])
async def test_same_request_different_content_is_rejected(runtime, kind, model):
    hooks, database, _ = runtime
    await write(hooks, kind=kind)
    with pytest.raises(MemoryWriteConflictError):
        await write(hooks, kind=kind, content="另一个偏好")
    assert len(database.records[model]) == 1 and database.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("tenant_id", str(uuid4())), ("user_id", str(uuid4())),
                                       ("conversation_id", uuid4()), ("request_id", uuid4())])
async def test_write_requires_original_turn_owned_by_same_tenant_and_user(runtime, field, value):
    hooks, database, writer = runtime
    with pytest.raises(MemoryHookSourceNotFoundError):
        await write(hooks, **{field: value})
    assert database.commits == 0
    writer.create_short_term.assert_not_awaited()
    writer.propose_long_term.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", " ", "x" * 1001, 100, True])
async def test_invalid_content_cannot_reach_storage(runtime, content):
    hooks, database, writer = runtime
    with pytest.raises(ValidationError):
        await write(hooks, content=content)
    assert not database.queries
    writer.create_short_term.assert_not_awaited()


@pytest.mark.parametrize("extra", ["tenant_id", "user_id", "sql", "expires_at", "scenario", "kind"])
def test_model_argument_schema_excludes_identity_sql_dates_and_business_controls(extra):
    with pytest.raises(ValidationError):
        MemoryHookArguments(**{"content": "我关注科技", extra: "untrusted"})


@pytest.mark.asyncio
async def test_content_trimming_and_summary_are_deterministic_without_losing_text(runtime):
    hooks, _, writer = runtime
    result = await write(hooks, content="  " + "偏" * 700 + "  ")
    command = writer.create_short_term.await_args.kwargs["command"]
    assert command.content.value == "偏" * 700
    assert command.content.summary == "偏" * 500
    assert result["memory"]["content"] == "偏" * 700


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["short_term", "long_term_candidate"])
async def test_write_failure_is_not_retried_or_claimed_landed(runtime, kind):
    hooks, database, writer = runtime
    method = writer.create_short_term if kind == "short_term" else writer.propose_long_term
    method.side_effect = ConnectionError("storage failure")
    bound = AsyncMock()
    with pytest.raises(ConnectionError):
        await write(hooks, kind=kind, on_bound=bound)
    assert method.await_count == 1 and database.commits == 0
    bound.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_after_commit_preserves_saved_memory_and_binding(runtime):
    hooks, database, _ = runtime
    entered = asyncio.Event()
    metadata = []

    async def bind(result):
        metadata.append(result)
        entered.set()
        await asyncio.Future()

    task = asyncio.create_task(write(hooks, on_bound=bind))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert database.commits == 1 and len(database.records[ShortTermUserMemoryRecord]) == 1
    assert metadata[0]["receipt"]["entity_id"] == str(database.records[ShortTermUserMemoryRecord][0].id)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["short_term", "long_term_candidate"])
async def test_status_reads_latest_authoritative_version_with_owner_filter(runtime, kind):
    hooks, database, _ = runtime
    saved = await write(hooks, kind=kind)
    model = ShortTermUserMemoryRecord if kind == "short_term" else LongTermMemoryCandidateRecord
    row = database.records[model][0]
    row.version = 7
    row.status = "expired" if kind == "short_term" else "rejected"
    result = await hooks.status(tenant_id=TENANT, user_id=USER, memory_id=row.id, kind=kind)
    assert result["memory"]["version"] == "7" and result["receipt"]["version"] == "7"
    assert result["memory"]["status"] == row.status
    assert result["receipt"]["status"] == "landed" and result["receipt"]["pending_gate"] is None
    assert result["memory"]["id"] == saved["memory"]["id"]
    for identity in ({"tenant_id": str(uuid4()), "user_id": USER},
                     {"tenant_id": TENANT, "user_id": str(uuid4())}):
        hidden = await hooks.status(**identity, memory_id=row.id, kind=kind)
        assert hidden["not_found"] is True and hidden["memory"] is None
        assert hidden["receipt"]["status"] == "not_landed"
    assert not any(statement._for_update_arg is not None for statement in database.queries)


@pytest.mark.asyncio
async def test_pending_candidate_followup_reports_approval_without_promoting(runtime):
    hooks, database, writer = runtime
    saved = await write(hooks, kind="long_term_candidate")
    pending = await hooks.status(tenant_id=TENANT, user_id=USER, memory_id=saved["memory"]["id"],
                                 kind="long_term_candidate")
    assert pending["receipt"]["status"] == "pending_approval"
    row = database.records[LongTermMemoryCandidateRecord][0]
    row.status, row.version = "approved", 2
    approved = await hooks.status(tenant_id=TENANT, user_id=USER, memory_id=row.id, kind="long_term_candidate")
    assert approved["memory"]["candidate_status"] == "approved"
    assert approved["memory"]["not_promoted"] is False and approved["promotion_performed"] is False
    assert approved["receipt"]["status"] == "landed" and approved["receipt"]["version"] == "2"
    assert writer.propose_long_term.await_count == 1
    assert not hasattr(hooks, "promote") and not hasattr(hooks, "approve")


@pytest.mark.asyncio
async def test_expired_pending_candidate_is_effectively_expired_without_mutation(runtime):
    hooks, database, _ = runtime
    database.records[ConversationTurnRecord][0].created_at = NOW - timedelta(days=8)
    saved = await write(hooks, kind="long_term_candidate")
    assert saved["memory"]["candidate_status"] == "pending"
    assert saved["memory"]["effective_status"] == "expired"
    assert saved["receipt"]["status"] == "landed" and saved["receipt"]["pending_gate"] is None
    assert database.records[LongTermMemoryCandidateRecord][0].status == "pending"


@pytest.mark.asyncio
async def test_unknown_memory_kind_is_rejected_without_query(runtime):
    hooks, database, _ = runtime
    with pytest.raises(ValueError, match="memory_kind_invalid"):
        await hooks.status(tenant_id=TENANT, user_id=USER, memory_id=uuid4(), kind="approved_long_term")
    assert not database.queries
