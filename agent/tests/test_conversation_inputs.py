"""Active-turn supplements through the real repository and shared JSON/SSE loop."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.conversations import router
from app.modules.conversation.input_state import TurnInputSnapshot
from app.modules.conversation.service import ConversationAgentService
from app.modules.conversation.streaming import StreamingConversation
from app.models.conversation import ConversationTurnRecord
from app.model_runtime.core import RawInferenceResult
from app.repositories.conversation import ConversationConflict, ConversationNotFound, PostgresConversationRepository
from app.schemas.conversation import ToolTrace
from tests.test_conversation_agent import TENANT, USER, Tools, headers, runtime
from tests.test_conversation_store import FakeDatabase


def response(answer):
    return RawInferenceResult(content=json.dumps({"action": "respond", "answer": answer}, ensure_ascii=False))


def harness(*, behavior=None, repository_type=PostgresConversationRepository, tools=None, **options):
    base, _, inference, _ = runtime(behavior=behavior)
    database = FakeDatabase()
    repository = repository_type(database)
    service = ConversationAgentService(repository=repository, model=base._model,
                                       tools=tools or Tools(), **options)
    return service, repository, database, inference


async def prepare(service, repository, content="原始问题"):
    conversation = await repository.create(TENANT, USER, "补充验收")
    claim = await service.prepare_turn(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id,
                                      request_id=uuid4(), content=content)
    identity = dict(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id)
    return conversation, claim, identity


async def append(service, claim, identity, content="补充要求", request_id=None):
    return await service.submit_input(**identity, turn_id=claim.turn.id,
                                      request_id=request_id or uuid4(), content=content)


@pytest.mark.asyncio
async def test_owned_input_receipts_are_idempotent_and_limits_are_frozen():
    service, repository, database, _ = harness(max_inputs=1, max_inputs_chars=12)
    conversation, claim, identity = await prepare(service, repository)
    request_id = uuid4()
    receipt = await append(service, claim, identity, "只讨论科技", request_id)
    assert receipt.status == "pending" and receipt.seq == 1
    assert (await append(service, claim, identity, "只讨论科技", request_id)).id == receipt.id
    service._max_inputs = 100
    with pytest.raises(ConversationConflict, match="count exceeded"):
        await append(service, claim, identity, "第二条")
    with pytest.raises(ConversationConflict, match="different content"):
        await append(service, claim, identity, "不同内容", request_id)
    with pytest.raises(ConversationConflict, match="belongs to a message"):
        await append(service, claim, identity, request_id=claim.turn.request_id)
    with pytest.raises(ConversationConflict, match="belongs to an input"):
        await repository.begin_turn(**identity, request_id=request_id, content="新轮次")
    for scope in ({**identity, "tenant_id": str(uuid4())}, {**identity, "user_id": str(uuid4())}):
        with pytest.raises(ConversationNotFound):
            await append(service, claim, scope)
    assert len(database.lock_queries) >= 5


@pytest.mark.asyncio
async def test_character_deadline_finished_and_missing_turn_boundaries():
    service, repository, database, _ = harness(max_inputs_chars=8)
    _, claim, identity = await prepare(service, repository)
    with pytest.raises(ConversationConflict, match="length exceeded"):
        await append(service, claim, identity, "超出字符总预算")
    with pytest.raises(ConversationNotFound):
        await service.submit_input(**identity, turn_id=uuid4(), request_id=uuid4(), content="补充")
    record = database.records[ConversationTurnRecord][0]
    record.created_at -= timedelta(seconds=61)
    with pytest.raises(ConversationConflict, match="deadline expired"):
        await append(service, claim, identity)
    record.created_at += timedelta(seconds=61)
    request_id = uuid4()
    accepted = await append(service, claim, identity, "补充", request_id)
    await repository.finish_turn(**identity, turn_id=claim.turn.id, assistant_content="失败说明",
                                 tools=[], model_request_ids=[], error_code="interrupted")
    assert (await append(service, claim, identity, "补充", request_id)).id == accepted.id
    with pytest.raises(ConversationConflict, match="no longer accepts"):
        await append(service, claim, identity)
    detail = await repository.get(**identity)
    assert detail.turns[0].inputs[0].status == "failed"


@pytest.mark.asyncio
async def test_finish_claims_late_inputs_before_committing_and_keeps_tool_prefix():
    service, repository, _, _ = harness()
    _, claim, identity = await prepare(service, repository)
    await append(service, claim, identity)
    trace = ToolTrace(name="capabilities", status="completed", attempts=1, arguments={}, result={"limits": "只读"})
    advance = await repository.finish_or_claim(**identity, turn_id=claim.turn.id,
        request_id=claim.turn.request_id, expected_input_seq=0, assistant_content="旧答复",
        tools=[trace], model_request_ids=["model-before-input"])
    assert advance.turn is None and advance.inputs[0].status == "applied"
    snapshot = TurnInputSnapshot.from_turn(claim.turn).with_inputs(advance.inputs)
    assert snapshot.original == "原始问题" and "补充要求：补充要求" in snapshot.message()
    with pytest.raises(ConversationConflict, match="shortened"):
        await repository.finish_or_claim(**identity, turn_id=claim.turn.id,
            request_id=claim.turn.request_id, expected_input_seq=1, assistant_content="新答复",
            tools=[], model_request_ids=[])
    terminal = await repository.finish_or_claim(**identity, turn_id=claim.turn.id,
        request_id=claim.turn.request_id, expected_input_seq=1, assistant_content="新答复",
        tools=[trace], model_request_ids=["model-before-input", "model-after-input"])
    assert terminal.turn.status == "completed" and terminal.turn.user_content == "原始问题"
    assert terminal.turn.inputs[0].status == "applied"
    with pytest.raises(ConversationConflict):
        await repository.claim_inputs(**identity, turn_id=claim.turn.id, request_id=claim.turn.request_id)


@pytest.mark.asyncio
async def test_input_during_inference_discards_stale_tool_plan_and_survives_history():
    started, release = asyncio.Event(), asyncio.Event()
    payloads = []
    async def behavior(request, count):
        payloads.append(json.loads(request.messages[1].content))
        if count == 1:
            started.set()
            await release.wait()
            return RawInferenceResult(content='{"action":"tool","tool_name":"capabilities","arguments":{}}')
        return response("已按补充要求回答。")
    service, repository, _, inference = harness(behavior=behavior)
    conversation, claim, identity = await prepare(service, repository)
    task = asyncio.create_task(service.run_turn(**identity, claim=claim))
    await started.wait()
    await append(service, claim, identity, "只讨论科技")
    release.set()
    turn = await task
    assert len(inference.calls) == 2 and service._tools.calls == []
    assert "只讨论科技" in payloads[1]["message"] and turn.inputs[0].status == "applied"
    assert turn.user_content == "原始问题"
    await service.send(**identity, request_id=uuid4(), content="回顾补充要求")
    assert "只讨论科技" in payloads[-1]["history"][0]["user"]
    assert conversation.id == identity["conversation_id"]


@pytest.mark.asyncio
async def test_input_during_tool_preserves_completed_checkpoint_without_reexecution():
    started, release = asyncio.Event(), asyncio.Event()
    class BlockingTools(Tools):
        async def execute(self, **kwargs):
            started.set()
            await release.wait()
            return await super().execute(**kwargs)
    payloads = []
    async def behavior(request, count):
        payloads.append(json.loads(request.messages[1].content))
        if count == 1:
            return RawInferenceResult(content='{"action":"tool","tool_name":"capabilities","arguments":{}}')
        return response("已经解释。")
    tools = BlockingTools()
    service, repository, _, _ = harness(behavior=behavior, tools=tools)
    _, claim, identity = await prepare(service, repository)
    task = asyncio.create_task(service.run_turn(**identity, claim=claim))
    await started.wait()
    await append(service, claim, identity, "详细解释能力")
    release.set()
    turn = await task
    assert len(tools.calls) == 1 and len(turn.tools) == 1
    assert "详细解释能力" in payloads[1]["message"]
    assert payloads[1]["tool_results"][0]["status"] == "completed"
    assert turn.inputs[0].status == "applied"


@pytest.mark.asyncio
async def test_sse_late_input_replans_before_any_committed_answer_and_replay_is_safe():
    saving, release = asyncio.Event(), asyncio.Event()
    class LateRepository(PostgresConversationRepository):
        async def finish_or_claim(self, **kwargs):
            if not saving.is_set():
                saving.set()
                await release.wait()
            return await super().finish_or_claim(**kwargs)
    async def behavior(request, count):
        payload = json.loads(request.messages[1].content)
        return response("按补充要求回答。" if "只讨论科技" in payload["message"] else "旧答复。")
    service, repository, _, inference = harness(behavior=behavior, repository_type=LateRepository)
    _, claim, identity = await prepare(service, repository)
    events = []
    async def consume():
        async for event in StreamingConversation(service=service, claim=claim, **identity).iter_events():
            events.append(event)
    task = asyncio.create_task(consume())
    await saving.wait()
    await append(service, claim, identity, "只讨论科技")
    release.set()
    await task
    assert len(inference.calls) == 2
    assert any('"phase":"input_applied"' in event for event in events)
    assert all("旧答复" not in event for event in events)
    done = next(event for event in events if event.startswith("event: done"))
    assert '"status":"completed"' in done and "只讨论科技" in done
    replay = await service.prepare_turn(**identity, request_id=claim.turn.request_id, content=claim.turn.user_content)
    assert not replay.acquired
    assert (await service.run_turn(**identity, claim=replay)).inputs[0].content == "只讨论科技"
    assert len(inference.calls) == 2


@pytest.mark.asyncio
async def test_cancellation_marks_pending_supplement_failed_and_keeps_receipt():
    started = asyncio.Event()
    async def behavior(request, count):
        started.set()
        await asyncio.Event().wait()
    service, repository, _, _ = harness(behavior=behavior)
    _, claim, identity = await prepare(service, repository)
    task = asyncio.create_task(service.run_turn(**identity, claim=claim))
    await started.wait()
    receipt = await append(service, claim, identity)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    turn = (await repository.get(**identity)).turns[0]
    assert turn.error_code == "interrupted"
    assert turn.inputs[0].id == receipt.id and turn.inputs[0].status == "failed"


@pytest.mark.asyncio
async def test_planning_budget_bounds_replans_and_new_sql_does_not_merge_supplements():
    async def behavior(request, count):
        return RawInferenceResult(content='{"action":"tool","tool_name":"query_hot_news","arguments":{"question":"原始问题"}}')
    service, repository, _, _ = harness(behavior=behavior, max_planning_steps=1)
    _, claim, identity = await prepare(service, repository)
    await append(service, claim, identity)
    turn = await service.run_turn(**identity, claim=claim, hot_news_query_allowed=True)
    assert turn.error_code == "planning_limit" and turn.tools[0].attempts == 0
    assert turn.tools[0].error_code == "query_supplement_requires_new_turn"
    assert service._tools.calls == [] and turn.inputs[0].status == "failed"


def test_input_api_acceptance_identity_conflicts_and_body_validation():
    service, repository, _, _ = harness()
    application = FastAPI()
    application.include_router(router)
    application.state.settings = SimpleNamespace(data_loop_gateway_token="public-test-token")
    application.state.conversation_service = service
    with TestClient(application) as client:
        conversation = client.post("/api/v1/conversations", headers=headers(), json={}).json()
        async def begin():
            return await service.prepare_turn(tenant_id=TENANT, user_id=USER,
                conversation_id=UUID(conversation["id"]), request_id=uuid4(), content="原始问题")
        claim = client.portal.call(begin)
        url = f"/api/v1/conversations/{conversation['id']}/turns/{claim.turn.id}/inputs"
        body = {"request_id": str(uuid4()), "content": "只讨论科技"}
        accepted = client.post(url, headers=headers(), json=body)
        assert accepted.status_code == 202 and accepted.json()["status"] == "pending"
        assert client.post(url, headers=headers(), json=body).json()["id"] == accepted.json()["id"]
        assert client.post(url, headers=headers(**{"X-User-ID": str(uuid4())}), json=body).status_code == 404
        assert client.post(url, headers=headers(**{"X-Tenant-ID": str(uuid4())}), json=body).status_code == 404
        assert client.post(url, headers=headers(), json={**body, "content": "changed"}).status_code == 409
        assert client.post(url, headers=headers(), json={**body, "tenant_id": "other"}).status_code == 422
