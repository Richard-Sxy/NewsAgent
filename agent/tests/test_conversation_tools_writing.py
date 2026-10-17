"""Writing hooks reuse JobService, enforce gateway grants and persist receipts."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.conversations import router
from app.modules.conversation.local import local_conversation_plan
from app.modules.conversation.plan import ConversationPlan
from app.modules.conversation.tools import ConversationToolDenied, ConversationTools, HOOK_REGISTRY
from app.modules.conversation.writing import ConversationWritingReader
from app.domain.execution import StepType
from app.domain.job_scenario import JobScenario
from app.domain.job_status import JobStatus
from app.model_runtime.core import RawInferenceResult
from app.modules.writing.job import JobNotFoundError
from tests.test_conversation_agent import TENANT, USER, runtime


JOB_ID = "33333333-3333-4333-8333-333333333333"
OTHER_TENANT = "44444444-4444-4444-8444-444444444444"
NOW = datetime.now(timezone.utc)
GRANTS = frozenset({"writing:read"})


def job_row(**changes):
    fields = dict(id=UUID(JOB_ID), tenant_id=UUID(TENANT), topic="AI 芯片周报", requirements={},
        status=JobStatus.RESEARCH_REVIEW, current_step=None, temporal_workflow_id=f"writing:{JOB_ID}",
        scenario=JobScenario.ASSISTED_WRITING, research_retries=0, review_rounds=0,
        progress_percent=20, sections_completed=0, sections_total=0,
        created_at=NOW, updated_at=NOW, version=7)
    return SimpleNamespace(**{**fields, **changes})


class Jobs:
    def __init__(self):
        self.row = job_row()
        self.calls = []
        self.failures = 0

    async def list_for_operations(self, session, **kwargs):
        self.calls.append(kwargs)
        if self.failures:
            self.failures -= 1
            raise ConnectionError("transient database error")
        return ([self.row], 1) if kwargs["tenant_id"] == self.row.tenant_id else ([], 0)

    async def get(self, session, *, tenant_id, job_id):
        self.calls.append({"tenant_id": tenant_id, "job_id": job_id})
        if tenant_id != self.row.tenant_id or job_id != self.row.id:
            raise JobNotFoundError()
        return self.row


class Database:
    @asynccontextmanager
    async def session(self):
        yield object()


def tools_with_reader():
    jobs = Jobs()
    reader = ConversationWritingReader(database=Database(), jobs=jobs)
    return ConversationTools(hot_news=None, knowledge_store=None, embedding=None,
                             embedding_version="test", writing_reader=reader), jobs


async def execute(tools, name="list_writing_jobs", arguments=None, permissions=GRANTS, tenant_id=TENANT):
    return await tools.execute(name=name, arguments=arguments or {}, tenant_id=tenant_id,
                               user_id=USER, trace_id="test", permissions=permissions)


@pytest.mark.asyncio
@pytest.mark.parametrize("permissions", [frozenset(), frozenset({"hot-news:admin"}), frozenset({"data-loop:admin"})])
async def test_hook_permission_refused_before_read(permissions):
    tools, jobs = tools_with_reader()
    with pytest.raises(ConversationToolDenied, match="hook_permission_denied"):
        await execute(tools, permissions=permissions)
    assert not jobs.calls
    assert not {"list_writing_jobs", "writing_job_status"} & {
        item["name"] for item in tools.available_descriptions(tenant_id=TENANT, permissions=permissions)}


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", [
    ("list_writing_jobs", {"limit": True}), ("list_writing_jobs", {"limit": 11}),
    ("list_writing_jobs", {"tenant_id": OTHER_TENANT}),
    ("writing_job_status", {"job_id": "malformed"}),
    ("writing_job_status", {"job_id": JOB_ID, "sql": "select 1"}),
])
async def test_hook_argument_whitelist(name, arguments):
    tools, jobs = tools_with_reader()
    with pytest.raises(ConversationToolDenied, match="tool_arguments_invalid"):
        await execute(tools, name, arguments)
    assert not jobs.calls


@pytest.mark.asyncio
async def test_receipts_use_database_id_version_and_gate_without_identity_or_requirements():
    tools, jobs = tools_with_reader()
    result = await execute(tools, permissions=frozenset({"writing:admin"}))
    receipt = result["receipts"][0]
    assert receipt["entity_id"] == JOB_ID and receipt["version"] == "7"
    assert receipt["status"] == "pending_approval" and receipt["pending_gate"] == "research"
    assert receipt["deep_link"] == f"/jobs?job_id={JOB_ID}"
    assert "tenant_id" not in result["items"][0] and "requirements" not in result["items"][0]
    assert jobs.calls[0] == {"tenant_id": UUID(TENANT), "limit": 5, "offset": 0}
    jobs.row = job_row(status=JobStatus.DRAFTING, current_step=StepType.SECTION_DRAFT)
    current = await execute(tools, "writing_job_status", {"job_id": JOB_ID})
    assert current["receipt"]["status"] == "landed" and current["receipt"]["pending_gate"] is None


@pytest.mark.asyncio
async def test_cross_tenant_status_does_not_return_another_tenant_receipt():
    tools, _ = tools_with_reader()
    result = await execute(tools, "writing_job_status", {"job_id": JOB_ID}, tenant_id=OTHER_TENANT)
    assert result["not_found"] and result["receipt"]["status"] == "not_landed"
    assert result["receipt"]["entity_id"] is None and "job" not in result
    assert (await execute(tools, tenant_id=OTHER_TENANT))["items"] == []


def payload(message, history=None, tool_results=None):
    return dict(message=message, history=history or [], tool_results=tool_results or [],
                available_tools=[{"name": name} for name in HOOK_REGISTRY])


@pytest.mark.parametrize("message", ["写作进度？", "那个任务进度怎样", "进度怎样？"])
def test_local_followup_uses_saved_task_identity(message):
    trace = dict(name="list_writing_jobs", status="completed", result={"items": [{"id": JOB_ID}]})
    plan = ConversationPlan.model_validate_json(local_conversation_plan(payload(message, [{"tools": [trace]}])))
    assert plan.tool_name == "writing_job_status" and plan.arguments == {"job_id": JOB_ID}


def test_local_denial_does_not_claim_success():
    plan = ConversationPlan.model_validate_json(local_conversation_plan(payload("写作进度？",
        tool_results=[dict(name="writing_job_status", status="denied", result={})])))
    assert "没有成功" in plan.answer and "已读取" not in plan.answer


@pytest.mark.asyncio
async def test_read_retry_checkpoint_and_request_replay():
    service, repo, inference, _ = runtime()
    tools, jobs = tools_with_reader()
    service._tools = tools
    jobs.failures = 1
    conversation = await repo.create(TENANT, USER, "写作查询")
    request_id = uuid4()
    seen = []
    async def notify(event, data):
        if event == "tool_finished":
            assert repo.checkpoint_calls[-1].tools[-1].result["receipts"][0]["entity_id"] == JOB_ID
            seen.append(event)
    claim = await service.prepare_turn(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id,
                                      request_id=request_id, content="我有哪些写作任务")
    turn = await service.run_turn(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id,
                                 claim=claim, permissions=GRANTS, on_event=notify)
    assert turn.status == "completed" and turn.tools[0].attempts == 2 and seen == ["tool_finished"]
    before = len(jobs.calls)
    replay = await service.send(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id,
        request_id=request_id, content="我有哪些写作任务", permissions=GRANTS)
    assert replay.id == turn.id and len(jobs.calls) == before
    followup = await service.send(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id,
        request_id=uuid4(), content="那个任务进度怎样", permissions=GRANTS)
    assert followup.tools[0].name == "writing_job_status"
    assert followup.tools[0].result["receipt"]["entity_id"] == JOB_ID
    assert "无执行中的步骤" in followup.assistant_content


@pytest.mark.parametrize("extra_headers,visible", [
    ({}, False), ({"X-Writing-Roles": "writing:read"}, True),
    ({"X-Writing-Roles": "writing:admin"}, True),
    ({"X-Data-Loop-Roles": "writing:admin"}, False),
    ({"X-Hot-News-Roles": "hot-news:read,writing:admin"}, False),
])
def test_http_runtime_and_prompt_use_same_independent_role_grants(extra_headers, visible):
    service, repo, inference, _ = runtime()
    service._tools, jobs = tools_with_reader()
    app = FastAPI()
    app.state.settings = SimpleNamespace(data_loop_gateway_token="writing-read-public-test-token")
    app.state.conversation_service = service
    app.include_router(router)
    headers = {"Authorization": "Bearer writing-read-public-test-token", "X-Tenant-ID": TENANT,
               "X-User-ID": USER, "X-Hot-News-Roles": "hot-news:read", **extra_headers}
    with TestClient(app) as client:
        info = client.get("/api/v1/conversations/runtime", headers=headers).json()
        assert ("list_writing_jobs" in info["available_tools"]) is visible
        conversation = client.post("/api/v1/conversations", json={}, headers=headers).json()
        body = {"request_id": str(uuid4()), "content": "我有哪些写作任务"}
        response = client.post(f"/api/v1/conversations/{conversation['id']}/messages", json=body, headers=headers)
        assert response.status_code == 200
        turn = response.json()
        assert bool(jobs.calls) is visible and bool(turn["tools"]) is visible
        available = json.loads(inference.calls[0].messages[1].content)["available_tools"]
        assert [item["name"] for item in available] == info["available_tools"]


@pytest.mark.asyncio
async def test_model_cannot_bypass_hidden_hooks():
    async def force_hook(request, count):
        return RawInferenceResult(content=ConversationPlan(action="tool", tool_name="list_writing_jobs",
            arguments={}).model_dump_json(), request_id=f"model-{count}", model_version="test")
    service, repo, _, _ = runtime(behavior=force_hook, max_tool_calls=2)
    service._tools, jobs = tools_with_reader()
    conversation = await repo.create(TENANT, USER, "拒绝越权")
    turn = await service.send(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id,
                             request_id=uuid4(), content="写作任务")
    assert not jobs.calls
    assert turn.tools[0].status == "denied" and turn.tools[0].error_code == "hook_permission_denied"
    assert turn.tools[1].error_code == "repeated_tool_call"
