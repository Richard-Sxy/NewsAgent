"""Conversation contracts, transaction claims, scope filters and PostgreSQL DDL."""

import asyncio
import importlib.util
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import MetaData, Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import Null

from app.db.base import Base
from app.models.conversation import ConversationRecord, ConversationTurnRecord
from app.repositories.conversation import (
    ConversationConflict,
    ConversationNotFound,
    PostgresConversationRepository,
)
from app.schemas.conversation import (
    CreateConversationRequest,
    SendConversationMessageRequest,
    ToolTrace,
)


class QueryResult:
    def __init__(self, records):
        self.records = records

    def scalar_one_or_none(self):
        assert len(self.records) <= 1
        return self.records[0] if self.records else None

    def scalars(self):
        return self

    def all(self):
        return list(self.records)


class FakeSession:
    """Evaluate SQLAlchemy equality selects; serialize row claims like PostgreSQL."""

    def __init__(self, database):
        self.database = database
        self.pending = []
        self.locks = []

    async def execute(self, statement):
        model = statement.column_descriptions[0]["entity"]
        criteria = statement._where_criteria

        def matches(record):
            def value(element):
                if isinstance(element, Null):
                    return None
                if hasattr(element, "value"):
                    return element.value
                if hasattr(element, "clauses"):
                    return tuple(value(part) for part in element.clauses)
                return getattr(record, element.key)
            for clause in criteria:
                assert clause.operator in {operators.eq, operators.lt, operators.gt, operators.is_}
                matched = (
                    value(clause.left) is value(clause.right)
                    if clause.operator is operators.is_
                    else clause.operator(value(clause.left), value(clause.right))
                )
                if not matched:
                    return False
            return True

        records = [record for record in self.database.records[model] if matches(record)]
        if statement._for_update_arg is not None:
            assert model is ConversationRecord
            self.database.lock_queries.append(statement)
            if records:
                lock = self.database.locks.setdefault(records[0].id, asyncio.Lock())
                await lock.acquire()
                self.locks.append(lock)
                records = [record for record in self.database.records[model] if matches(record)]
        for order in reversed(statement._order_by_clauses):
            descending = getattr(order, "modifier", None) is operators.desc_op
            column = order.element if hasattr(order, "element") else order
            records.sort(key=lambda record: getattr(record, column.key), reverse=descending)
        if statement._limit_clause is not None:
            records = records[:statement._limit_clause.value]
        return QueryResult(records)

    def add(self, record):
        self.pending.append(record)

    async def flush(self):
        for record in self.pending:
            self.database.records[type(record)].append(record)
        self.pending.clear()
        turns = self.database.records[ConversationTurnRecord]
        request_keys = [(turn.conversation_id, turn.request_id) for turn in turns]
        assert len(set(request_keys)) == len(request_keys)
        active_ids = [turn.conversation_id for turn in turns if turn.status == "processing"]
        assert len(set(active_ids)) == len(active_ids)
        await asyncio.sleep(0)


class FakeDatabase:
    def __init__(self):
        self.records = {ConversationRecord: [], ConversationTurnRecord: []}
        self.locks = {}
        self.lock_queries = []
        self.commits = 0

    @asynccontextmanager
    async def session(self):
        session = FakeSession(self)
        try:
            yield session
            self.commits += 1
        finally:
            for lock in reversed(session.locks):
                lock.release()


@pytest.fixture
def store():
    database = FakeDatabase()
    return PostgresConversationRepository(database), database


def test_request_contract_strips_whitespace_and_forbids_identity_injection():
    assert CreateConversationRequest().title == "新对话"
    assert CreateConversationRequest(title="  运营讨论  ").title == "运营讨论"
    request = SendConversationMessageRequest(request_id=uuid4(), content="  查找热点\n")
    assert request.content == "查找热点"
    for title in (" ", "a" * 101):
        with pytest.raises(ValidationError):
            CreateConversationRequest(title=title)
    for content in ("\n", "a" * 4001):
        with pytest.raises(ValidationError):
            SendConversationMessageRequest(request_id=uuid4(), content=content)
    with pytest.raises(ValidationError):
        CreateConversationRequest(title="conversation", tenant_id="other-tenant")
    with pytest.raises(ValidationError):
        SendConversationMessageRequest(request_id=uuid4(), content="hi", user_id="other-user")
    with pytest.raises(ValidationError):
        ToolTrace(name="tool", status="completed", attempts=1, arguments={}, result={}, instruction="override")


@pytest.mark.asyncio
async def test_create_list_and_get_are_tenant_and_user_scoped(store):
    repository, _database = store
    owned = await repository.create("tenant-a", "user-a", "Owned")
    await repository.create("tenant-a", "user-b", "Other user")
    await repository.create("tenant-b", "user-a", "Other tenant")
    assert [item.id for item in await repository.list("tenant-a", "user-a")] == [owned.id]
    assert (await repository.get("tenant-a", "user-a", owned.id)).conversation.id == owned.id
    for tenant, user in (("tenant-b", "user-a"), ("tenant-a", "user-b")):
        assert await repository.get(tenant, user, owned.id) is None
        with pytest.raises(ConversationNotFound):
            await repository.begin_turn(tenant, user, owned.id, uuid4(), "hello")
    with pytest.raises(ConversationNotFound):
        await repository.begin_turn("tenant-a", "user-a", uuid4(), uuid4(), "hello")


@pytest.mark.asyncio
async def test_claim_is_idempotent_and_conflicting_content_or_active_turn_is_rejected(store):
    repository, database = store
    conversation = await repository.create("tenant", "user", "讨论")
    request_id = uuid4()
    claim = await repository.begin_turn("tenant", "user", conversation.id, request_id, " hello ")
    assert claim.acquired and claim.turn.status == "processing"
    replay = await repository.begin_turn("tenant", "user", conversation.id, request_id, "hello")
    assert not replay.acquired and replay.turn.id == claim.turn.id
    with pytest.raises(ConversationConflict, match="different content"):
        await repository.begin_turn("tenant", "user", conversation.id, request_id, "different")
    with pytest.raises(ConversationConflict, match="processing turn"):
        await repository.begin_turn("tenant", "user", conversation.id, uuid4(), "next")
    assert len(database.records[ConversationTurnRecord]) == 1
    for statement in database.lock_queries:
        sql = str(statement.compile(dialect=postgresql.dialect()))
        assert "FOR UPDATE" in sql
        assert "conversations.tenant_id =" in sql and "conversations.user_id =" in sql


@pytest.mark.asyncio
async def test_concurrent_same_request_is_claimed_once(store):
    repository, database = store
    conversation = await repository.create("tenant", "user", "讨论")
    request_id = uuid4()
    claims = await asyncio.gather(*[
        repository.begin_turn("tenant", "user", conversation.id, request_id, "hello")
        for _ in range(8)
    ])
    assert sum(claim.acquired for claim in claims) == 1
    assert len({claim.turn.id for claim in claims}) == 1
    assert len(database.records[ConversationTurnRecord]) == 1


@pytest.mark.asyncio
async def test_finish_preserves_full_tool_json_and_completed_replay(store):
    repository, _database = store
    conversation = await repository.create("tenant", "user", "讨论")
    request_id = uuid4()
    claim = await repository.begin_turn("tenant", "user", conversation.id, request_id, "hello")
    trace = ToolTrace(
        name="news.search", status="completed", attempts=1,
        arguments={"query": "财经"}, result={"rows": [{"evidence": "内容" * 5000}], "score": 0.25},
    )
    finished = await repository.finish_turn(
        "tenant", "user", conversation.id, claim.turn.id, "已完成", [trace], ["model-1", "model-2"]
    )
    assert finished.status == "completed" and finished.completed_at is not None
    assert finished.tools == [trace]
    assert finished.model_request_ids == ["model-1", "model-2"]
    replay = await repository.begin_turn("tenant", "user", conversation.id, request_id, "hello")
    assert not replay.acquired and replay.turn == finished
    with pytest.raises(ConversationConflict, match="only a processing"):
        await repository.finish_turn("tenant", "user", conversation.id, claim.turn.id, "overwrite", [], [])


@pytest.mark.asyncio
async def test_turn_runtime_metadata_is_bound_once_and_assistant_text_is_bounded(store):
    repository, _database = store
    conversation = await repository.create("tenant", "user", "讨论")
    request_id = uuid4()
    metadata = {"scene": "news", "prompt_version": "v1", "prompt_sha256": "ab" * 32}
    claim = await repository.begin_turn(
        "tenant", "user", conversation.id, request_id, "hello", runtime_metadata=metadata
    )
    replay = await repository.begin_turn(
        "tenant", "user", conversation.id, request_id, "hello", runtime_metadata={"prompt_version": "v2"}
    )
    assert claim.turn.runtime_metadata == metadata
    assert replay.turn.runtime_metadata == metadata
    with pytest.raises(ValidationError):
        await repository.finish_turn("tenant", "user", conversation.id, claim.turn.id, "x" * 30001, [], [])
    finished = await repository.finish_turn("tenant", "user", conversation.id, claim.turn.id, "x" * 30000, [], [])
    assert len(finished.assistant_content) == 30000
    assert finished.runtime_metadata == metadata


@pytest.mark.asyncio
async def test_failure_replay_and_finish_scope_are_enforced(store):
    repository, _database = store
    conversation = await repository.create("tenant", "user", "讨论")
    other = await repository.create("tenant", "user", "另一个会话")
    request_id = uuid4()
    claim = await repository.begin_turn("tenant", "user", conversation.id, request_id, "hello")
    with pytest.raises(ConversationNotFound):
        await repository.finish_turn("other-tenant", "user", conversation.id, claim.turn.id, None, [], [])
    with pytest.raises(ConversationNotFound):
        await repository.finish_turn("tenant", "other-user", conversation.id, claim.turn.id, None, [], [])
    with pytest.raises(ConversationNotFound):
        await repository.finish_turn("tenant", "user", other.id, claim.turn.id, None, [], [])
    failed = await repository.finish_turn(
        "tenant", "user", conversation.id, claim.turn.id, None, [], ["model-failed"], "model_timeout"
    )
    assert failed.status == "failed" and failed.error_code == "model_timeout"
    replay = await repository.begin_turn("tenant", "user", conversation.id, request_id, "hello")
    assert not replay.acquired and replay.turn == failed
    next_turn = await repository.begin_turn("tenant", "user", conversation.id, uuid4(), "next")
    assert next_turn.acquired


@pytest.mark.asyncio
async def test_expired_turn_is_interrupted_and_late_worker_cannot_overwrite_it(store):
    repository, database = store
    conversation = await repository.create("tenant", "user", "讨论")
    old_request = uuid4()
    old = await repository.begin_turn("tenant", "user", conversation.id, old_request, "old")
    database.records[ConversationTurnRecord][0].created_at = datetime.now(timezone.utc) - timedelta(seconds=91)
    new = await repository.begin_turn("tenant", "user", conversation.id, uuid4(), "new")
    assert new.acquired
    with pytest.raises(ConversationConflict):
        await repository.finish_turn("tenant", "user", conversation.id, old.turn.id, "late response", [], [])
    replay = await repository.begin_turn("tenant", "user", conversation.id, old_request, "old")
    assert not replay.acquired and replay.turn.status == "failed"
    assert replay.turn.error_code == "interrupted" and replay.turn.assistant_content is None
    assert replay.turn.completed_at is not None
    assert (await repository.get("tenant", "user", conversation.id)).turns[-1].id == new.turn.id


@pytest.mark.asyncio
async def test_repeating_expired_request_never_executes_old_message_again(store):
    repository, database = store
    conversation = await repository.create("tenant", "user", "讨论")
    request_id = uuid4()
    old = await repository.begin_turn("tenant", "user", conversation.id, request_id, "old")
    database.records[ConversationTurnRecord][0].created_at = datetime.now(timezone.utc) - timedelta(seconds=91)
    replay = await repository.begin_turn("tenant", "user", conversation.id, request_id, "old")
    assert not replay.acquired and replay.turn.id == old.turn.id
    assert replay.turn.status == "failed" and replay.turn.error_code == "interrupted"
    assert len(database.records[ConversationTurnRecord]) == 1


@pytest.mark.asyncio
async def test_read_without_recovery_threshold_remains_read_only(store):
    repository, database = store
    conversation = await repository.create("tenant", "user", "讨论")
    claim = await repository.begin_turn("tenant", "user", conversation.id, uuid4(), "old")
    database.records[ConversationTurnRecord][0].created_at = datetime.now(timezone.utc) - timedelta(seconds=91)
    prior_updated_at = database.records[ConversationRecord][0].updated_at
    prior_lock_count = len(database.lock_queries)
    detail = await repository.get("tenant", "user", conversation.id)
    assert detail.turns[0].id == claim.turn.id
    assert detail.turns[0].status == "processing"
    assert detail.conversation.updated_at == prior_updated_at
    assert len(database.lock_queries) == prior_lock_count


@pytest.mark.asyncio
async def test_read_recovers_expired_turn_and_releases_the_conversation(store):
    repository, database = store
    conversation = await repository.create("tenant", "user", "讨论")
    claim = await repository.begin_turn(
        "tenant", "user", conversation.id, uuid4(), "old", runtime_metadata={"prompt_version": "v1"}
    )
    database.records[ConversationTurnRecord][0].created_at = datetime.now(timezone.utc) - timedelta(seconds=91)
    prior_updated_at = database.records[ConversationRecord][0].updated_at
    prior_lock_count = len(database.lock_queries)
    detail = await repository.get("tenant", "user", conversation.id, stale_after_seconds=90)
    recovered = detail.turns[0]
    assert recovered.id == claim.turn.id and recovered.status == "failed"
    assert recovered.error_code == "interrupted" and recovered.completed_at is not None
    assert recovered.runtime_metadata == {"prompt_version": "v1"}
    assert detail.conversation.updated_at > prior_updated_at
    assert len(database.lock_queries) == prior_lock_count + 1
    with pytest.raises(ConversationConflict):
        await repository.finish_turn("tenant", "user", conversation.id, claim.turn.id, "late", [], [])
    new = await repository.begin_turn("tenant", "user", conversation.id, uuid4(), "new")
    assert new.acquired


@pytest.mark.asyncio
async def test_read_does_not_interrupt_a_turn_before_its_deadline(store):
    repository, database = store
    conversation = await repository.create("tenant", "user", "讨论")
    claim = await repository.begin_turn("tenant", "user", conversation.id, uuid4(), "active")
    database.records[ConversationTurnRecord][0].created_at = datetime.now(timezone.utc) - timedelta(seconds=20)
    prior_updated_at = database.records[ConversationRecord][0].updated_at
    detail = await repository.get("tenant", "user", conversation.id, stale_after_seconds=90)
    assert detail.turns[0].id == claim.turn.id
    assert detail.turns[0].status == "processing"
    assert detail.turns[0].error_code is None and detail.turns[0].completed_at is None
    assert detail.conversation.updated_at == prior_updated_at
    with pytest.raises(ValueError):
        await repository.get("tenant", "user", conversation.id, stale_after_seconds=0)


@pytest.mark.asyncio
async def test_read_recovery_cannot_change_another_tenant_or_user_conversation(store):
    repository, database = store
    conversation = await repository.create("tenant", "user", "讨论")
    claim = await repository.begin_turn("tenant", "user", conversation.id, uuid4(), "old")
    database.records[ConversationTurnRecord][0].created_at = datetime.now(timezone.utc) - timedelta(seconds=91)
    prior_updated_at = database.records[ConversationRecord][0].updated_at
    for tenant, user in (("other-tenant", "user"), ("tenant", "other-user")):
        assert await repository.get(tenant, user, conversation.id, stale_after_seconds=90) is None
    detail = await repository.get("tenant", "user", conversation.id)
    assert detail.turns[0].id == claim.turn.id and detail.turns[0].status == "processing"
    assert detail.conversation.updated_at == prior_updated_at


@pytest.mark.asyncio
async def test_recent_turns_are_returned_in_chronological_order(store):
    repository, database = store
    conversation = await repository.create("tenant", "user", "讨论")
    for index in range(5):
        request_id = uuid4()
        claim = await repository.begin_turn("tenant", "user", conversation.id, request_id, str(index))
        await repository.finish_turn("tenant", "user", conversation.id, claim.turn.id, str(index), [], [])
        database.records[ConversationTurnRecord][-1].created_at = datetime(2026, 10, 4, index, tzinfo=timezone.utc)
    detail = await repository.get("tenant", "user", conversation.id, limit=2)
    assert [turn.user_content for turn in detail.turns] == ["3", "4"]
    assert len(database.records[ConversationTurnRecord]) == 5
    with pytest.raises(ValueError):
        await repository.get("tenant", "user", conversation.id, limit=0)


def test_postgresql_models_bind_owner_and_constrain_requests_and_active_turns():
    dialect = postgresql.dialect()
    turns = ConversationTurnRecord.__table__
    foreign_key = next(iter(turns.foreign_key_constraints))
    assert tuple(column.name for column in foreign_key.columns) == ("tenant_id", "user_id", "conversation_id")
    assert tuple(element.target_fullname for element in foreign_key.elements) == (
        "conversations.tenant_id", "conversations.user_id", "conversations.id"
    )
    ddl = str(CreateTable(turns).compile(dialect=dialect))
    assert "UNIQUE (conversation_id, request_id)" in ddl
    assert "status IN ('processing', 'completed', 'failed')" in ddl
    assert "completed_at IS NOT NULL" in ddl
    index = next(index for index in turns.indexes if index.name == "uq_conversation_turns_processing")
    assert "CREATE UNIQUE INDEX" in str(CreateIndex(index).compile(dialect=dialect))
    assert "WHERE status = 'processing'" in str(CreateIndex(index).compile(dialect=dialect))


def test_migration_adds_only_conversation_tables_with_matching_constraints():
    path = Path(__file__).parents[1] / "alembic/versions/20261004_0018_conversations.py"
    spec = importlib.util.spec_from_file_location("conversation_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class RecordingOperations:
        def __init__(self):
            self.metadata = MetaData(naming_convention=Base.metadata.naming_convention)
            self.indexes = {}
            self.drops = []

        def create_table(self, name, *columns):
            Table(name, self.metadata, *columns)

        def create_index(self, name, table, columns, **kwargs):
            self.indexes[name] = (table, columns, kwargs)

        def add_column(self, table, column):
            self.metadata.tables[table].append_column(column)

        def drop_column(self, table, column):
            self.drops.append(("column", column))

        def drop_index(self, name, **kwargs):
            self.drops.append(("index", name))

        def drop_table(self, name):
            self.drops.append(("table", name))

    operations = RecordingOperations()
    module.op = operations
    module.upgrade()
    assert module.down_revision == "20261002_0017"
    assert set(operations.metadata.tables) == {"conversations", "conversation_turns"}
    memory_path = path.with_name("20261006_0019_conversation_memory.py")
    memory_spec = importlib.util.spec_from_file_location("conversation_memory_migration", memory_path)
    memory_module = importlib.util.module_from_spec(memory_spec)
    memory_spec.loader.exec_module(memory_module)
    memory_module.op = operations
    assert memory_module.down_revision == module.revision
    memory_module.upgrade()
    deletion_path = path.with_name("20261007_0020_conversation_deletion.py")
    deletion_spec = importlib.util.spec_from_file_location("conversation_deletion_migration", deletion_path)
    deletion_module = importlib.util.module_from_spec(deletion_spec)
    deletion_spec.loader.exec_module(deletion_module)
    deletion_module.op = operations
    assert deletion_module.down_revision == memory_module.revision
    deletion_module.upgrade()
    for model in (ConversationRecord, ConversationTurnRecord):
        expected = str(CreateTable(model.__table__).compile(dialect=postgresql.dialect()))
        actual = str(CreateTable(operations.metadata.tables[model.__tablename__]).compile(dialect=postgresql.dialect()))
        # Additive migrations append columns after existing timestamps; compare
        # semantic DDL clauses without imposing physical column order.
        expected = sorted(expected.splitlines())
        actual = sorted(actual.splitlines())
        assert actual == expected
    assert operations.indexes["uq_conversation_turns_processing"][2]["unique"] is True
    deletion_module.downgrade()
    assert ("column", "deleted_at") in operations.drops
    memory_module.downgrade()
    assert ("column", "context_memory") in operations.drops
    module.downgrade()
    assert [name for kind, name in operations.drops if kind == "table"] == ["conversation_turns", "conversations"]
