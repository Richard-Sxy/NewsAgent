"""Confirmed writing starts through real conversation inputs and service adapters."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from app.api.conversations import router
from app.conversation.service import ConversationAgentService
from app.conversation.tools import ConversationTools
from app.conversation.writing import ConversationWritingCommands, ConversationWritingConflict
from app.domain.job_status import JobStatus
from app.model_runtime.core import RawInferenceResult
from app.repositories.conversation import ConversationConflict, ConversationNotFound, PostgresConversationRepository
from tests.test_conversation_agent import TENANT, USER, runtime
from tests.test_conversation_store import FakeDatabase
from tests.test_conversation_tools_writing import job_row


ADMIN = frozenset({"writing:admin"})
PROPOSAL = {"topic": "AI 芯片周报", "target_words": 1200, "tone": "explanatory"}
QUESTION = "写一篇 AI 芯片周报，1200 字，解释清楚技术背景"


class WritingDatabase:
    def __init__(self):
        self.commits = 0

    @asynccontextmanager
    async def session(self):
        yield self

    async def commit(self):
        self.commits += 1


class WritingJobs:
    def __init__(self):
        self.rows = {}
        self.calls = []

    async def create_or_get(self, session, *, tenant_id, created_by, request):
        self.calls.append({"tenant_id": tenant_id, "created_by": created_by, "request": request})
        key = tenant_id, request.idempotency_key
        if key in self.rows:
            return self.rows[key], False
        row = job_row(id=uuid4(), tenant_id=tenant_id, topic=request.topic,
                      requirements=request.requirements, scenario=request.scenario,
                      status=JobStatus.CREATED, progress_percent=0, version=1)
        row.temporal_workflow_id = f"news-writing:{tenant_id}:{row.id}"
        self.rows[key] = row
        return row, True


class WritingOrchestrator:
    def __init__(self, database):
        self.database = database
        self.calls = []
        self.started = asyncio.Event()
        self.release = None
        self.failure = None

    async def start_job(self, job):
        assert self.database.commits, "The persisted job must be visible before Temporal starts"
        self.calls.append(job)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.failure is not None:
            raise self.failure


def writing_harness(*, proposal=None, timeout=2, behavior=None, repository_type=PostgresConversationRepository,
                    max_tool_calls=3):
    parameters = dict(PROPOSAL if proposal is None else proposal)
    async def planner(request, count):
        if count == 1:
            plan = {"action": "tool", "tool_name": "start_writing_intent", "arguments": parameters}
        else:
            plan = {"action": "respond", "answer": "写作请求已处理，请查看工具回执。"}
        return RawInferenceResult(content=json.dumps(plan, ensure_ascii=False), request_id=f"writing-model-{count}")
    base, _, inference, _ = runtime(behavior=behavior or planner)
    conversation_database = FakeDatabase()
    repository = repository_type(conversation_database)
    writing_database, jobs = WritingDatabase(), WritingJobs()
    orchestrator = WritingOrchestrator(writing_database)
    commands = ConversationWritingCommands(database=writing_database, jobs=jobs, orchestrator=orchestrator)
    tools = ConversationTools(hot_news=None, knowledge_store=None, embedding=None,
                              embedding_version="test", writing_commands=commands)
    service = ConversationAgentService(repository=repository, model=base._model, tools=tools,
                                       turn_timeout_seconds=timeout, max_tool_calls=max_tool_calls)
    return service, repository, conversation_database, jobs, orchestrator, inference


async def prepare_writing(service, repository, *, content=QUESTION):
    conversation = await repository.create(TENANT, USER, "写作确认验收")
    identity = dict(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id)
    claim = await service.prepare_turn(**identity, request_id=uuid4(), content=content)
    return claim, identity


def confirmation_content(confirmation, *, token=None, action="confirm"):
    return "capability-confirm:" + json.dumps({"token": token or confirmation["token"], "action": action})


async def submit_confirmation(service, claim, identity, confirmation, *, permissions=ADMIN, **changes):
    return await service.submit_input(**identity, turn_id=claim.turn.id,
        request_id=UUID(confirmation["input_request_id"]),
        content=confirmation_content(confirmation, **changes), permissions=permissions)


async def persisted_proposal(repository, identity):
    turn = (await repository.get(**identity)).turns[-1]
    return turn, turn.tools[-1].result["confirmation"]


@pytest.mark.asyncio
async def test_unconfirmed_intent_is_checkpointed_before_event_and_never_starts_job():
    service, repository, _, jobs, orchestrator, _ = writing_harness(timeout=0.2)
    claim, identity = await prepare_writing(service, repository)
    observed = []
    async def event(name, data):
        if name == "tool_finished" and data["index"] == 0:
            persisted, confirmation = await persisted_proposal(repository, identity)
            assert persisted.status == "processing"
            assert data["trace"] == persisted.tools[-1].model_dump(mode="json")
            result = persisted.tools[-1].result
            assert result["receipt"]["status"] == "not_landed"
            assert result["receipt"]["entity_id"] is None
            assert confirmation["state"] == "pending"
            assert confirmation["provenance"] == "model_suggestion"
            assert confirmation["suggested"] == PROPOSAL
            assert confirmation["hook"] == "start_writing_intent"
            assert UUID(confirmation["input_request_id"])
            assert datetime.fromisoformat(confirmation["expires_at"].replace("Z", "+00:00")) > datetime.now(timezone.utc)
            observed.append(result)
    turn = await service.run_turn(**identity, claim=claim, permissions=ADMIN, on_event=event)
    assert observed and not jobs.calls and not orchestrator.calls
    assert turn.status == "completed" and turn.tools[-1].error_code == "confirmation_expired"
    assert turn.tools[0].result["receipt"]["status"] == "not_landed"


@pytest.mark.asyncio
async def test_explicit_confirmation_starts_once_and_same_message_uuid_replays_without_model():
    service, repository, _, jobs, orchestrator, inference = writing_harness()
    claim, identity = await prepare_writing(service, repository)
    receipts = []
    async def event(name, data):
        if name == "tool_finished" and data["index"] == 0:
            _, confirmation = await persisted_proposal(repository, identity)
            first = await submit_confirmation(service, claim, identity, confirmation)
            repeated = await submit_confirmation(service, claim, identity, confirmation)
            assert first.id == repeated.id
            receipts.append(first.id)
    turn = await service.run_turn(**identity, claim=claim, permissions=ADMIN, on_event=event)
    assert turn.status == "completed" and len(jobs.calls) == len(orchestrator.calls) == 1
    assert len(turn.tools) == 2 and turn.tools[0].result["confirmation"]["state"] == "pending"
    accepted = turn.tools[1]
    assert accepted.name == "start_writing_intent" and accepted.result["workflow_accepted"] is True
    assert accepted.result["receipt"]["entity_id"] == str(orchestrator.calls[0].id)
    assert accepted.result["receipt"]["version"] == "1"
    assert turn.inputs[0].id == receipts[0] and turn.inputs[0].status == "applied"
    request = jobs.calls[0]["request"]
    assert str(identity["conversation_id"]) in request.idempotency_key
    assert str(claim.turn.request_id) in request.idempotency_key
    assert request.requirements == {"target_words": 1200, "tone": "explanatory", "brief": QUESTION}
    assert jobs.calls[0]["tenant_id"] == UUID(TENANT) and jobs.calls[0]["created_by"] == UUID(USER)
    model_calls = len(inference.calls)
    replay = await service.send(**identity, request_id=claim.turn.request_id, content=QUESTION, permissions=ADMIN)
    assert replay.id == turn.id and len(inference.calls) == model_calls
    assert len(jobs.calls) == len(orchestrator.calls) == 1
    token = turn.tools[0].result["confirmation"]["token"]
    await service.send(**identity, request_id=uuid4(), content="解释刚才的写作任务", permissions=ADMIN)
    prompt = json.dumps(json.loads(inference.calls[-1].messages[1].content), ensure_ascii=False)
    assert token not in prompt and "capability-confirm:" not in prompt
    assert token in turn.inputs[0].content, "The durable input audit must retain the exact confirmation"


@pytest.mark.asyncio
@pytest.mark.parametrize("proposal", [
    {**PROPOSAL, "tenant_id": TENANT}, {**PROPOSAL, "scenario": "research_package"},
    {**PROPOSAL, "sql": "select 1"}, {**PROPOSAL, "target_words": True},
    {**PROPOSAL, "target_words": 100}, {**PROPOSAL, "tone": "publish_now"},
])
async def test_model_parameter_whitelist_refuses_proposal_without_confirmation(proposal):
    service, repository, _, jobs, orchestrator, _ = writing_harness(proposal=proposal)
    claim, identity = await prepare_writing(service, repository)
    turn = await service.run_turn(**identity, claim=claim, permissions=ADMIN)
    assert turn.tools[0].status == "denied" and turn.tools[0].error_code == "tool_arguments_invalid"
    assert "confirmation" not in turn.tools[0].result and not jobs.calls and not orchestrator.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("permissions", [frozenset(), frozenset({"writing:read"}), frozenset({"hot-news:admin"})])
async def test_hidden_writing_intent_cannot_be_invoked_by_model(permissions):
    service, repository, _, jobs, orchestrator, _ = writing_harness()
    claim, identity = await prepare_writing(service, repository)
    assert "start_writing_intent" not in service.runtime_info(tenant_id=TENANT, permissions=permissions)["available_tools"]
    turn = await service.run_turn(**identity, claim=claim, permissions=permissions)
    assert turn.tools[0].status == "denied" and turn.tools[0].error_code == "hook_permission_denied"
    assert not jobs.calls and not orchestrator.calls


@pytest.mark.asyncio
async def test_confirmation_rejects_wrong_token_request_permissions_and_owner_before_enqueue():
    service, repository, _, jobs, orchestrator, _ = writing_harness()
    claim, identity = await prepare_writing(service, repository)
    rejected = []
    async def event(name, data):
        if name != "tool_finished" or data["index"] != 0:
            return
        _, confirmation = await persisted_proposal(repository, identity)
        with pytest.raises(ConversationConflict):
            await submit_confirmation(service, claim, identity, confirmation, token="wrong-token")
        with pytest.raises(ConversationConflict):
            await service.submit_input(**identity, turn_id=claim.turn.id, request_id=uuid4(),
                content=confirmation_content(confirmation), permissions=ADMIN)
        with pytest.raises(HTTPException) as denied:
            await submit_confirmation(service, claim, identity, confirmation, permissions=frozenset({"writing:read"}))
        assert denied.value.status_code == 403
        for scope in ({**identity, "tenant_id": str(uuid4())}, {**identity, "user_id": str(uuid4())}):
            with pytest.raises(ConversationNotFound):
                await submit_confirmation(service, claim, scope, confirmation)
        assert not (await repository.get(**identity)).turns[-1].inputs
        rejected.append(True)
        await submit_confirmation(service, claim, identity, confirmation)
    turn = await service.run_turn(**identity, claim=claim, permissions=ADMIN, on_event=event)
    assert rejected and turn.status == "completed" and len(orchestrator.calls) == len(jobs.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix,suffix", [("  \n", ""), ("", "\n \t"), (" \t", " \n")])
async def test_whitespace_confirmation_normalizes_before_role_validation_and_idempotency(prefix, suffix):
    service, repository, _, jobs, orchestrator, _ = writing_harness()
    claim, identity = await prepare_writing(service, repository)
    proposed = asyncio.Event()
    async def event(name, data):
        if name == "tool_finished" and data["index"] == 0:
            proposed.set()
    execution = asyncio.create_task(service.run_turn(**identity, claim=claim, permissions=ADMIN, on_event=event))
    try:
        await asyncio.wait_for(proposed.wait(), timeout=2)
        _, confirmation = await persisted_proposal(repository, identity)
        canonical = confirmation_content(confirmation)
        payload = dict(**identity, turn_id=claim.turn.id, request_id=UUID(confirmation["input_request_id"]),
                       content=prefix + canonical + suffix)
        with pytest.raises(HTTPException) as denied:
            await service.submit_input(**payload, permissions=frozenset({"writing:read"}))
        assert denied.value.status_code == 403
        assert not (await repository.get(**identity)).turns[-1].inputs
        assert not jobs.calls and not orchestrator.calls
        accepted = await service.submit_input(**payload, permissions=ADMIN)
        replay = await service.submit_input(**{**payload, "content": canonical}, permissions=ADMIN)
        assert accepted.id == replay.id and accepted.content == canonical
        terminal = await execution
        assert terminal.status == "completed" and len(jobs.calls) == len(orchestrator.calls) == 1
        assert terminal.inputs[0].content == canonical
    finally:
        if not execution.done():
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await execution


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [1, 2])
async def test_confirmation_reserves_proposal_and_execution_slots_within_tool_budget(budget):
    service, repository, _, jobs, orchestrator, _ = writing_harness(max_tool_calls=budget)
    claim, identity = await prepare_writing(service, repository)
    async def event(name, data):
        if name == "tool_finished" and data["index"] == 0 and data["status"] == "completed":
            _, confirmation = await persisted_proposal(repository, identity)
            await submit_confirmation(service, claim, identity, confirmation)
    turn = await service.run_turn(**identity, claim=claim, permissions=ADMIN, on_event=event)
    assert turn.status == "completed" and len(turn.tools) <= budget
    if budget == 1:
        assert turn.tools[0].error_code == "confirmation_tool_budget"
        assert "confirmation" not in turn.tools[0].result
        assert not jobs.calls and not orchestrator.calls
    else:
        assert len(turn.tools) == 2 and turn.tools[-1].result["workflow_accepted"] is True
        assert len(jobs.calls) == len(orchestrator.calls) == 1


@pytest.mark.asyncio
async def test_confirmation_deadline_and_finished_turn_prevent_late_start(monkeypatch):
    service, repository, _, jobs, orchestrator, _ = writing_harness(timeout=0.15)
    claim, identity = await prepare_writing(service, repository)
    proposals = []
    async def event(name, data):
        if name == "tool_finished" and data["index"] == 0:
            _, confirmation = await persisted_proposal(repository, identity)
            proposals.append(confirmation)
            monkeypatch.setattr("app.conversation.service.confirmation_expired", lambda _: True)
            with pytest.raises(ConversationConflict):
                await submit_confirmation(service, claim, identity, confirmation)
    await service.run_turn(**identity, claim=claim, permissions=ADMIN, on_event=event)
    with pytest.raises(ConversationConflict):
        await submit_confirmation(service, claim, identity, proposals[0])
    assert not jobs.calls and not orchestrator.calls


@pytest.mark.asyncio
async def test_ordinary_supplement_invalidates_prior_confirmation_without_starting_it():
    service, repository, _, jobs, orchestrator, inference = writing_harness()
    claim, identity = await prepare_writing(service, repository)
    async def event(name, data):
        if name == "tool_finished" and data["index"] == 0:
            _, confirmation = await persisted_proposal(repository, identity)
            await service.submit_input(**identity, turn_id=claim.turn.id, request_id=uuid4(),
                                       content="先不要写了，只解释研究方法", permissions=ADMIN)
            with pytest.raises(ConversationConflict):
                await submit_confirmation(service, claim, identity, confirmation)
    turn = await service.run_turn(**identity, claim=claim, permissions=ADMIN, on_event=event)
    assert not jobs.calls and not orchestrator.calls
    assert turn.status == "completed" and turn.tools[-1].error_code == "confirmation_changed"
    assert turn.inputs[0].status == "applied" and len(inference.calls) == 1
    assert "重新描述" in turn.assistant_content


@pytest.mark.asyncio
async def test_explicit_cancel_preserves_input_audit_without_starting_job():
    service, repository, _, jobs, orchestrator, _ = writing_harness()
    claim, identity = await prepare_writing(service, repository)
    async def event(name, data):
        if name == "tool_finished" and data["index"] == 0:
            _, confirmation = await persisted_proposal(repository, identity)
            await submit_confirmation(service, claim, identity, confirmation, action="cancel")
    turn = await service.run_turn(**identity, claim=claim, permissions=ADMIN, on_event=event)
    assert turn.status == "completed" and turn.tools[-1].error_code == "confirmation_cancelled"
    assert turn.inputs[0].status == "applied" and not jobs.calls and not orchestrator.calls


@pytest.mark.asyncio
async def test_confirmation_accepts_applied_supplements_before_the_proposal_baseline():
    service, repository, _, jobs, orchestrator, _ = writing_harness()
    claim, identity = await prepare_writing(service, repository)
    await service.submit_input(**identity, turn_id=claim.turn.id, request_id=uuid4(),
                               content="优先解释关键技术", permissions=ADMIN)
    async def event(name, data):
        if name == "tool_finished" and data["index"] == 0:
            _, confirmation = await persisted_proposal(repository, identity)
            assert confirmation["input_seq"] == 1
            await submit_confirmation(service, claim, identity, confirmation)
    turn = await service.run_turn(**identity, claim=claim, permissions=ADMIN, on_event=event)
    assert turn.status == "completed" and len(orchestrator.calls) == 1
    assert len(turn.inputs) == 2 and all(item.status == "applied" for item in turn.inputs)
    assert "优先解释关键技术" in jobs.calls[0]["request"].requirements["brief"]


@pytest.mark.asyncio
async def test_confirmed_workflow_transport_failure_is_not_retried_and_retains_binding():
    service, repository, _, jobs, orchestrator, inference = writing_harness()
    orchestrator.failure = ConnectionError("Temporary Temporal failure")
    claim, identity = await prepare_writing(service, repository)
    async def event(name, data):
        if name == "tool_finished" and data["index"] == 0:
            _, confirmation = await persisted_proposal(repository, identity)
            await submit_confirmation(service, claim, identity, confirmation)
    turn = await service.run_turn(**identity, claim=claim, permissions=ADMIN, on_event=event)
    failed = turn.tools[-1]
    assert failed.status == "failed" and failed.attempts == 1
    assert failed.error_code == "writing_start_unavailable" and failed.result["workflow_accepted"] is False
    assert failed.result["receipt"]["entity_id"] == str(orchestrator.calls[0].id)
    assert len(jobs.rows) == len(orchestrator.calls) == len(inference.calls) == 1
    token = turn.tools[0].result["confirmation"]["token"]
    assert "confirmation" not in service._model_trace(turn.tools[0])["result"]
    assert "confirmation" not in service._context_trace(turn.tools[0])["result"]
    assert service._model_trace(failed)["result"]["job_id"] == failed.result["job_id"]
    await service.send(**identity, request_id=uuid4(), content="解释保存后的任务状态", permissions=ADMIN)
    prompt = json.dumps(json.loads(inference.calls[-1].messages[1].content), ensure_ascii=False)
    assert token not in prompt and "capability-confirm:" not in prompt


@pytest.mark.asyncio
async def test_writing_adapter_checks_same_key_payload_and_accepts_without_completion_wait():
    database, jobs = WritingDatabase(), WritingJobs()
    orchestrator = WritingOrchestrator(database)
    commands = ConversationWritingCommands(database=database, jobs=jobs, orchestrator=orchestrator)
    identity = dict(tenant_id=TENANT, user_id=USER, conversation_id=uuid4(), request_id=uuid4(),
                    original_content=QUESTION, proposal=PROPOSAL)
    bound = []
    async def binding(value):
        assert database.commits and value["workflow_accepted"] is False
        assert not orchestrator.calls
        bound.append(value)
    accepted = await commands.start(**identity, on_bound=binding)
    assert accepted["workflow_accepted"] is True and bound[0]["job_id"] == accepted["job_id"]
    replay = await commands.start(**identity)
    assert replay["job_id"] == accepted["job_id"] and len(jobs.rows) == 1
    before = len(orchestrator.calls)
    with pytest.raises(ConversationWritingConflict):
        await commands.start(**{**identity, "proposal": {**PROPOSAL, "target_words": 1500}})
    assert len(orchestrator.calls) == before
    with pytest.raises(ValidationError):
        await commands.start(**{**identity, "proposal": {**PROPOSAL, "tenant_id": TENANT}})


@pytest.mark.asyncio
async def test_http_confirmation_uses_owned_turn_input_and_independent_gateway_permission():
    proposed = asyncio.Event()
    class ProposalRepository(PostgresConversationRepository):
        async def checkpoint_turn(self, **kwargs):
            result = await super().checkpoint_turn(**kwargs)
            if kwargs["tools"][-1].result.get("confirmation"):
                proposed.set()
            return result
    service, repository, _, jobs, orchestrator, _ = writing_harness(repository_type=ProposalRepository)
    conversation = await repository.create(TENANT, USER, "HTTP 写作确认")
    identity = dict(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id)
    application = FastAPI()
    application.state.settings = SimpleNamespace(data_loop_gateway_token="writing-confirm-public-token")
    application.state.conversation_service = service
    application.include_router(router)
    headers = {"Authorization": "Bearer writing-confirm-public-token", "X-Tenant-ID": TENANT,
               "X-User-ID": USER, "X-Hot-News-Roles": "hot-news:read", "X-Writing-Roles": "writing:admin"}
    path = f"/api/v1/conversations/{identity['conversation_id']}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test", headers=headers) as client:
        execution = asyncio.create_task(client.post(path + "/messages", json={
            "request_id": str(uuid4()), "content": QUESTION}))
        try:
            await asyncio.wait_for(proposed.wait(), timeout=2)
            pending, confirmation = await persisted_proposal(repository, identity)
            body = {"request_id": confirmation["input_request_id"], "content": confirmation_content(confirmation)}
            input_path = path + f"/turns/{pending.id}/inputs"
            wrong = {**headers, "X-Writing-Roles": "writing:read", "X-Hot-News-Roles": "hot-news:read,writing:admin"}
            denied = await client.post(input_path, json=body, headers=wrong)
            assert denied.status_code == 403
            assert not (await repository.get(**identity)).turns[-1].inputs and not jobs.calls
            other_user = await client.post(input_path, json=body, headers={**headers, "X-User-ID": str(uuid4())})
            assert other_user.status_code == 404
            accepted = await client.post(input_path, json=body)
            assert accepted.status_code == 202
            terminal = await execution
            assert terminal.status_code == 200
            assert terminal.json()["status"] == "completed" and len(orchestrator.calls) == 1
            replay = await client.post(input_path, json=body)
            assert replay.status_code == 202 and replay.json()["id"] == accepted.json()["id"]
            history = await client.get(path)
            assert history.json()["turns"][-1]["tools"][-1]["result"]["job_id"] == str(orchestrator.calls[0].id)
        finally:
            if not execution.done():
                execution.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await execution

