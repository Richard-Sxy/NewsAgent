"""Opt-in active-turn input acceptance on the existing isolated demo database.

Random identities and retained audit rows; no DDL, cleanup or existing-row edits.
"""

import asyncio
import json
from uuid import uuid4

import pytest

from app.conversation.service import ConversationAgentService
from app.model_runtime.core import RawInferenceResult
from app.repositories.conversation import ConversationConflict, ConversationNotFound, PostgresConversationRepository
from tests.test_conversation_agent import runtime
from tests.test_conversation_postgres import DATABASE_URL, _conversation, database, identity


pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not DATABASE_URL, reason="requires explicit isolated conversation PostgreSQL")]


@pytest.mark.asyncio
async def test_postgres_concurrent_input_receipt_claim_and_terminal_replay(database, identity):
    repository = PostgresConversationRepository(database)
    conversation = await _conversation(repository, identity)
    tenant_id, user_id = identity
    scope = dict(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation.id)
    claim = await repository.begin_turn(**scope, request_id=uuid4(), content="原始问题")
    receipt_id = uuid4()
    async def submit():
        return await repository.enqueue_input(**scope, turn_id=claim.turn.id,
            request_id=receipt_id, content="只讨论科技", max_inputs=3,
            max_input_chars=8000, stale_after_seconds=60)
    receipts = await asyncio.gather(*(submit() for _ in range(6)))
    assert len({receipt.id for receipt in receipts}) == 1
    for wrong in ({**scope, "tenant_id": str(uuid4())}, {**scope, "user_id": str(uuid4())}):
        with pytest.raises(ConversationNotFound):
            await repository.claim_inputs(**wrong, turn_id=claim.turn.id, request_id=claim.turn.request_id)
    inputs = await repository.claim_inputs(**scope, turn_id=claim.turn.id, request_id=claim.turn.request_id)
    assert len(inputs) == 1 and inputs[0].status == "applied"
    assert await repository.claim_inputs(**scope, turn_id=claim.turn.id, request_id=claim.turn.request_id) == []
    result = await repository.finish_or_claim(**scope, turn_id=claim.turn.id,
        request_id=claim.turn.request_id, expected_input_seq=1, assistant_content="按补充要求回答。",
        tools=[], model_request_ids=[])
    assert result.turn.status == "completed" and result.turn.inputs[0].status == "applied"
    assert (await submit()).id == receipts[0].id
    with pytest.raises(ConversationConflict):
        await repository.enqueue_input(**scope, turn_id=claim.turn.id, request_id=uuid4(),
            content="终态迟到要求", max_inputs=3, max_input_chars=8000, stale_after_seconds=60)
    replay = await repository.begin_turn(**scope, request_id=claim.turn.request_id, content="原始问题")
    assert not replay.acquired and len(replay.turn.inputs) == 1


@pytest.mark.asyncio
async def test_postgres_different_inputs_have_unique_order_and_failure_keeps_both(database, identity):
    repository = PostgresConversationRepository(database)
    conversation = await _conversation(repository, identity)
    tenant_id, user_id = identity
    scope = dict(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation.id)
    claim = await repository.begin_turn(**scope, request_id=uuid4(), content="原始问题")
    async def submit(content):
        return await repository.enqueue_input(**scope, turn_id=claim.turn.id, request_id=uuid4(),
            content=content, max_inputs=3, max_input_chars=8000, stale_after_seconds=60)
    receipts = await asyncio.gather(submit("补充科技"), submit("补充财经"))
    assert {receipt.seq for receipt in receipts} == {1, 2}
    applied = await repository.claim_inputs(**scope, turn_id=claim.turn.id, request_id=claim.turn.request_id)
    assert [item.seq for item in applied] == [1, 2]
    pending = await submit("最后补充")
    turn = await repository.finish_turn(**scope, turn_id=claim.turn.id, assistant_content="本轮超时。",
                                        tools=[], model_request_ids=[], error_code="turn_timeout")
    assert [item.seq for item in turn.inputs] == [1, 2, 3]
    assert all(item.status == "failed" and item.error_code == "turn_timeout" for item in turn.inputs)
    assert turn.inputs[-1].id == pending.id
    assert turn.inputs[0].applied_at is not None and turn.inputs[-1].applied_at is None


@pytest.mark.asyncio
async def test_postgres_finish_and_input_race_never_accepts_an_unprocessed_receipt(database, identity):
    repository = PostgresConversationRepository(database)
    conversation = await _conversation(repository, identity)
    tenant_id, user_id = identity
    scope = dict(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation.id)
    claim = await repository.begin_turn(**scope, request_id=uuid4(), content="原始问题")
    ready = asyncio.Event()
    async def submit():
        await ready.wait()
        return await repository.enqueue_input(**scope, turn_id=claim.turn.id, request_id=uuid4(),
            content="迟到要求", max_inputs=3, max_input_chars=8000, stale_after_seconds=60)
    async def finish():
        await ready.wait()
        return await repository.finish_or_claim(**scope, turn_id=claim.turn.id,
            request_id=claim.turn.request_id, expected_input_seq=0, assistant_content="旧答复。",
            tools=[], model_request_ids=[])
    tasks = [asyncio.create_task(submit()), asyncio.create_task(finish())]
    ready.set()
    receipt, advance = await asyncio.gather(*tasks, return_exceptions=True)
    assert not isinstance(advance, Exception)
    if advance.turn is not None:
        assert isinstance(receipt, ConversationConflict)
        assert advance.turn.inputs == []
    else:
        assert not isinstance(receipt, Exception)
        assert len(advance.inputs) == 1 and advance.inputs[0].id == receipt.id
        advance = await repository.finish_or_claim(**scope, turn_id=claim.turn.id,
            request_id=claim.turn.request_id, expected_input_seq=1, assistant_content="新答复。",
            tools=[], model_request_ids=[])
        assert advance.turn.inputs[0].status == "applied"


@pytest.mark.asyncio
async def test_postgres_supplement_during_model_is_seen_by_next_plan_and_history(database, identity):
    repository = PostgresConversationRepository(database)
    started, release = asyncio.Event(), asyncio.Event()
    payloads = []
    async def behavior(request, count):
        payloads.append(json.loads(request.messages[1].content))
        if count == 1:
            started.set()
            await release.wait()
        answer = "按补充要求回答。" if "只讨论科技" in payloads[-1]["message"] else "旧答复。"
        return RawInferenceResult(content=json.dumps({"action": "respond", "answer": answer}, ensure_ascii=False))
    base, _, inference, tools = runtime(behavior=behavior)
    service = ConversationAgentService(repository=repository, model=base._model, tools=tools)
    conversation = await _conversation(repository, identity)
    tenant_id, user_id = identity
    scope = dict(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation.id)
    claim = await service.prepare_turn(**scope, request_id=uuid4(), content="原始问题")
    task = asyncio.create_task(service.run_turn(**scope, claim=claim))
    await asyncio.wait_for(started.wait(), 5)
    await service.submit_input(**scope, turn_id=claim.turn.id, request_id=uuid4(), content="只讨论科技")
    release.set()
    turn = await asyncio.wait_for(task, 10)
    assert turn.status == "completed" and turn.assistant_content == "按补充要求回答。"
    assert len(inference.calls) == 2 and turn.inputs[0].status == "applied"
    assert (await repository.get(**scope)).turns[0].user_content == "原始问题"
    await service.send(**scope, request_id=uuid4(), content="回顾补充要求")
    assert "只讨论科技" in payloads[-1]["history"][0]["user"]
