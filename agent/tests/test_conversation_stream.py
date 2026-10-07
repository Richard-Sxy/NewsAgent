"""Authenticated SSE progress with committed-only answers, using in-memory Ports."""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from starlette.requests import ClientDisconnect

from app.api.conversations import ConversationStreamingResponse, router
from app.conversation.service import ConversationAgentService
from app.conversation.context import ConversationMemory
from app.conversation.streaming import StreamingConversation, encode_event
from app.model_runtime.agent_client import NativeStructuredAgentClient
from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.core import RawInferenceResult, StructuredInferenceService
from app.repositories.conversation import ConversationConflict, ConversationNotFound, TurnClaim
from app.schemas.conversation import ConversationDetailResponse, ConversationTurnView, ConversationView, ToolTrace


TENANT = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"
TOKEN = "stream-public-test-token"


class MemoryRepository:
    def __init__(self):
        self.conversations = {}
        self.turns = {}
        self.finish_calls = []
        self.checkpoint_calls = []
        self.memories = {}
        self.checkpoint_error = None
        self.save_error = None
        self.save_started = None
        self.save_release = None

    def owner(self, tenant_id, user_id, conversation_id):
        owner = self.conversations.get(conversation_id)
        if owner is None or owner[:2] != (tenant_id, user_id):
            raise ConversationNotFound()
        return owner[2]

    async def create(self, tenant_id, user_id, title):
        now = datetime.now(timezone.utc)
        conversation = ConversationView(id=uuid4(), title=title, created_at=now, updated_at=now)
        self.conversations[conversation.id] = (tenant_id, user_id, conversation)
        self.turns[conversation.id] = []
        return conversation

    async def get(self, tenant_id, user_id, conversation_id, limit=50, **kwargs):
        conversation = self.owner(tenant_id, user_id, conversation_id)
        return ConversationDetailResponse(
            conversation=conversation,
            turns=[turn.model_copy(deep=True) for turn in self.turns[conversation_id][-limit:]],
        )

    async def begin_turn(self, tenant_id, user_id, conversation_id, request_id, content, **kwargs):
        self.owner(tenant_id, user_id, conversation_id)
        for turn in self.turns[conversation_id]:
            if turn.request_id == request_id:
                if turn.user_content != content:
                    raise ConversationConflict()
                return TurnClaim(turn.model_copy(deep=True), False)
        if any(turn.status == "processing" for turn in self.turns[conversation_id]):
            raise ConversationConflict()
        turn = ConversationTurnView(
            id=uuid4(), request_id=request_id, user_content=content, status="processing",
            created_at=datetime.now(timezone.utc), runtime_metadata=kwargs.get("runtime_metadata", {}),
        )
        self.turns[conversation_id].append(turn)
        return TurnClaim(turn.model_copy(deep=True), True)

    async def load_memory(self, tenant_id, user_id, conversation_id):
        self.owner(tenant_id, user_id, conversation_id)
        return self.memories.get(conversation_id, ConversationMemory()).model_copy(deep=True)

    async def context_page(self, tenant_id, user_id, conversation_id, *, after, before, limit=100):
        self.owner(tenant_id, user_id, conversation_id)
        return sorted([turn.model_copy(deep=True) for turn in self.turns[conversation_id]
                       if turn.status == "completed" and (turn.created_at, turn.id) < before.key()
                       and (after is None or (turn.created_at, turn.id) > after.key())],
                      key=lambda turn: (turn.created_at, turn.id))[:limit]

    async def save_memory(self, *, tenant_id, user_id, conversation_id, turn_id, request_id,
                          expected_through, memory, tools, model_request_ids):
        current = await self.load_memory(tenant_id, user_id, conversation_id)
        if current.through != expected_through:
            raise ConversationConflict()
        await self.checkpoint_turn(tenant_id, user_id, conversation_id, turn_id, request_id,
                                   tools, model_request_ids)
        self.memories[conversation_id] = memory.model_copy(deep=True)

    async def checkpoint_turn(self, tenant_id, user_id, conversation_id, turn_id, request_id, tools, model_request_ids):
        self.owner(tenant_id, user_id, conversation_id)
        if self.checkpoint_error is not None:
            raise self.checkpoint_error
        for turn in self.turns[conversation_id]:
            if turn.id == turn_id and turn.request_id == request_id:
                if turn.status != "processing":
                    raise ConversationConflict()
                traces = [ToolTrace.model_validate(item).model_copy(deep=True) for item in tools]
                if traces[:len(turn.tools)] != turn.tools or model_request_ids[:len(turn.model_request_ids)] != turn.model_request_ids:
                    raise ConversationConflict()
                turn.tools = traces
                turn.model_request_ids = list(model_request_ids)
                self.checkpoint_calls.append(turn.model_copy(deep=True))
                return turn.model_copy(deep=True)
        raise ConversationNotFound()

    async def finish_turn(self, tenant_id, user_id, conversation_id, turn_id, **kwargs):
        self.owner(tenant_id, user_id, conversation_id)
        self.finish_calls.append(kwargs)
        if kwargs["error_code"] != "interrupted":
            if self.save_started is not None:
                self.save_started.set()
            if self.save_release is not None:
                await self.save_release.wait()
            if self.save_error is not None:
                raise self.save_error
        for turn in self.turns[conversation_id]:
            if turn.id == turn_id:
                if turn.status != "processing":
                    raise ConversationConflict()
                if kwargs["tools"][:len(turn.tools)] != turn.tools or kwargs["model_request_ids"][:len(turn.model_request_ids)] != turn.model_request_ids:
                    raise ConversationConflict()
                for key, value in kwargs.items():
                    setattr(turn, key, value)
                turn.status = "failed" if kwargs["error_code"] else "completed"
                turn.completed_at = datetime.now(timezone.utc)
                return turn.model_copy(deep=True)
        raise ConversationNotFound()


class Inference:
    def __init__(self, behavior=None):
        self.calls = []
        self.behavior = behavior

    async def complete(self, request):
        self.calls.append(request)
        if self.behavior is not None:
            return await self.behavior(request, len(self.calls))
        return reply("已保存的安全解释。\n下一行解释。")


class ReadTools:
    descriptions = [{"name": "capabilities", "arguments": {}}]

    def __init__(self):
        self.calls = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["tenant_id"] == TENANT
        return {"limits": "本轮仅查询已批准的只读能力。"}


def reply(answer):
    return RawInferenceResult(
        content=json.dumps({"action": "respond", "answer": answer}, ensure_ascii=False),
        request_id="approved-request",
    )


def runtime(*, behavior=None, timeout=1):
    config = load_model_runtime_config("deploy/model-runtime.local.yml")
    inference, tools, repository = Inference(behavior), ReadTools(), MemoryRepository()
    model = NativeStructuredAgentClient(
        StructuredInferenceService(inference=inference, prompts=config.prompt_registry()),
        config, timeout_seconds=1,
    )
    service = ConversationAgentService(
        repository=repository, model=model, tools=tools, turn_timeout_seconds=timeout,
    )
    return service, repository, inference, tools


async def prepare(service, repository, content="你好", request_id=None):
    conversation = await repository.create(TENANT, USER, "流式测试")
    claim = await service.prepare_turn(
        tenant_id=TENANT, user_id=USER, conversation_id=conversation.id,
        request_id=request_id or uuid4(), content=content,
    )
    return conversation, claim


def stream(service, conversation, claim, **kwargs):
    return StreamingConversation(
        service=service, claim=claim, tenant_id=TENANT, user_id=USER,
        conversation_id=conversation.id, heartbeat_seconds=0.03, chunk_chars=7, **kwargs,
    ).iter_events()


def decode(frame):
    if isinstance(frame, bytes):
        frame = frame.decode("utf-8")
    if frame.startswith(":"):
        return "heartbeat", {}
    lines = frame.split("\n")
    assert len([line for line in lines if line.startswith("event: ")]) == 1
    assert len([line for line in lines if line.startswith("data: ")]) == 1
    return lines[0][7:], json.loads(lines[1][6:])


async def next_event(iterator):
    return decode(await asyncio.wait_for(anext(iterator), timeout=1))


async def collect(iterator):
    events = []
    while True:
        try:
            events.append(await next_event(iterator))
        except StopAsyncIteration:
            return events


def answer(events):
    deltas = [data for name, data in events if name == "answer_delta"]
    assert [item["index"] for item in deltas] == list(range(len(deltas)))
    return "".join(item["text"] for item in deltas)


@pytest.mark.asyncio
async def test_live_progress_precedes_slow_model_and_tool_output_has_no_raw_payload():
    waiting, release = asyncio.Event(), asyncio.Event()

    async def behavior(request, call):
        if call == 1:
            return RawInferenceResult(content='{"action":"tool","tool_name":"capabilities","arguments":{}}')
        waiting.set()
        await release.wait()
        return reply("已根据允许工具完成说明。")

    service, repository, inference, tools = runtime(behavior=behavior)
    conversation, claim = await prepare(service, repository)
    iterator = stream(service, conversation, claim)
    try:
        events = [await next_event(iterator)]
        assert events[0] == ("accepted", {
            "turn_id": str(claim.turn.id), "request_id": str(claim.turn.request_id),
            "replayed": False, "status": "processing",
        })
        await asyncio.wait_for(waiting.wait(), timeout=1)
        while not any(name == "phase" and data.get("call") == 2 for name, data in events):
            events.append(await next_event(iterator))
        assert not release.is_set()
        assert not any(name in {"answer_delta", "done"} for name, _ in events)
        assert any(name == "phase" and data["phase"] == "history" for name, data in events)
        assert [name for name, _ in events if name.startswith("tool_")] == ["tool_started", "tool_finished"]
        finished = next(data for name, data in events if name == "tool_finished")
        assert finished == {"index": 0, "name": "capabilities", "status": "completed", "attempts": 1, "error_code": None}
        assert "result" not in finished and "arguments" not in finished
        release.set()
        events.extend(await collect(iterator))
        persisted = repository.turns[conversation.id][0]
        assert persisted.status == "completed"
        assert answer(events) == persisted.assistant_content
        assert events[-1] == ("done", {"turn": persisted.model_dump(mode="json")})
        assert len(inference.calls) == 2 and len(tools.calls) == 1
    finally:
        await iterator.aclose()


@pytest.mark.asyncio
async def test_no_answer_delta_before_final_storage_commit_and_exact_multiline_reassembly():
    service, repository, _, _ = runtime()
    repository.save_started, repository.save_release = asyncio.Event(), asyncio.Event()
    conversation, claim = await prepare(service, repository)
    iterator = stream(service, conversation, claim)
    pending = None
    try:
        events = [await next_event(iterator)]
        await asyncio.wait_for(repository.save_started.wait(), timeout=1)
        while not any(name == "phase" and data["phase"] == "saving" for name, data in events):
            events.append(await next_event(iterator))
        assert repository.turns[conversation.id][0].status == "processing"
        assert not any(name == "answer_delta" for name, _ in events)
        pending = asyncio.create_task(next_event(iterator))
        await asyncio.sleep(0.01)
        assert not pending.done(), "a pending DB transaction must not expose an answer"
        repository.save_release.set()
        events.append(await pending)
        pending = None
        events.extend(await collect(iterator))
        persisted = repository.turns[conversation.id][0]
        assert answer(events) == persisted.assistant_content
        assert "\n" in answer(events)
        assert events[-1][0] == "done"
    finally:
        if pending is not None:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        await iterator.aclose()


@pytest.mark.asyncio
async def test_terminal_replay_emits_stored_answer_without_any_inference_or_tool_execution():
    service, repository, inference, tools = runtime()
    conversation, claim = await prepare(service, repository)
    original = await service.run_turn(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id, claim=claim)
    replay = await service.prepare_turn(
        tenant_id=TENANT, user_id=USER, conversation_id=conversation.id,
        request_id=claim.turn.request_id, content=claim.turn.user_content,
    )
    count = len(inference.calls)
    events = await collect(stream(service, conversation, replay))
    assert events[0][0] == "accepted" and events[0][1]["replayed"] is True
    assert events[0][1]["status"] == "completed"
    assert not any(name in {"phase", "tool_started", "tool_finished"} for name, _ in events)
    assert answer(events) == original.assistant_content
    assert events[-1] == ("done", {"turn": original.model_dump(mode="json")})
    assert len(inference.calls) == count and tools.calls == []


@pytest.mark.asyncio
async def test_storage_failure_is_sanitized_and_never_emits_uncommitted_answer():
    service, repository, _, _ = runtime()
    repository.save_error = OperationalError("private SQL", {}, RuntimeError("private credential"))
    conversation, claim = await prepare(service, repository)
    events = await collect(stream(service, conversation, claim))
    assert events[-1][0] == "error" and events[-1][1]["code"] == "storage_unavailable"
    assert not any(name in {"answer_delta", "done"} for name, _ in events)
    assert "private" not in json.dumps(events)
    assert repository.turns[conversation.id][0].status == "processing"


@pytest.mark.asyncio
async def test_invalid_raw_model_content_and_fabricated_numbers_never_reach_deltas():
    for raw in ['private credential before {"action":"respond"}', '{"action":"respond","answer":"news_id：fake 9999"}']:
        async def behavior(request, call):
            return RawInferenceResult(content=raw, request_id="invalid-request")
        service, repository, _, tools = runtime(behavior=behavior)
        conversation, claim = await prepare(service, repository)
        events = await collect(stream(service, conversation, claim))
        assert "private credential" not in json.dumps(events) and "9999" not in answer(events) and "fake" not in answer(events)
        assert answer(events) == repository.turns[conversation.id][0].assistant_content
        assert events[-1][0] == "done"
        assert tools.calls == []


@pytest.mark.asyncio
async def test_turn_timeout_is_persisted_before_failed_done_event():
    cancelled = asyncio.Event()

    async def blocked(request, call):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service, repository, _, _ = runtime(behavior=blocked, timeout=0.02)
    conversation, claim = await prepare(service, repository)
    events = await collect(stream(service, conversation, claim))
    persisted = repository.turns[conversation.id][0]
    assert cancelled.is_set()
    assert persisted.status == "failed" and persisted.error_code == "turn_timeout"
    assert events[-1] == ("done", {"turn": persisted.model_dump(mode="json")})
    assert answer(events) == persisted.assistant_content


@pytest.mark.asyncio
async def test_disconnect_cancels_and_awaits_model_then_persists_interruption():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked(request, call):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service, repository, inference, _ = runtime(behavior=blocked)
    conversation, claim = await prepare(service, repository)
    iterator = stream(service, conversation, claim)
    assert (await next_event(iterator))[0] == "accepted"
    await asyncio.wait_for(started.wait(), timeout=1)
    await iterator.aclose()
    persisted = repository.turns[conversation.id][0]
    assert cancelled.is_set()
    assert persisted.status == "failed" and persisted.error_code == "interrupted"
    assert not any(task.get_name() == f"conversation-stream-{claim.turn.id}" for task in asyncio.all_tasks())
    replay = await service.prepare_turn(
        tenant_id=TENANT, user_id=USER, conversation_id=conversation.id,
        request_id=claim.turn.request_id, content=claim.turn.user_content,
    )
    events = await collect(stream(service, conversation, replay))
    assert events[0][1]["replayed"] is True and events[-1][1]["turn"]["error_code"] == "interrupted"
    assert len(inference.calls) == 1


@pytest.mark.asyncio
async def test_disconnect_during_final_save_also_attempts_interruption_without_background_work():
    service, repository, inference, _ = runtime()
    repository.save_started, repository.save_release = asyncio.Event(), asyncio.Event()
    conversation, claim = await prepare(service, repository)
    iterator = stream(service, conversation, claim)
    assert (await next_event(iterator))[0] == "accepted"
    await asyncio.wait_for(repository.save_started.wait(), timeout=1)
    await iterator.aclose()
    persisted = repository.turns[conversation.id][0]
    assert persisted.status == "failed" and persisted.error_code == "interrupted"
    assert repository.finish_calls[-1]["error_code"] == "interrupted"
    assert len(inference.calls) == 1
    assert not any(task.get_name() == f"conversation-stream-{claim.turn.id}" for task in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_heartbeat_keeps_connection_alive_without_generated_answer():
    started = asyncio.Event()

    async def blocked(request, call):
        started.set()
        await asyncio.Event().wait()

    service, repository, _, _ = runtime(behavior=blocked)
    conversation, claim = await prepare(service, repository)
    iterator = stream(service, conversation, claim)
    try:
        events = [await next_event(iterator)]
        await asyncio.wait_for(started.wait(), timeout=1)
        while events[-1][0] != "heartbeat":
            events.append(await next_event(iterator))
        assert not any(name in {"answer_delta", "done"} for name, _ in events)
    finally:
        await iterator.aclose()


def test_sse_json_encoding_prevents_newline_event_or_header_injection():
    text = '安全正文\n\nevent: error\ndata: {"code":"forged"}\r\n'
    frame = encode_event("answer_delta", {"text": text})
    assert frame.count("\nevent: ") == 0
    assert frame.count("\ndata: ") == 1
    assert decode(frame) == ("answer_delta", {"text": text})
    with pytest.raises(ValueError):
        encode_event("done\nevent: error", {})


def api():
    service, repository, inference, tools = runtime()
    application = FastAPI()
    application.include_router(router)
    application.state.settings = SimpleNamespace(data_loop_gateway_token=TOKEN)
    application.state.conversation_service = service
    return TestClient(application), service, repository, inference, tools


def headers(**overrides):
    return {"Authorization": f"Bearer {TOKEN}", "X-Tenant-ID": TENANT,
            "X-User-ID": USER, "X-Hot-News-Roles": "hot-news:read", **overrides}


def test_stream_preflight_auth_scope_schema_and_conflict_remain_http_errors():
    client, _, repository, inference, tools = api()
    created = client.post("/api/v1/conversations", headers=headers(), json={"title": "入口"})
    conversation_id = created.json()["id"]
    path = f"/api/v1/conversations/{conversation_id}/messages/stream"
    body = {"request_id": str(uuid4()), "content": "你好"}
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, headers=headers(**{"X-Hot-News-Roles": "hot-news:decide"}), json=body).status_code == 403
    for field in ["X-Tenant-ID", "X-User-ID"]:
        response = client.post(path, headers=headers(**{field: str(uuid4())}), json=body)
        assert response.status_code == 404 and "text/event-stream" not in response.headers.get("content-type", "")
    for invalid in [{**body, "tenant_id": TENANT}, {**body, "user_id": USER}, {**body, "content": " "}, {**body, "request_id": "invalid"}]:
        response = client.post(path, headers=headers(), json=invalid)
        assert response.status_code == 422 and "text/event-stream" not in response.headers.get("content-type", "")
    from uuid import UUID
    active = ConversationTurnView(
        id=uuid4(), request_id=UUID(body["request_id"]), user_content="你好", status="processing",
        created_at=datetime.now(timezone.utc),
    )
    repository.turns[UUID(conversation_id)].append(active)
    for request_id in [body["request_id"], str(uuid4())]:
        response = client.post(path, headers=headers(), json={**body, "request_id": request_id})
        assert response.status_code == 409 and "text/event-stream" not in response.headers.get("content-type", "")
    assert inference.calls == [] and tools.calls == []
    del client.app.state.conversation_service
    assert client.post(path, headers=headers(), json=body).status_code == 503


def test_stream_preflight_storage_error_is_sanitized_http_503():
    client, _, repository, _, _ = api()
    created = client.post("/api/v1/conversations", headers=headers(), json={"title": "故障"})

    async def unavailable(**kwargs):
        raise OperationalError("private SQL", {}, RuntimeError("private credential"))

    repository.begin_turn = unavailable
    response = client.post(
        f"/api/v1/conversations/{created.json()['id']}/messages/stream", headers=headers(),
        json={"request_id": str(uuid4()), "content": "你好"},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "conversation storage unavailable"}
    assert "text/event-stream" not in response.headers.get("content-type", "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec_version", "start_before_disconnect"),
    [("2.4", True), ("2.4", False), ("2.3", True)],
)
async def test_actual_asgi_disconnect_stops_runner_and_saves_interruption_before_response_exits(spec_version, start_before_disconnect):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked(request, call):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service, repository, inference, _ = runtime(behavior=blocked)
    conversation, claim = await prepare(service, repository)
    iterator = stream(service, conversation, claim)
    response = ConversationStreamingResponse(iterator, media_type="text/event-stream")
    bodies = []

    async def receive():
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] != "http.response.body":
            return
        bodies.append(message["body"])
        if spec_version == "2.4":
            # ASGI 2.4 uses the send exception instead of listening for disconnect.
            if start_before_disconnect:
                await started.wait()
            raise OSError("closed client socket with private transport detail")

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": spec_version},
        "method": "POST", "path": "/api/v1/conversations/test/messages/stream",
        "query_string": b"", "headers": [], "scheme": "http", "http_version": "1.1",
    }
    if spec_version == "2.4":
        with pytest.raises(ClientDisconnect):
            await asyncio.wait_for(response(scope, receive, send), timeout=1)
    else:
        await asyncio.wait_for(response(scope, receive, send), timeout=1)

    assert bodies and decode(bodies[0])[0] == "accepted"
    assert not any(decode(body)[0] in {"answer_delta", "done"} for body in bodies if body)
    if inference.calls:
        assert cancelled.is_set(), "the response must await its runner's actual cancellation"
    persisted = repository.turns[conversation.id][0]
    assert persisted.status == "failed" and persisted.error_code == "interrupted"
    assert len(inference.calls) <= 1
    if start_before_disconnect:
        assert len(inference.calls) == 1
    assert not any(task.get_name() == f"conversation-stream-{claim.turn.id}" for task in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_early_disconnect_of_terminal_replay_never_rewrites_completed_turn():
    service, repository, inference, _ = runtime()
    conversation, claim = await prepare(service, repository)
    original = await service.run_turn(
        tenant_id=TENANT, user_id=USER, conversation_id=conversation.id, claim=claim,
    )
    replay = await service.prepare_turn(
        tenant_id=TENANT, user_id=USER, conversation_id=conversation.id,
        request_id=claim.turn.request_id, content=claim.turn.user_content,
    )
    saved_count, model_count = len(repository.finish_calls), len(inference.calls)
    response = ConversationStreamingResponse(stream(service, conversation, replay))

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("client closed while replaying accepted event")

    with pytest.raises(ClientDisconnect):
        await asyncio.wait_for(
            response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send),
            timeout=1,
        )
    assert repository.turns[conversation.id][0].model_dump() == original.model_dump()
    assert len(repository.finish_calls) == saved_count and len(inference.calls) == model_count
    assert not any(task.get_name() == f"conversation-stream-{claim.turn.id}" for task in asyncio.all_tasks())
