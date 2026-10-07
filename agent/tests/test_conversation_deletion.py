"""Owned soft deletion, restore, turn races and HTTP access boundaries."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from app.api.conversations import router
from app.conversation.context import ContextCursor, ConversationMemory
from app.conversation.service import ConversationAgentService
from app.models.conversation import ConversationRecord, ConversationTurnRecord
from app.repositories.conversation import ConversationConflict, ConversationNotFound
from app.schemas.conversation import ToolTrace
from tests.test_conversation_store import store  # noqa: F401


TENANT = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"


def headers(**overrides):
    return {"Authorization": "Bearer public-deletion-test-token", "X-Tenant-ID": TENANT,
            "X-User-ID": USER, "X-Hot-News-Roles": "hot-news:read", **overrides}


def application(repository):
    model = AsyncMock()
    service = ConversationAgentService(repository=repository, model=model,
                                       tools=SimpleNamespace(), turn_timeout_seconds=37)
    app = FastAPI()
    app.include_router(router)
    app.state.settings = SimpleNamespace(data_loop_gateway_token="public-deletion-test-token")
    app.state.conversation_service = service
    return app, model


@pytest.mark.asyncio
async def test_delete_hides_all_access_paths_and_restore_preserves_history_memory_and_traces(store):
    repository, database = store
    conversation = await repository.create(TENANT, USER, "保留历史")
    claim = await repository.begin_turn(TENANT, USER, conversation.id, uuid4(), "历史问题")
    trace = ToolTrace(name="read_hot_news", status="completed", attempts=1,
                      arguments={"run_id": str(uuid4())}, result={"news_id": "synthetic-news"})
    finished = await repository.finish_turn(TENANT, USER, conversation.id, claim.turn.id,
                                            "历史回答", [trace], ["persisted-model-call"])
    record = database.records[ConversationRecord][0]
    memory = ConversationMemory(summary="历史摘要", through=ContextCursor(
        created_at=finished.created_at, id=finished.id), compressed_turns=1)
    record.context_memory = memory.model_dump(mode="json")
    await repository.delete(TENANT, USER, conversation.id)
    deleted_at = record.deleted_at
    assert deleted_at is not None and deleted_at.tzinfo is not None
    await repository.delete(TENANT, USER, conversation.id)
    assert record.deleted_at == deleted_at
    assert await repository.list(TENANT, USER) == []
    assert await repository.get(TENANT, USER, conversation.id, stale_after_seconds=90) is None
    before = ContextCursor(created_at=datetime.now(timezone.utc) + timedelta(seconds=1), id=uuid4())
    for action in (
        lambda: repository.begin_turn(TENANT, USER, conversation.id, claim.turn.request_id, "历史问题"),
        lambda: repository.load_memory(TENANT, USER, conversation.id),
        lambda: repository.context_page(TENANT, USER, conversation.id, after=None, before=before),
        lambda: repository.checkpoint_turn(TENANT, USER, conversation.id, claim.turn.id,
                                            claim.turn.request_id, [trace], ["persisted-model-call"]),
        lambda: repository.finish_turn(TENANT, USER, conversation.id, claim.turn.id, "迟到",
                                       [trace], ["persisted-model-call"]),
        lambda: repository.save_memory(tenant_id=TENANT, user_id=USER,
            conversation_id=conversation.id, turn_id=claim.turn.id, request_id=claim.turn.request_id,
            expected_through=memory.through, memory=memory, tools=[trace], model_request_ids=[]),
    ):
        with pytest.raises(ConversationNotFound):
            await action()
    restored = await repository.restore(TENANT, USER, conversation.id)
    assert restored.id == conversation.id and record.deleted_at is None
    assert (await repository.get(TENANT, USER, conversation.id)).turns == [finished]
    assert await repository.load_memory(TENANT, USER, conversation.id) == memory
    assert await repository.context_page(TENANT, USER, conversation.id, after=None, before=before) == [finished]
    assert await repository.restore(TENANT, USER, conversation.id) == restored
    replay = await repository.begin_turn(TENANT, USER, conversation.id, claim.turn.request_id, "历史问题")
    assert not replay.acquired and replay.turn == finished
    assert len(database.records[ConversationTurnRecord]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant,user", [("other-tenant", USER), (TENANT, "other-user")])
async def test_delete_and_restore_never_cross_owner_scope_even_after_deletion(store, tenant, user):
    repository, database = store
    conversation = await repository.create(TENANT, USER, "拥有者")
    for deleted in (False, True):
        if deleted:
            await repository.delete(TENANT, USER, conversation.id)
        for identifier in (conversation.id, uuid4()):
            with pytest.raises(ConversationNotFound):
                await repository.delete(tenant, user, identifier)
            with pytest.raises(ConversationNotFound):
                await repository.restore(tenant, user, identifier)
        assert (database.records[ConversationRecord][0].deleted_at is not None) == deleted
    assert len(database.records[ConversationRecord]) == 1


@pytest.mark.asyncio
async def test_live_turn_refuses_delete_and_stale_turn_is_interrupted_without_losing_checkpoint(store):
    repository, database = store
    conversation = await repository.create(TENANT, USER, "处理中")
    claim = await repository.begin_turn(TENANT, USER, conversation.id, uuid4(), "查询")
    trace = ToolTrace(name="capabilities", status="completed", attempts=1, arguments={}, result={"read_only": True})
    await repository.checkpoint_turn(TENANT, USER, conversation.id, claim.turn.id,
                                    claim.turn.request_id, [trace], ["checkpoint-model"])
    with pytest.raises(ConversationConflict, match="processing turn"):
        await repository.delete(TENANT, USER, conversation.id, stale_after_seconds=90)
    with pytest.raises(ValueError):
        await repository.delete(TENANT, USER, conversation.id, stale_after_seconds=0)
    assert database.records[ConversationRecord][0].deleted_at is None
    active = database.records[ConversationTurnRecord][0]
    assert active.status == "processing" and active.completed_at is None
    active.created_at = datetime.now(timezone.utc) - timedelta(seconds=91)
    await repository.delete(TENANT, USER, conversation.id, stale_after_seconds=90)
    assert active.status == "failed" and active.error_code == "interrupted"
    assert active.completed_at is not None and active.assistant_content is None
    assert active.tools == [trace.model_dump(mode="json")] and active.model_request_ids == ["checkpoint-model"]
    await repository.restore(TENANT, USER, conversation.id)
    with pytest.raises(ConversationConflict):
        await repository.finish_turn(TENANT, USER, conversation.id, claim.turn.id, "迟到回答",
                                       [trace], ["checkpoint-model"])
    with pytest.raises(ConversationConflict):
        await repository.checkpoint_turn(TENANT, USER, conversation.id, claim.turn.id,
                                        claim.turn.request_id, [trace], ["checkpoint-model"])
    replay = await repository.begin_turn(TENANT, USER, conversation.id, claim.turn.request_id, "查询")
    assert not replay.acquired and replay.turn.error_code == "interrupted"
    assert (await repository.begin_turn(TENANT, USER, conversation.id, uuid4(), "新问题")).acquired


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["delete", "begin"])
async def test_delete_and_new_claim_are_serialized_by_the_same_owner_row_lock(store, first):
    repository, database = store
    conversation = await repository.create(TENANT, USER, "并发")
    calls = {"delete": lambda: repository.delete(TENANT, USER, conversation.id),
             "begin": lambda: repository.begin_turn(TENANT, USER, conversation.id, uuid4(), "新消息")}
    order = [first, "begin" if first == "delete" else "delete"]
    results = await asyncio.gather(*(calls[name]() for name in order), return_exceptions=True)
    record = database.records[ConversationRecord][0]
    if first == "delete":
        assert results[0] is None and isinstance(results[1], ConversationNotFound)
        assert record.deleted_at is not None and not database.records[ConversationTurnRecord]
    else:
        assert results[0].acquired and isinstance(results[1], ConversationConflict)
        assert record.deleted_at is None and len(database.records[ConversationTurnRecord]) == 1
    for statement in database.lock_queries:
        sql = str(statement.compile(dialect=postgresql.dialect()))
        assert "FOR UPDATE" in sql and "conversations.tenant_id =" in sql and "conversations.user_id =" in sql


@pytest.mark.asyncio
async def test_http_delete_is_idempotent_hidden_messages_are_denied_and_restore_never_calls_model(store):
    repository, _database = store
    app, model = application(repository)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers()) as client:
        created = await client.post("/api/v1/conversations", json={"title": "删除接口"})
        assert created.status_code == 201
        path = "/api/v1/conversations/" + created.json()["id"]
        for _ in range(2):
            response = await client.delete(path)
            assert response.status_code == 204 and response.content == b""
        assert (await client.get("/api/v1/conversations")).json()["items"] == []
        assert (await client.get(path)).status_code == 404
        body = {"request_id": str(uuid4()), "content": "删除后不能处理"}
        for suffix in ("/messages", "/messages/stream"):
            assert (await client.post(path + suffix, json=body)).status_code == 404
        for _ in range(2):
            restored = await client.post(path + "/restore")
            assert restored.status_code == 200 and restored.json()["id"] == created.json()["id"]
        assert (await client.get(path)).status_code == 200
        assert len((await client.get("/api/v1/conversations")).json()["items"]) == 1
    assert not model.mock_calls


@pytest.mark.asyncio
async def test_http_deadline_matches_read_and_send_budget_and_allows_only_expired_work(store):
    repository, database = store
    conversation = await repository.create(TENANT, USER, "期限")
    claim = await repository.begin_turn(TENANT, USER, conversation.id, uuid4(), "原消息")
    active = database.records[ConversationTurnRecord][0]
    active.created_at = datetime.now(timezone.utc) - timedelta(seconds=66)
    app, model = application(repository)  # 37 + 30 seconds, not repository's default 90.
    path = f"/api/v1/conversations/{conversation.id}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers()) as client:
        assert (await client.delete(path)).status_code == 409
        assert (await client.get(path)).json()["turns"][0]["status"] == "processing"
        active.created_at = datetime.now(timezone.utc) - timedelta(seconds=68)
        assert (await client.delete(path)).status_code == 204
        assert (await client.post(path + "/restore")).status_code == 200
        turn = (await client.get(path)).json()["turns"][0]
        assert turn["id"] == str(claim.turn.id) and turn["error_code"] == "interrupted"
    assert not model.mock_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["X-Tenant-ID", "X-User-ID"])
async def test_http_delete_and_restore_scope_permission_and_storage_failures(store, field):
    repository, _database = store
    conversation = await repository.create(TENANT, USER, "权限")
    app, _model = application(repository)
    path = f"/api/v1/conversations/{conversation.id}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.delete(path)).status_code == 401
        assert (await client.post(path + "/restore")).status_code == 401
        denied = headers(**{"X-Hot-News-Roles": "hot-news:decide"})
        assert (await client.delete(path, headers=denied)).status_code == 403
        assert (await client.post(path + "/restore", headers=denied)).status_code == 403
        foreign = headers(**{field: str(uuid4())})
        assert (await client.delete(path, headers=foreign)).status_code == 404
        assert (await client.delete(path, headers=headers())).status_code == 204
        assert (await client.delete(path, headers=foreign)).status_code == 404
        assert (await client.post(path + "/restore", headers=foreign)).status_code == 404
        missing = f"/api/v1/conversations/{uuid4()}"
        assert (await client.delete(missing, headers=headers())).status_code == 404
        assert (await client.post(missing + "/restore", headers=headers())).status_code == 404
        app.state.conversation_service.repository = SimpleNamespace(
            delete=AsyncMock(side_effect=SQLAlchemyError()), restore=AsyncMock(side_effect=SQLAlchemyError()))
        assert (await client.delete(path, headers=headers())).status_code == 503
        assert (await client.post(path + "/restore", headers=headers())).status_code == 503


def test_soft_delete_column_is_nullable_and_timezone_aware():
    column = ConversationRecord.__table__.c.deleted_at
    assert column.nullable and column.type.timezone
