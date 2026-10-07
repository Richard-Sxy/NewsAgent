"""Committed terminal-tool checkpoints under the original conversation claim."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from app.conversation.plan import ConversationPlan
from app.conversation.service import ConversationAgentService
from app.models.conversation import ConversationTurnRecord
from app.model_runtime.core import RawInferenceResult
from app.repositories.conversation import ConversationConflict, ConversationNotFound, PostgresConversationRepository
from app.schemas.conversation import ToolTrace
from tests.test_conversation_store import FakeDatabase
from tests.test_conversation_stream import (
    TENANT, USER, collect, decode, prepare, reply, runtime, stream,
)


def trace(name: str = "capabilities", status: str = "completed", **changes) -> ToolTrace:
    return ToolTrace(name=name, status=status, attempts=1, arguments={},
                     result={"checked": True}, **changes)


@pytest.fixture
def store():
    database = FakeDatabase()
    return PostgresConversationRepository(database), database


async def claimed(repository):
    conversation = await repository.create(TENANT, USER, "checkpoint test")
    claim = await repository.begin_turn(TENANT, USER, conversation.id, uuid4(), "show capabilities")
    return conversation, claim


async def checkpoint(repository, conversation, claim, tools, model_ids=None, **changes):
    arguments = {
        "tenant_id": TENANT, "user_id": USER, "conversation_id": conversation.id,
        "turn_id": claim.turn.id, "request_id": claim.turn.request_id,
        "tools": tools, "model_request_ids": model_ids or ["model-one"],
    }
    arguments.update(changes)
    return await repository.checkpoint_turn(**arguments)


@pytest.mark.asyncio
async def test_checkpoint_commits_terminal_traces_without_finishing_and_is_idempotent(store):
    repository, database = store
    conversation, claim = await claimed(repository)
    completed = trace()
    checkpointed = await checkpoint(repository, conversation, claim, [completed])

    assert checkpointed.status == "processing"
    assert checkpointed.completed_at is None and checkpointed.assistant_content is None
    assert checkpointed.tools == [completed]
    assert checkpointed.model_request_ids == ["model-one"]
    replay = await checkpoint(repository, conversation, claim, [completed])
    assert replay == checkpointed
    completed.result["checked"] = False
    stored = (await repository.get(TENANT, USER, conversation.id)).turns[0]
    assert stored.tools[0].result["checked"] is True
    assert database.commits >= 5


@pytest.mark.asyncio
async def test_checkpoint_appends_denied_and_failed_terminal_tools_without_rewriting_prefix(store):
    repository, _database = store
    conversation, claim = await claimed(repository)
    first = trace()
    await checkpoint(repository, conversation, claim, [first])
    denied = trace("read_hot_news", "denied", error_code="tool_not_allowed_or_invalid")
    await checkpoint(repository, conversation, claim, [first, denied], ["model-one", "model-two"])
    failed = trace("analyze_hot_news_data", "failed", error_code="analysis_timeout")
    saved = await checkpoint(repository, conversation, claim, [first, denied, failed], ["model-one", "model-two", "model-three"])

    assert saved.tools == [first, denied, failed]
    assert saved.status == "processing"


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["tenant", "user", "request", "turn", "conversation"])
async def test_checkpoint_requires_exact_tenant_user_conversation_turn_and_request(store, identity):
    repository, _database = store
    conversation, claim = await claimed(repository)
    changes = {
        "tenant": {"tenant_id": "other-tenant"}, "user": {"user_id": "other-user"},
        "request": {"request_id": uuid4()}, "turn": {"turn_id": uuid4()},
        "conversation": {"conversation_id": uuid4()},
    }[identity]

    with pytest.raises(ConversationNotFound):
        await checkpoint(repository, conversation, claim, [trace()], **changes)
    stored = (await repository.get(TENANT, USER, conversation.id)).turns[0]
    assert stored.tools == [] and stored.model_request_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["checkpoint", "finish"])
@pytest.mark.parametrize("rewrite", ["short_tools", "changed_tool", "short_ids", "changed_id"])
async def test_old_or_short_writes_cannot_replace_a_committed_checkpoint(store, method, rewrite):
    repository, _database = store
    conversation, claim = await claimed(repository)
    first = trace()
    await checkpoint(repository, conversation, claim, [first])
    tools, ids = [first], ["model-one"]
    if rewrite == "short_tools":
        tools = []
    elif rewrite == "changed_tool":
        tools = [trace("read_hot_news")]
    elif rewrite == "short_ids":
        ids = []
    else:
        ids = ["changed-model"]

    with pytest.raises(ConversationConflict, match="checkpoint cannot"):
        if method == "checkpoint":
            await repository.checkpoint_turn(TENANT, USER, conversation.id, claim.turn.id, claim.turn.request_id, tools, ids)
        else:
            await repository.finish_turn(TENANT, USER, conversation.id, claim.turn.id, "old", tools, ids)
    stored = (await repository.get(TENANT, USER, conversation.id)).turns[0]
    assert stored.tools == [first] and stored.model_request_ids == ["model-one"]
    assert stored.status == "processing"
    finished = await repository.finish_turn(TENANT, USER, conversation.id, claim.turn.id, "done", [first], ["model-one", "final-model"])
    assert finished.status == "completed" and finished.tools == [first]


@pytest.mark.asyncio
async def test_expiry_recovery_preserves_checkpoint_and_rejects_late_owner_writes(store):
    repository, database = store
    conversation, claim = await claimed(repository)
    first = trace()
    await checkpoint(repository, conversation, claim, [first])
    database.records[ConversationTurnRecord][0].created_at = datetime.now(timezone.utc) - timedelta(seconds=91)
    recovered = (await repository.get(TENANT, USER, conversation.id, stale_after_seconds=90)).turns[0]

    assert recovered.status == "failed" and recovered.error_code == "interrupted"
    assert recovered.tools == [first] and recovered.model_request_ids == ["model-one"]
    assert recovered.assistant_content is None
    with pytest.raises(ConversationConflict, match="processing"):
        await checkpoint(repository, conversation, claim, [first])
    with pytest.raises(ConversationConflict, match="processing"):
        await repository.finish_turn(TENANT, USER, conversation.id, claim.turn.id, "late", [first], ["model-one"])
    replay = await repository.begin_turn(TENANT, USER, conversation.id, claim.turn.request_id, claim.turn.user_content)
    assert not replay.acquired and replay.turn == recovered


@pytest.mark.asyncio
async def test_explicit_interruption_keeps_already_committed_tools(store):
    repository, _database = store
    conversation, claim = await claimed(repository)
    first = trace()
    await checkpoint(repository, conversation, claim, [first])
    interrupted = await repository.finish_turn(TENANT, USER, conversation.id, claim.turn.id, "interrupted", [first], ["model-one"], "interrupted")

    assert interrupted.status == "failed" and interrupted.tools == [first]
    assert interrupted.model_request_ids == ["model-one"]


class SimulatedProcessExit(BaseException):
    """Exit outside ordinary exception/cancellation cleanup, without killing pytest."""


@pytest.mark.asyncio
async def test_service_commits_tool_before_simulated_crash_and_later_claim_recovery(store):
    repository, database = store

    class Model:
        calls = 0

        def input_chars(self, *, payload, **_kwargs):
            return len(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))

        async def run_structured(self, **_kwargs):
            self.calls += 1
            if self.calls == 2:
                raise SimulatedProcessExit()
            return SimpleNamespace(request_id="model-one", value=ConversationPlan(action="tool", tool_name="capabilities", arguments={}))

    class Tools:
        calls = 0

        async def execute(self, **_kwargs):
            self.calls += 1
            return {"limits": "approved read tools"}

    tools, model = Tools(), Model()
    service = ConversationAgentService(repository=repository, model=model, tools=tools)
    conversation = await repository.create(TENANT, USER, "crash test")
    claim = await service.prepare_turn(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id, request_id=uuid4(), content="capabilities")
    with pytest.raises(SimulatedProcessExit):
        await service.run_turn(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id, claim=claim)
    unfinished = (await repository.get(TENANT, USER, conversation.id)).turns[0]
    assert unfinished.status == "processing" and unfinished.tools[0].status == "completed"
    assert unfinished.model_request_ids == ["model-one"]
    database.records[ConversationTurnRecord][0].created_at = datetime.now(timezone.utc) - timedelta(seconds=91)
    recovered_claim = await service.prepare_turn(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id, request_id=claim.turn.request_id, content=claim.turn.user_content)
    assert not recovered_claim.acquired and recovered_claim.turn.error_code == "interrupted"
    assert recovered_claim.turn.tools == unfinished.tools
    replay = await service.run_turn(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id, claim=recovered_claim)
    assert replay == recovered_claim.turn and tools.calls == 1 and model.calls == 2


@pytest.mark.asyncio
async def test_stream_tool_finished_is_after_checkpoint_and_normal_answer_remains_committed():
    async def behavior(_request, call):
        if call == 1:
            return RawInferenceResult(content='{"action":"tool","tool_name":"capabilities","arguments":{}}', request_id="tool-model")
        return reply("已核对允许工具。")

    service, repository, _inference, tools = runtime(behavior=behavior)
    conversation, claim = await prepare(service, repository)
    iterator = stream(service, conversation, claim)
    events = []
    try:
        async for frame in iterator:
            event = decode(frame)
            events.append(event)
            if event[0] == "tool_finished":
                assert len(repository.checkpoint_calls) == 1
                checkpointed = repository.checkpoint_calls[0]
                assert checkpointed.status == "processing"
                assert checkpointed.tools[0].status == "completed"
                assert checkpointed.model_request_ids == ["tool-model"]
    finally:
        await iterator.aclose()
    assert events[-1][0] == "done" and events[-1][1]["turn"]["status"] == "completed"
    assert len(tools.calls) == 1


@pytest.mark.asyncio
async def test_checkpoint_storage_failure_stops_planning_and_does_not_publish_tool_finished():
    async def behavior(_request, _call):
        return RawInferenceResult(content='{"action":"tool","tool_name":"capabilities","arguments":{}}')

    service, repository, inference, tools = runtime(behavior=behavior)
    repository.checkpoint_error = OperationalError("hidden SQL", {}, RuntimeError("private detail"))
    conversation, claim = await prepare(service, repository)
    events = await collect(stream(service, conversation, claim))

    assert len(tools.calls) == len(inference.calls) == 1
    assert repository.checkpoint_calls == []
    assert not any(name == "tool_finished" for name, _data in events)
    assert events[-1][0] == "done" and events[-1][1]["turn"]["status"] == "failed"
    assert "private detail" not in str(events)


@pytest.mark.asyncio
async def test_stream_disconnect_after_checkpoint_preserves_committed_tool_trace():
    waiting = asyncio.Event()

    async def behavior(_request, call):
        if call == 1:
            return RawInferenceResult(content='{"action":"tool","tool_name":"capabilities","arguments":{}}', request_id="tool-model")
        waiting.set()
        await asyncio.Event().wait()

    service, repository, _inference, _tools = runtime(behavior=behavior)
    conversation, claim = await prepare(service, repository)
    iterator = stream(service, conversation, claim)
    await anext(iterator)
    await asyncio.wait_for(waiting.wait(), timeout=1)
    assert len(repository.checkpoint_calls) == 1
    await iterator.aclose()
    stored = repository.turns[conversation.id][0]
    assert stored.status == "failed" and stored.error_code == "interrupted"
    assert stored.tools == repository.checkpoint_calls[0].tools
    assert stored.model_request_ids == ["tool-model"]
