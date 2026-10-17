"""Registry, authenticated capability execution and confirmation wiring contracts."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from temporalio.service import RPCError, RPCStatusCode

from app.api.conversations import router
from app.modules.conversation.candidate_templates import candidate_templates
from app.modules.conversation.receipts import CapabilityReceipt
from app.modules.conversation.service import ConversationAgentService
from app.modules.conversation.tools import ConversationToolDenied, ConversationTools, HOOK_REGISTRY
from app.modules.data_loop.bundle_contract import UnsupportedProductionBundleRuntimeError
from app.modules.data_loop.bundle_runtime import NativeProductionBundleRuntimeRegistry
from app.model_runtime.core import PromptRegistry, PromptSpec, RawInferenceResult
from app.modules.hot_news.analysis.runner import HOT_NEWS_SCENE
from app.repositories.conversation import PostgresConversationRepository
from app.schemas.conversation import ToolTrace
from app.modules.data_loop.orchestrator import DataLoopWorkflowAccessError
from tests.test_conversation_agent import TENANT, USER, runtime
from tests.test_conversation_feedback_hook import feedback_harness
from tests.test_conversation_store import FakeDatabase
from tests.test_conversation_writing_confirm import confirmation_content
from tests.test_conversation_tools_writing import JOB_ID, tools_with_reader
from tests.test_production_bundle import spec


RUN_ID = "33333333-3333-4333-8333-333333333333"
NEWS_ID = "news-verified-source"
CANDIDATE_ID = "44444444-4444-4444-8444-444444444444"
WORKFLOW_ID = "hot-news-data-loop-" + "a" * 64
ALL_GRANTS = frozenset({"writing:admin", "memory:admin", "data-loop:admin", "hot-news:admin"})


def receipt(entity_type, entity_id, *, pending_gate=None):
    return CapabilityReceipt(entity_type=entity_type, entity_id=entity_id,
        status="pending_approval" if pending_gate else "landed", version="1", deep_link=None,
        pending_gate=pending_gate, created_at=datetime.now(timezone.utc)).model_dump(mode="json")


class MemoryPort:
    def __init__(self):
        self.calls = []
        self.bound = asyncio.Event()
        self.block = False
        self.saved_id = str(uuid4())

    async def save_short_term(self, **kwargs):
        return await self._write("short_term", kwargs)

    async def propose_long_term(self, **kwargs):
        return await self._write("long_term_candidate", kwargs)

    async def _write(self, kind, kwargs):
        self.calls.append(kwargs)
        result = {"receipt": receipt("memory", self.saved_id,
            pending_gate="memory_promotion" if kind == "long_term_candidate" else None),
            "memory": {"id": self.saved_id, "content": kwargs["content"], "tier": kind,
                       "status": "pending" if kind == "long_term_candidate" else "active",
                       "version": "1", "effective_status": "pending" if kind == "long_term_candidate" else "active",
                       "not_promoted": kind == "long_term_candidate",
                       "expires_at": (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()},
            "promotion_performed": False}
        if kwargs.get("on_bound"):
            await kwargs["on_bound"](result)
        self.bound.set()
        if self.block:
            await asyncio.Event().wait()
        return result


class DataLoopPort:
    candidate_enabled = True

    def __init__(self):
        self.prepared = []
        self.starts = []
        self.bound = asyncio.Event()
        self.block = False
        self.proof = "server-only-prepared-context-proof"
        self.status_error = None

    async def status(self, *, tenant_id, proposal):
        if self.status_error is not None:
            raise self.status_error
        if tenant_id != TENANT:
            raise DataLoopWorkflowAccessError("Data Loop workflow was not found")
        return {"workflow_id": proposal["workflow_id"], "receipt": receipt("evaluation_run", proposal["workflow_id"]),
                "run": {"workflow_id": proposal["workflow_id"], "phase": "accepted", "gate_passed": None,
                        "waiting_for_approval": False, "evaluation_run_id": None}}

    async def prepare_evaluation(self, *, tenant_id, proposal):
        self.prepared.append((tenant_id, proposal))
        return {"suggested": proposal, "confirmation_fields": {"candidate_id": proposal["candidate_id"],
                    "notice": "按冻结数据评测，人工批准仍在页面完成"},
                "prepared_context": {"proof": self.proof, "candidate_id": proposal["candidate_id"]}}

    async def start_evaluation(self, **kwargs):
        assert kwargs["prepared_context"] == {"proof": self.proof, "candidate_id": kwargs["proposal"]["candidate_id"]}
        self.starts.append(kwargs)
        result = {"workflow_id": WORKFLOW_ID, "workflow_accepted": True,
            "evaluation_policy_version": "hot-news-gate-v1", "receipt": receipt("evaluation_run", WORKFLOW_ID),
            "run": {"workflow_id": WORKFLOW_ID, "phase": "accepted", "gate_passed": None,
                    "waiting_for_approval": False, "evaluation_run_id": None}}
        if kwargs.get("on_bound"):
            await kwargs["on_bound"](result)
        self.bound.set()
        if self.block:
            await asyncio.Event().wait()
        return result


def integrated_tools(*, memory=None, feedback=None, dataloop=None):
    return ConversationTools(hot_news=None, knowledge_store=None, embedding=None, embedding_version="test",
        writing_reader=object(), writing_commands=object(), memory_hooks=memory or MemoryPort(),
        feedback_hooks=feedback or object(), dataloop_hooks=dataloop or DataLoopPort())


def service_harness(*, name, arguments, tools=None):
    async def planner(request, count):
        plan = {"action": "tool", "tool_name": name, "arguments": arguments} if count == 1 else {
            "action": "respond", "answer": "请查看本轮已经保存的工具回执。"}
        return RawInferenceResult(content=json.dumps(plan, ensure_ascii=False), request_id=f"integration-model-{count}")
    base, _, inference, _ = runtime(behavior=planner)
    repository = PostgresConversationRepository(FakeDatabase())
    service = ConversationAgentService(repository=repository, model=base._model,
        tools=tools or integrated_tools(), turn_timeout_seconds=2, max_tool_calls=5)
    return service, repository, inference


async def prepare(service, repository, *, tenant_id=TENANT, content="测试能力", conversation=None):
    conversation = conversation or await repository.create(tenant_id, USER, "能力接线验收")
    identity = dict(tenant_id=tenant_id, user_id=USER, conversation_id=conversation.id)
    claim = await service.prepare_turn(**identity, request_id=uuid4(), content=content)
    return claim, identity


async def seed_source(repository, identity, *, name="read_hot_news", status="completed", run_id=RUN_ID, news_id=NEWS_ID):
    prior = await repository.begin_turn(**identity, request_id=uuid4(), content="读取已有分析")
    source = ToolTrace(name=name, status=status, attempts=1, arguments={}, result={"run_id": run_id,
        "items": [{"news_id": news_id, "rank": 1, "title": "已验证新闻标题"}]})
    return await repository.finish_turn(**identity, turn_id=prior.turn.id, assistant_content="已读取分析来源",
        tools=[source], model_request_ids=["source-model-request"])


@pytest.mark.parametrize("grants,expected", [
    (set(), set()), ({"writing:read"}, {"list_writing_jobs", "writing_job_status"}),
    ({"writing:admin"}, {"list_writing_jobs", "writing_job_status", "start_writing_intent"}),
    ({"memory:read"}, {"memory_status"}),
    ({"memory:write"}, {"save_short_term_memory", "propose_long_term_memory"}),
    ({"memory:admin"}, {"save_short_term_memory", "propose_long_term_memory", "memory_status"}),
    ({"data-loop:read"}, {"evaluation_status"}),
    ({"data-loop:admin"}, {"evaluation_status", "start_data_loop", "propose_bundle_candidate"}),
    ({"data-loop:admin", "hot-news:decide"}, {"evaluation_status", "start_data_loop", "propose_bundle_candidate", "save_feedback_case"}),
    ({"hot-news:admin"}, set()),
])
def test_runtime_hook_list_is_permission_trimmed_from_the_same_registry(grants, expected):
    service, _, _ = service_harness(name="capabilities", arguments={})
    runtime_info = service.runtime_info(tenant_id=TENANT, permissions=frozenset(grants))
    names = set(runtime_info["available_tools"]) & set(HOOK_REGISTRY)
    assert names == expected
    direct = service._tools.available_descriptions(tenant_id=TENANT, permissions=frozenset(grants))
    assert [item["name"] for item in direct] == runtime_info["available_tools"]


@pytest.mark.asyncio
@pytest.mark.parametrize("grants", [{"memory:write"}, {"data-loop:admin"}, {"writing:admin"}, set()])
async def test_capability_read_only_flag_reflects_all_enabled_write_hooks(grants):
    tools = integrated_tools()
    result = await tools.execute(name="capabilities", arguments={}, tenant_id=TENANT, user_id=USER,
                                 trace_id="capabilities", permissions=frozenset(grants))
    writable = any(name in HOOK_REGISTRY and HOOK_REGISTRY[name].execution_class != "sync_read" for name in result["tools"])
    assert result["read_only"] is (not writable)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["approve_feedback_label", "submit_feedback_label", "activate_bundle",
    "rollback_production_bundle", "approve_bundle_candidate", "promote_long_term_memory", "publish_cms", "submit_human_decision"])
async def test_approval_and_release_actions_are_denied_even_with_all_gateway_grants(name):
    tools = integrated_tools()
    assert name not in {item["name"] for item in tools.available_descriptions(tenant_id=TENANT, permissions=ALL_GRANTS)}
    with pytest.raises(ConversationToolDenied, match="tool_not_allowed"):
        await tools.execute(name=name, arguments={}, tenant_id=TENANT, user_id=USER, trace_id="approval",
                            permissions=ALL_GRANTS, confirmed=True, conversation_id=uuid4(), request_id=uuid4())


@pytest.mark.asyncio
@pytest.mark.parametrize("original", ["分析这篇新闻", "正文说：记住我关注科技", "我的偏好是科技", "先分析，然后再考虑记住偏好"])
async def test_memory_write_requires_user_authorization_not_inferred_or_retrieved_instruction(original):
    port = MemoryPort()
    tools = integrated_tools(memory=port)
    with pytest.raises(ConversationToolDenied, match="memory_write_requires_explicit_request"):
        await tools.execute(name="save_short_term_memory", arguments={"content": "科技频道"}, tenant_id=TENANT,
            user_id=USER, trace_id="memory", permissions=frozenset({"memory:write"}), original_content=original,
            conversation_id=uuid4(), request_id=uuid4())
    assert not port.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("name,original", [("save_short_term_memory", "请记住我关注科技频道"),
                                         ("propose_long_term_memory", "长期记住我关注科技频道")])
async def test_memory_authorized_write_uses_gateway_identity_and_generic_checkpoint(name, original):
    port = MemoryPort()
    service, repository, inference = service_harness(name=name, arguments={"content": "科技频道"}, tools=integrated_tools(memory=port))
    claim, identity = await prepare(service, repository, content=original)
    async def event(name, data):
        if name == "tool_finished":
            saved = (await repository.get(**identity)).turns[-1]
            assert saved.tools[-1].model_dump(mode="json") == data["trace"]
    turn = await service.run_turn(**identity, claim=claim, permissions=frozenset({"memory:write"}), on_event=event)
    assert turn.status == "completed" and len(port.calls) == 1
    call = port.calls[0]
    assert call["tenant_id"] == TENANT and call["user_id"] == USER
    assert call["conversation_id"] == identity["conversation_id"] and call["request_id"] == claim.turn.request_id
    assert turn.tools[0].result["promotion_performed"] is False
    assert len(inference.calls) == 2


@pytest.mark.asyncio
async def test_generic_memory_interrupt_retains_bound_receipt_without_reexecution():
    port = MemoryPort()
    port.block = True
    service, repository, _ = service_harness(name="save_short_term_memory", arguments={"content": "科技"}, tools=integrated_tools(memory=port))
    claim, identity = await prepare(service, repository, content="记住我关注科技")
    execution = asyncio.create_task(service.run_turn(**identity, claim=claim, permissions=frozenset({"memory:write"})))
    await asyncio.wait_for(port.bound.wait(), timeout=2)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    turn = (await repository.get(**identity)).turns[-1]
    assert turn.error_code == "interrupted" and turn.tools[-1].result["receipt"]["entity_id"] == port.saved_id
    replay = await service.send(**identity, request_id=claim.turn.request_id, content=claim.turn.user_content,
                                permissions=frozenset({"memory:write"}))
    assert replay.id == turn.id and len(port.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source_name,source_status", [("list_hot_news", "completed"), ("search_knowledge", "completed"),
    ("read_hot_news", "failed"), ("query_hot_news", "denied")])
async def test_feedback_only_consumes_completed_saved_analysis_source(monkeypatch, source_name, source_status):
    hooks, memory, _, database, _, _, arguments, _ = feedback_harness(monkeypatch)
    service, repository, _ = service_harness(name="save_feedback_case", arguments=arguments, tools=integrated_tools(feedback=hooks))
    conversation = await repository.create(memory.tenant_id, USER, "反馈来源验收")
    identity = dict(tenant_id=memory.tenant_id, user_id=USER, conversation_id=conversation.id)
    await seed_source(repository, identity, name=source_name, status=source_status, run_id=str(memory.run_id), news_id=memory.news_id)
    claim, identity = await prepare(service, repository, tenant_id=memory.tenant_id, conversation=conversation)
    turn = await service.run_turn(**identity, claim=claim, permissions=frozenset({"data-loop:admin", "hot-news:decide"}))
    assert turn.tools[0].status == "denied" and turn.tools[0].error_code == "feedback_source_not_in_conversation"
    assert not database.rows


@pytest.mark.asyncio
@pytest.mark.parametrize("source_name", ["read_hot_news", "query_hot_news"])
async def test_feedback_confirm_uses_existing_atomic_services_and_hides_confirmation_context(monkeypatch, source_name):
    hooks, memory, _, database, _, cases, arguments, _ = feedback_harness(monkeypatch)
    service, repository, inference = service_harness(name="save_feedback_case", arguments=arguments, tools=integrated_tools(feedback=hooks))
    conversation = await repository.create(memory.tenant_id, USER, "反馈确认验收")
    identity = dict(tenant_id=memory.tenant_id, user_id=USER, conversation_id=conversation.id)
    await seed_source(repository, identity, name=source_name, run_id=str(memory.run_id), news_id=memory.news_id)
    claim, identity = await prepare(service, repository, tenant_id=memory.tenant_id, conversation=conversation)
    grants = frozenset({"data-loop:admin", "hot-news:decide"})
    async def event(name, data):
        if name == "tool_finished" and data["index"] == 0:
            saved = (await repository.get(**identity)).turns[-1]
            assert saved.tools[-1].model_dump(mode="json") == data["trace"]
            confirmation = saved.tools[-1].result["confirmation"]
            assert any("驳回" in field["value"] for field in confirmation["fields"])
            assert saved.tools[-1].result["prepared_context"]["source_fields"]["production_bundle_version"] == memory.production_bundle_version
            await service.submit_input(**identity, turn_id=claim.turn.id, request_id=UUID(confirmation["input_request_id"]),
                                       content=confirmation_content(confirmation), permissions=grants)
    turn = await service.run_turn(**identity, claim=claim, permissions=grants, on_event=event)
    assert turn.status == "completed" and len(database.rows) == len(cases) == 1
    assert turn.tools[-1].result["label_submitted"] is False and turn.tools[-1].result["label_approved"] is False
    assert turn.tools[-1].result["feedback"]["case_id"] == turn.tools[-1].result["receipt"]["entity_id"]
    assert len(inference.calls) == 1
    token = turn.tools[0].result["confirmation"]["token"]
    assert "prepared_context" not in service._model_trace(turn.tools[0])["result"]
    assert "prepared_context" not in service._context_trace(turn.tools[0])["result"]
    await service.send(**identity, request_id=uuid4(), content="解释刚才保存的案例", permissions=grants)
    prompt = inference.calls[-1].messages[1].content
    assert token not in prompt and "source_fields" not in prompt and "capability-confirm:" not in prompt


@pytest.mark.asyncio
async def test_feedback_cannot_copy_another_conversation_analysis_reference(monkeypatch):
    hooks, memory, _, database, _, _, arguments, _ = feedback_harness(monkeypatch)
    service, repository, _ = service_harness(name="save_feedback_case", arguments=arguments, tools=integrated_tools(feedback=hooks))
    first = await repository.create(memory.tenant_id, USER, "别的会话")
    first_identity = dict(tenant_id=memory.tenant_id, user_id=USER, conversation_id=first.id)
    await seed_source(repository, first_identity, run_id=str(memory.run_id), news_id=memory.news_id)
    claim, identity = await prepare(service, repository, tenant_id=memory.tenant_id,
                                    content=f"将运行 {memory.run_id} 的错误沉淀为反馈")
    turn = await service.run_turn(**identity, claim=claim, permissions=frozenset({"data-loop:admin", "hot-news:decide"}))
    assert turn.tools[0].error_code == "feedback_source_not_in_conversation" and not database.rows


@pytest.mark.asyncio
async def test_model_cannot_select_unmentioned_candidate_for_evaluation():
    port = DataLoopPort()
    service, repository, _ = service_harness(name="start_data_loop", arguments={"candidate_id": CANDIDATE_ID},
                                            tools=integrated_tools(dataloop=port))
    claim, identity = await prepare(service, repository, content="启动评测")
    turn = await service.run_turn(**identity, claim=claim, permissions=frozenset({"data-loop:admin"}))
    assert turn.tools[0].status == "denied" and turn.tools[0].error_code == "candidate_not_in_conversation"
    assert not port.prepared and not port.starts


@pytest.mark.asyncio
@pytest.mark.parametrize("source_name,source_status,allowed", [("propose_bundle_candidate", "completed", True),
    ("propose_bundle_candidate", "denied", False), ("search_knowledge", "completed", False)])
async def test_candidate_reference_requires_actual_completed_candidate_receipt(source_name, source_status, allowed):
    port = DataLoopPort()
    service, repository, _ = service_harness(name="start_data_loop", arguments={"candidate_id": CANDIDATE_ID},
                                            tools=integrated_tools(dataloop=port))
    conversation = await repository.create(TENANT, USER, "候选来源验收")
    identity = dict(tenant_id=TENANT, user_id=USER, conversation_id=conversation.id)
    prior = await repository.begin_turn(**identity, request_id=uuid4(), content="提交候选")
    await repository.finish_turn(**identity, turn_id=prior.turn.id, assistant_content="候选工具结果",
        tools=[ToolTrace(name=source_name, status=source_status, attempts=1, arguments={},
                         result={"candidate": {"candidate_id": CANDIDATE_ID}})], model_request_ids=[])
    claim, identity = await prepare(service, repository, conversation=conversation, content="启动刚才候选的评测")
    grants = frozenset({"data-loop:admin"})
    async def event(name, data):
        if name == "tool_finished" and data["index"] == 0 and data["status"] == "completed":
            turn = (await repository.get(**identity)).turns[-1]
            confirmation = turn.tools[0].result["confirmation"]
            await service.submit_input(**identity, turn_id=claim.turn.id, request_id=UUID(confirmation["input_request_id"]),
                                       content=confirmation_content(confirmation), permissions=grants)
    turn = await service.run_turn(**identity, claim=claim, permissions=grants, on_event=event)
    assert turn.status == "completed"
    if allowed:
        assert len(port.starts) == 1 and turn.tools[-1].result["workflow_accepted"] is True
    else:
        assert not port.prepared and not port.starts and turn.tools[0].error_code == "candidate_not_in_conversation"


@pytest.mark.asyncio
async def test_generic_evaluation_confirm_interrupt_preserves_binding_and_excludes_private_context():
    port = DataLoopPort()
    port.block = True
    service, repository, _ = service_harness(name="start_data_loop", arguments={"candidate_id": CANDIDATE_ID},
                                            tools=integrated_tools(dataloop=port))
    claim, identity = await prepare(service, repository, content=f"启动候选 {CANDIDATE_ID} 的评测")
    grants = frozenset({"data-loop:admin"})
    async def event(name, data):
        if name == "tool_finished" and data["index"] == 0:
            saved = (await repository.get(**identity)).turns[-1]
            proposal = saved.tools[0]
            assert port.proof not in json.dumps(service._model_trace(proposal))
            assert port.proof not in json.dumps(service._context_trace(proposal))
            confirmation = proposal.result["confirmation"]
            await service.submit_input(**identity, turn_id=claim.turn.id, request_id=UUID(confirmation["input_request_id"]),
                                       content=confirmation_content(confirmation), permissions=grants)
    execution = asyncio.create_task(service.run_turn(**identity, claim=claim, permissions=grants, on_event=event))
    await asyncio.wait_for(port.bound.wait(), timeout=2)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    turn = (await repository.get(**identity)).turns[-1]
    assert turn.error_code == "interrupted" and turn.tools[-1].result["workflow_id"] == WORKFLOW_ID
    assert len(port.starts) == 1 and turn.tools[0].result["prepared_context"]["proof"] == port.proof


def test_trusted_candidate_templates_only_allow_registered_unambiguous_manifest_assets():
    baseline, candidate = spec(), spec(model_version="model-v2")
    prompts = PromptRegistry((PromptSpec(HOT_NEWS_SCENE, "prompt-v1", "Controlled registered prompt"),))
    registry = NativeProductionBundleRuntimeRegistry(None, prompts, (baseline, candidate),
        allowed_model_routes=("model-v1", "model-v2"))
    manifest = json.dumps([baseline.model_dump(), candidate.model_dump()])
    templates = candidate_templates(manifest, registry)
    assert templates[("model_version", "model-v2")] == candidate
    assert not any(parameter == "reranker_policy_version" for parameter, _ in templates)
    assert ("analysis_prompt_version", "prompt-v1") not in templates
    assert candidate_templates(json.dumps([baseline.model_dump()]), registry) == {}
    unregistered = spec(model_version="model-v3")
    with pytest.raises(UnsupportedProductionBundleRuntimeError):
        candidate_templates(json.dumps([baseline.model_dump(), unregistered.model_dump()]), registry)


def receipt_client(tools):
    service, _, _ = service_harness(name="capabilities", arguments={}, tools=tools)
    application = FastAPI()
    application.state.settings = SimpleNamespace(data_loop_gateway_token="capability-receipt-test-token")
    application.state.conversation_service = service
    application.include_router(router)
    headers = {"Authorization": "Bearer capability-receipt-test-token", "X-Tenant-ID": TENANT,
               "X-User-ID": USER, "X-Hot-News-Roles": "hot-news:read"}
    return TestClient(application), headers


def test_writing_receipt_http_enforces_independent_read_permission_and_tenant_404():
    tools, jobs = tools_with_reader()
    client, headers = receipt_client(tools)
    path = f"/api/v1/conversations/receipts/writing-jobs/{JOB_ID}"
    with client:
        for wrong in ({}, {"X-Data-Loop-Roles": "writing:read"}, {"X-Hot-News-Roles": "hot-news:read,writing:admin"}):
            assert client.get(path, headers={**headers, **wrong}).status_code == 403
        assert not jobs.calls
        approved = {**headers, "X-Writing-Roles": "writing:read"}
        result = client.get(path, headers=approved)
        assert result.status_code == 200 and result.json()["receipt"]["entity_id"] == JOB_ID
        assert client.get(path, headers={**approved, "X-Tenant-ID": str(uuid4())}).status_code == 404
        assert client.get(path.replace(JOB_ID, "invalid"), headers=approved).status_code == 422
        tools._writing_reader = None
        assert client.get(path, headers=approved).status_code == 503


def test_evaluation_receipt_http_maps_permission_missing_tenant_and_temporal_unavailable():
    port = DataLoopPort()
    tools = integrated_tools(dataloop=port)
    client, headers = receipt_client(tools)
    path = f"/api/v1/conversations/receipts/evaluation-runs/{WORKFLOW_ID}"
    with client:
        assert client.get(path, headers=headers).status_code == 403
        assert client.get(path, headers={**headers, "X-Writing-Roles": "data-loop:read"}).status_code == 403
        approved = {**headers, "X-Data-Loop-Roles": "data-loop:read"}
        result = client.get(path, headers=approved)
        assert result.status_code == 200 and result.json()["run"]["workflow_id"] == WORKFLOW_ID
        assert client.get(path, headers={**approved, "X-Tenant-ID": str(uuid4())}).status_code == 404
        assert client.get(path.replace(WORKFLOW_ID, "invalid"), headers=approved).status_code == 422
        port.status_error = RPCError("test-only unavailable", RPCStatusCode.UNAVAILABLE, b"")
        # The previous successful entity remains valid for the short TTL; use
        # an uncached entity to exercise the transport failure mapping.
        fresh_path = path.replace(WORKFLOW_ID, "hot-news-data-loop-" + "b" * 64)
        assert client.get(fresh_path, headers=approved).status_code == 503
        tools._dataloop_hooks = None
        assert client.get(path, headers=approved).status_code == 503
