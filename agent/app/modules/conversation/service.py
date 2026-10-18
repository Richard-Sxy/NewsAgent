"""
聊天总协调入口。ConversationAgentService 负责读取历史、调用模型、执行工具、限制超时与调用次数、保存工具检查点和最终答复。
按长度加载已完成历史，达到预算后压缩并保存摘要。
重点看 prepare_turn() 和 run_turn()。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable
from uuid import UUID
from fastapi import HTTPException

from app.model_runtime.core import model_attempt_context
from app.observability.telemetry import operation, set_safe_attributes
from opentelemetry import trace
from app.observability.decorators import traced
from app.modules.conversation.plan import ConversationPlan
from app.modules.conversation.input_state import TurnInputSnapshot
from app.modules.conversation.confirmation import (parse_confirmation, new_confirmation,
    matches_confirmation, confirmation_expired)
from app.modules.conversation.receipts import CapabilityReceipt
from app.modules.conversation.local import has_dated_news_request
from app.modules.conversation.query_understanding import QueryResolutionError
from app.modules.conversation.context import LengthBasedContext, ContextBudgetExceeded, POLICY_VERSION
from app.modules.conversation.tools import ConversationToolDenied, TOOL_DESCRIPTIONS, TOOL_NAMES, render_tool_results, hook_permitted
from app.modules.conversation.writing import ConversationWritingConflict
from app.modules.data_loop.production_bundle import ProductionBundleRuleViolation, canonical_sha256
from app.modules.conversation.related_articles import related_article_deadline
from app.modules.data_analysis.runner import AnalysisExecutionError
from app.domain.errors import AgentOutputValidationError, HotNewsPersistenceError
from app.model_runtime.agent_client import model_request_context
from app.model_runtime.http import ModelTransportError
from app.repositories.conversation import ConversationConflict, ConversationNotFound
from app.schemas.conversation import ToolTrace, AppendConversationInputRequest
from app.sql_assistant.input_boundary import SqlAssistantQuestionError


ProgressCallback = Callable[[str, dict], Awaitable[None]]

"""对话Agent服务链路"""
class ConversationAgentService:
    def __init__(self, *, repository, model, tools, context_max_chars: int = 32000,
                 context_recent_chars: int = 12000, context_summary_chars: int = 4000,
                 context_threshold_ratio: float = 0.8, context_max_compaction_attempts: int = 2,
                 max_tool_calls: int = 3, max_planning_steps: int = 8, max_inputs: int = 3, max_inputs_chars: int = 8000,
                 turn_timeout_seconds: float = 60, local_simulation: bool = False,
                 runtime_metadata: dict | None = None):
        limits = {
            "max_planning_steps": max_planning_steps,
            "max_inputs": max_inputs,
            "max_input_chars": max_inputs_chars,
        }
        if any(type(value) is not int or value <= 0 for value in limits.values()):
            raise ValueError("conversation input must be positive integers")
        self.repository = repository
        self._model = model
        self._tools = tools
        self._context_options = dict(max_chars=context_max_chars, recent_chars=context_recent_chars,
                                     summary_chars=context_summary_chars, threshold_ratio=context_threshold_ratio,
                                     max_compaction_attempts=context_max_compaction_attempts)
        # Validate before accepting a request. Each request owns its own buffer.
        LengthBasedContext(repository=repository, model=model, trace_projection=self._context_trace,
                           **self._context_options)
        self._max_tool_calls = max_tool_calls
        self._max_planning_steps = max_planning_steps
        self._max_inputs = max_inputs
        self._max_input_chars = max_inputs_chars
        self._timeout = turn_timeout_seconds
        self._local = local_simulation
        self._runtime_metadata = dict(runtime_metadata or {})
        self._runtime_metadata.update(context_policy=POLICY_VERSION, **self._context_options)
        self._runtime_metadata.update(**limits, input_stale_after_seconds=max(1, math.ceil(self._timeout)))

    @property
    def turn_timeout_seconds(self) -> float:
        return self._timeout

    def enable_hot_news_query(self, runner):
        self._tools.enable_hot_news_query(runner)

    def _available_tools(self, tenant_id, permissions, hot_news_query_allowed):
        if hasattr(self._tools, "available_descriptions"):
            return self._tools.available_descriptions(tenant_id=tenant_id, permissions=permissions,
                hot_news_query_allowed=hot_news_query_allowed)
        available = list(getattr(self._tools, "descriptions", TOOL_DESCRIPTIONS))
        query = getattr(self._tools, "query_description", lambda _: None)(tenant_id)
        if hot_news_query_allowed and query:
            available.append(query)
        return available

    def runtime_info(self, *, tenant_id, hot_news_query_allowed=False, permissions=frozenset()):
        description = getattr(self._tools, "query_description", lambda _: None)(tenant_id)
        analysis = getattr(self._tools, "analysis_description", lambda: None)()
        return {"model_provider": "local" if self._local else self._runtime_metadata.get("model_provider", "unknown"),
                "model_route": self._runtime_metadata.get("model_route", "unknown"),
                "prompt_version": self._runtime_metadata.get("prompt_version", "unknown"),
                "query_enabled": bool(description and hot_news_query_allowed),
                "query_scope": description if description and hot_news_query_allowed else None,
                "data_analysis": analysis,
                "async_admission": self._runtime_metadata.get("async_admission", {
                    "enabled": False, "budget": 2, "queue_limit": 100, "scope": "conversation_async_hooks"}),
                "available_tools": [item["name"] for item in self._available_tools(
                    tenant_id, permissions, hot_news_query_allowed)]}

    async def send(self, *, tenant_id: str, user_id: str, conversation_id: UUID, request_id: UUID, content: str,
                   hot_news_query_allowed: bool = False, permissions=frozenset()):
        claim = await self.prepare_turn(tenant_id=tenant_id, user_id=user_id,
                                        conversation_id=conversation_id, request_id=request_id, content=content)
        return await self.run_turn(tenant_id=tenant_id, user_id=user_id,
                                   conversation_id=conversation_id, claim=claim, hot_news_query_allowed=hot_news_query_allowed,
                                   permissions=permissions)

    async def prepare_turn(self, *, tenant_id: str, user_id: str, conversation_id: UUID,
                           request_id: UUID, content: str):
        """在流式标头之前声明，保留正常的所有权/冲突错误。"""
        claim = await self.repository.begin_turn(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id,
            request_id=request_id, content=content, stale_after_seconds=int(self._timeout) + 30,
            runtime_metadata=self._runtime_metadata,
        )
        if not claim.acquired and claim.turn.status == "processing":
            raise ConversationConflict("message_processing")
        return claim

    async def submit_input(self, *, tenant_id: str, user_id: str, conversation_id: UUID,
                           turn_id: UUID, request_id: UUID, content: str, permissions=frozenset()):
        """Append to the active owned turn; retries return the original receipt."""
        content = AppendConversationInputRequest(request_id=request_id, content=content).content
        try:
            control = parse_confirmation(content)
        except ValueError as exc:
            raise ConversationConflict("confirmation_invalid") from exc
        if control is not None:
            detail = await self.repository.get(tenant_id=tenant_id, user_id=user_id,
                conversation_id=conversation_id, stale_after_seconds=int(self._timeout) + 30)
            turn = next((item for item in detail.turns if item.id == turn_id), None) if detail else None
            if turn is None:
                raise ConversationNotFound("confirmation_turn_not_found")
            confirmation = next((trace.result["confirmation"] for trace in reversed(turn.tools)
                if isinstance(trace.result.get("confirmation"), dict) and
                matches_confirmation(control, trace.result["confirmation"])), None)
            if confirmation is None or confirmation.get("input_request_id") != str(request_id):
                raise ConversationConflict("confirmation_token_mismatch")
            spec = getattr(self._tools, "hook_spec", lambda _: None)(confirmation.get("hook"))
            if spec is None or not hook_permitted(spec, permissions):
                raise HTTPException(status_code=403, detail="confirmation permission denied")
            previous = next((item for item in turn.inputs if item.request_id == request_id), None)
            if previous is None and any(item.seq > confirmation.get("input_seq", 0)
                and str(item.request_id) != confirmation["input_request_id"] for item in turn.inputs):
                raise ConversationConflict("confirmation_changed")
            if previous is None and (turn.status != "processing" or confirmation_expired(confirmation)):
                raise ConversationConflict("confirmation_expired")
        return await self.repository.enqueue_input(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id,
            turn_id=turn_id, request_id=request_id, content=content,
            max_inputs=self._max_inputs, max_input_chars=self._max_input_chars,
            stale_after_seconds=max(1, math.ceil(self._timeout)),
        )

    @traced("conversation.turn", identity={"conversation_id": "newsagent.conversation_id",
        "claim.turn.id": "newsagent.turn_id"}, result_status=True)
    async def run_turn(self, *, tenant_id: str, user_id: str, conversation_id: UUID,
                       claim, on_event: ProgressCallback | None = None, hot_news_query_allowed: bool = False,
                       permissions=frozenset()):
        """claim=TurnClaim(acquired=bool是否可以执行, turn包含这一轮的对话)  on_event异步回调函数，这边调用的对象是 publish() 函数，用这个函数异步处理。"""
        if not claim.acquired:
            return claim.turn
        snapshot = TurnInputSnapshot.from_turn(claim.turn)
        content = snapshot.message()
        traces = []
        model_ids = []
        error_code = None
        answer = ""
        turn_deadline = asyncio.get_running_loop().time() + self._timeout
        try:
            async with asyncio.timeout(self._timeout):
                await self._notify(on_event, "phase", {"phase": "history", "message": "正在恢复本会话上下文。"})
                # 处理上下文长度。
                context = LengthBasedContext(repository=self.repository, model=self._model,
                                             trace_projection=self._context_trace, **self._context_options)
                # 获取工具对象。
                available = self._available_tools(tenant_id, permissions, hot_news_query_allowed)
                # 定义一个函数内的异步函数，传入之前的异步函数。
                async def notify(event, data):
                    await self._notify(on_event, event, data)
                async def apply_inputs(values=None):
                    nonlocal snapshot, content
                    if values is None:
                        values = await self.repository.claim_inputs(
                            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id,
                            turn_id=claim.turn.id, request_id=claim.turn.request_id,
                        )
                    if not values:
                        return False
                    snapshot = snapshot.with_inputs(values)
                    content = snapshot.message()
                    await notify("phase", {"phase": "input_applied", "input_seq": snapshot.input_seq,
                                           "message": "补充要求已保存，将用于下一次规划；已完成工具结果仍保留。"})
                    return True
                async def finish_success(text):
                    await notify("phase", {"phase": "saving", "message": "正在保存本轮处理结果。"})
                    advance = await self.repository.finish_or_claim(
                        tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id,
                        turn_id=claim.turn.id, request_id=claim.turn.request_id,
                        expected_input_seq=snapshot.input_seq,
                        assistant_content=self._render_answer(text, traces, tenant_id),
                        tools=traces, model_request_ids=model_ids,
                    )
                    if advance.turn is not None:
                        return advance.turn
                    await apply_inputs(advance.inputs)
                    return None
                def input_payload():
                    return {"message": content, "history": [],
                            "tool_results": [self._model_trace(item) for item in traces],
                            "available_tools": available, "tools_remaining": self._max_tool_calls - len(traces)}
                def measure(history):
                    payload = input_payload()
                    payload["history"] = history   # 这边传入 chat_history()
                    return self._model.input_chars(mode="conversation", payload=payload, output_type=ConversationPlan)
                seen_calls = set()
                # 这边是模型请求文本。这边的 respository() 调用的是 PostgresConversationRepository，具体细节都存储到 Postgre 当中。
                with model_request_context(tenant_id=tenant_id, trace_id=str(claim.turn.id)):
                    history = await context.load(tenant_id=tenant_id, user_id=user_id,
                        conversation_id=conversation_id, claim=claim, model_ids=model_ids,
                        budget=context.max_chars, notify=notify, measure=measure)
                    # 这边执行工具调用，外部重试循环。
                    for call_index in range(self._max_planning_steps):
                        await apply_inputs()
                        payload = input_payload()   # 加载对话信息。
                        history = await context.fit(budget=context.max_chars,
                                                    model_ids=model_ids, traces=traces, notify=notify, measure=measure)
                        payload["history"] = history
                        if self._model.input_chars(mode="conversation", payload=payload,
                                                   output_type=ConversationPlan) > context.max_chars:
                            raise ContextBudgetExceeded()
                        await self._notify(on_event, "phase", {"phase": "model", "call": call_index + 1,
                                                              "message": "正在生成并校验本轮计划。"})   # 校验 on_event() 函数是否存在。
                        result = await self._infer(payload, on_event=on_event, call=call_index + 1)
                        if result.request_id:
                            model_ids.append(result.request_id)
                        plan = result.value
                        # A plan inferred before an accepted supplement must not
                        # execute a tool or commit an obsolete answer.
                        if await apply_inputs():
                            continue
                        # 如果意图是 respond 表示想回答，就回复内容。
                        # 这边用正则匹配 re 正则函数类 \d 表示任意匹配字符 ｜ 表示或者 plan.answer 表示需要匹配的对象 re.I 表示忽略大小写匹配
                        if plan.action == "respond":
                            if re.search(r"\d|news_id|run_id|query_id|chunk_id", plan.answer, flags=re.I):
                                answer = ("以下为本轮工具结果的权威数据快照，模型文字说明因包含数字已省略。"
                                          if traces else "本轮回答包含未经工具核实的数字，已按安全约束省略。")
                            else:
                                answer = plan.answer
                            terminal = await finish_success(answer)
                            if terminal is not None:
                                return terminal
                            continue
                        if len(traces) >= self._max_tool_calls:
                            answer = "本轮工具调用已达到上限，可以在下一轮继续明确追问。"
                            terminal = await finish_success(answer)
                            if terminal is not None:
                                return terminal
                            continue
                        # dumps 表示把 Python 对象转换成 JSON 字符串，这边的对象是 list = [name, arguments]
                        key = json.dumps([plan.tool_name, plan.arguments], ensure_ascii=False, sort_keys=True)
                        tool_index = len(traces)  # 获取轨迹长度
                        await self._notify(on_event, "tool_started", {"index": tool_index, "name": plan.tool_name})
                        if key in seen_calls:
                            traces.append(ToolTrace(name=plan.tool_name, status="denied", attempts=0,
                                                    arguments=plan.arguments, result={}, error_code="repeated_tool_call"))
                        elif (plan.tool_name == "analyze_hot_news_data" and any(
                              value not in self._analysis_run_ids(history, traces)
                              for value in (plan.arguments.get("run_id"), plan.arguments.get("reference_run_id"))
                              if value is not None)):
                            seen_calls.add(key)
                            traces.append(ToolTrace(name=plan.tool_name, status="denied", attempts=0,
                                                    arguments={}, result={}, error_code="analysis_run_not_in_conversation"))
                        elif (plan.tool_name == "save_feedback_case" and
                              (plan.arguments.get("run_id"), plan.arguments.get("news_id"))
                              not in self._feedback_targets(history, traces)):
                            seen_calls.add(key)
                            traces.append(ToolTrace(name=plan.tool_name, status="denied", attempts=0,
                                arguments={}, result={}, error_code="feedback_source_not_in_conversation"))
                        elif (plan.tool_name == "start_data_loop" and plan.arguments.get("candidate_id")
                              not in self._candidate_targets(history, traces, content)):
                            seen_calls.add(key)
                            traces.append(ToolTrace(name=plan.tool_name, status="denied", attempts=0,
                                arguments={}, result={}, error_code="candidate_not_in_conversation"))
                        # 如果工具调用名包含 热点新闻工具 和 日期 等信息。
                        elif plan.tool_name in {"list_hot_news", "read_hot_news"} and has_dated_news_request(content):
                            seen_calls.add(key)
                            traces.append(ToolTrace(name=plan.tool_name, status="denied", attempts=0,
                                                    arguments={}, result={"query_input_error": "dated_saved_report"},
                                                    error_code="dated_request_requires_query"))
                        elif plan.tool_name == "query_hot_news" and snapshot.inputs:
                            # Multi-input SQL semantics are not approved. Keep
                            # the already executed query trace and require one
                            # complete question in the next ordinary turn.
                            seen_calls.add(key)
                            traces.append(ToolTrace(name=plan.tool_name, status="denied", attempts=0,
                                arguments={}, result={"query_input_error": "supplement_requires_new_turn"},
                                error_code="query_supplement_requires_new_turn"))
                        elif plan.tool_name == "query_hot_news":
                            seen_calls.add(key)
                            partial = {}
                            traces.append(ToolTrace(name=plan.tool_name, status="failed", attempts=1,
                                                    arguments=plan.arguments, result=partial, error_code="query_pending"))

                            async def remember_binding(metadata):
                                partial.update(metadata)
                                request_id = metadata.get("understanding_model_request_id")
                                if isinstance(request_id, str) and request_id and request_id not in model_ids:
                                    model_ids.append(request_id)
                                traces[tool_index].result = dict(partial)
                                traces[tool_index].error_code = "query_in_progress"
                                await self._notify(on_event, "phase", {"phase": "hot_news_workflow",
                                    "message": "查询已绑定原有热点运行，正在只读取数、计算指标并调用分析模型。"})

                            traces[tool_index] = await self._execute_query(
                                plan, tenant_id, user_id, str(claim.turn.id), hot_news_query_allowed,
                                partial, remember_binding, original_question=content, deadline=turn_deadline,
                            )
                        elif (getattr(self._tools, "hook_spec", lambda _: None)(plan.tool_name) is not None
                              and self._tools.hook_spec(plan.tool_name).execution_class == "confirm_gated"):
                            seen_calls.add(key)
                            if len(traces) + 2 > self._max_tool_calls:
                                trace = ToolTrace(name=plan.tool_name, status="denied", attempts=0,
                                    arguments={}, result={}, error_code="confirmation_tool_budget")
                            else:
                                trace = await self._execute(plan, tenant_id, str(claim.turn.id),
                                    on_event=on_event, deadline=turn_deadline, user_id=user_id, permissions=permissions)
                            if trace.status == "completed":
                                confirmation = new_confirmation(hook=trace.name, suggested=trace.result["suggested"],
                                    lifetime_seconds=turn_deadline - asyncio.get_running_loop().time(), input_seq=snapshot.input_seq)
                                fields = trace.result.get("confirmation_fields")
                                if fields:
                                    if trace.name == "propose_bundle_candidate" and isinstance(fields, dict):
                                        fields = {**fields, "candidate_version": "conversation-" + canonical_sha256({
                                            "conversation_id": str(conversation_id), "request_id": str(claim.turn.request_id)})}
                                    labels = {"run_id": "运行 ID", "news_id": "新闻 ID", "title": "新闻标题",
                                        "production_bundle_version": "生产 Bundle", "issue_type": "问题类型",
                                        "expected_label_draft": "期望标签草稿", "source_action": "同时执行的反馈动作",
                                        "notice": "操作说明", "candidate_id": "候选 ID", "base_bundle_id": "基准 Bundle ID",
                                        "base_bundle_version": "基准 Bundle 版本", "structured_diff": "实际配置差异",
                                        "golden_dataset_id": "黄金集 ID", "risk_dataset_id": "高风险集 ID",
                                        "evaluation_policy_version": "门禁策略版本", "reason": "建议理由", "candidate_version": "候选版本",
                                        "cohorts": "冻结数据集来源（三层）", "window_start": "错误案例来源窗口起点",
                                        "window_end": "来源截止时间"}
                                    confirmation["fields"] = ([{"label": labels.get(key, key), "value":
                                        json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)}
                                        for key, value in fields.items()] if isinstance(fields, dict) else fields)
                                receipt = trace.result.get("receipt") or CapabilityReceipt(entity_type="writing_job", entity_id=None,
                                        status="not_landed", version=None, deep_link=None,
                                        created_at=datetime.now(timezone.utc)).model_dump(mode="json")
                                trace.result = {"confirmation": confirmation, "receipt": receipt,
                                    "prepared_context": trace.result.get("prepared_context", {})}
                            traces.append(trace)
                        elif (getattr(self._tools, "hook_spec", lambda _: None)(plan.tool_name) is not None
                              and self._tools.hook_spec(plan.tool_name).execution_class == "async_start"):
                            seen_calls.add(key)
                            partial = {}
                            traces.append(ToolTrace(name=plan.tool_name, status="failed", attempts=1,
                                arguments=plan.arguments, result={}, error_code="capability_start_pending"))
                            async def remember_capability_binding(metadata):
                                partial.update(metadata)
                                traces[tool_index].result = dict(partial)
                            traces[tool_index] = await self._start_confirmed_hook(name=plan.tool_name,
                                arguments=plan.arguments, tenant_id=tenant_id, user_id=user_id,
                                permissions=permissions, conversation_id=conversation_id,
                                request_id=claim.turn.request_id, trace_id=str(claim.turn.id),
                                original_content=content, partial=partial, on_bound=remember_capability_binding,
                                confirmed=False)
                        else:
                            seen_calls.add(key)
                            traces.append(await self._execute(plan, tenant_id, str(claim.turn.id),
                                on_event=on_event, deadline=turn_deadline,
                                user_id=user_id, permissions=permissions))
                        trace = traces[-1]
                        understanding_id = trace.result.get("understanding_model_request_id")
                        if isinstance(understanding_id, str) and understanding_id and understanding_id not in model_ids:
                            model_ids.append(understanding_id)
                        # Tool completion is committed under this exact request
                        # claim before announcing it or asking for another plan.
                        # A storage failure stops this loop; there is no in-memory
                        # fallback that pretends the checkpoint is durable.
                        await self.repository.checkpoint_turn(
                            tenant_id=tenant_id, user_id=user_id,
                            conversation_id=conversation_id, turn_id=claim.turn.id,
                            request_id=claim.turn.request_id,
                            tools=traces, model_request_ids=model_ids,
                        )
                        event_data = {
                            "index": tool_index, "name": trace.name, "status": trace.status,
                            "attempts": trace.attempts, "error_code": trace.error_code,
                        }
                        if getattr(self._tools, "hook_spec", lambda _: None)(trace.name):
                            event_data["trace"] = trace.model_dump(mode="json")
                        await self._notify(on_event, "tool_finished", event_data)
                        if trace.status == "completed" and trace.result.get("confirmation"):
                            # The immutable proposal has already been checkpointed.
                            # Confirmation is a server control, never another model plan.
                            proposed_content = content
                            confirmation = trace.result["confirmation"]
                            prepared_context = trace.result.get("prepared_context", {})
                            await notify("phase", {"phase": "awaiting_confirmation",
                                "message": "参数为模型建议；等待本轮确认，尚未执行写操作。"})
                            outcome, values = await self._await_confirmation(
                                tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id,
                                claim=claim, confirmation=confirmation, deadline=turn_deadline)
                            if values:
                                snapshot = snapshot.with_inputs(values)
                                content = snapshot.message()
                            execution_index = len(traces)
                            await notify("tool_started", {"index": execution_index, "name": trace.name})
                            if outcome == "confirm":
                                partial = {}
                                traces.append(ToolTrace(name=trace.name, status="failed", attempts=1,
                                    arguments=confirmation["suggested"], result={}, error_code="writing_start_pending"))
                                async def remember_writing_binding(metadata):
                                    partial.update(metadata)
                                    traces[execution_index].result = dict(partial)
                                traces[execution_index] = await self._start_confirmed_hook(
                                    name=trace.name, arguments=confirmation["suggested"], tenant_id=tenant_id,
                                    user_id=user_id, permissions=permissions, conversation_id=conversation_id,
                                    request_id=claim.turn.request_id, trace_id=str(claim.turn.id),
                                    original_content=proposed_content, partial=partial,
                                    on_bound=remember_writing_binding, prepared_context=prepared_context)
                                answer = (("操作已确认并保存，排队、启动状态与人工门禁见回执卡片。")
                                    if traces[-1].status == "completed" else "操作结果未获确认，请依据已保存引用在工作台核对。")
                            else:
                                traces.append(ToolTrace(name=trace.name, status="denied", attempts=0,
                                    arguments={}, result={"confirmation_outcome": outcome},
                                    error_code=f"confirmation_{outcome}"))
                                target = "启动写作任务" if trace.name == "start_writing_intent" else "执行写操作"
                                answer = {"expired": f"本轮确认已过期，没有{target}，请重新描述要求。",
                                          "cancelled": f"已取消本轮确认，没有{target}，可以重新描述要求。",
                                          "changed": f"补充要求已保存，原确认已作废，没有{target}，请重新描述要求。"}[outcome]
                            executed = traces[-1]
                            await self.repository.checkpoint_turn(tenant_id=tenant_id, user_id=user_id,
                                conversation_id=conversation_id, turn_id=claim.turn.id,
                                request_id=claim.turn.request_id, tools=traces, model_request_ids=model_ids)
                            await notify("tool_finished", {"index": execution_index, "name": executed.name,
                                "status": executed.status, "attempts": executed.attempts,
                                "error_code": executed.error_code, "trace": executed.model_dump(mode="json")})
                            terminal = await finish_success(answer)
                            if terminal is not None:
                                return terminal
                        if (trace.name == "query_hot_news" and trace.status != "completed"
                                and trace.result.get("query_resolution")):
                            # A validated semantic rejection already explains the
                            # boundary. Render it below from the Python snapshot,
                            # without another plan that can repeat or distort it.
                            answer = "本轮查询未执行，具体条件和数据范围限制如下。"
                            terminal = await finish_success(answer)
                            if terminal is not None:
                                return terminal
                    error_code, answer = "planning_limit", "本轮规划已达到上限，已保存工具记录和补充要求；请在下一轮明确追问。"
        except asyncio.CancelledError:
            # 捕获的取消允许有界清理。不要分离未跟踪的
            # 屏蔽数据库任务；严重崩溃/存储中断归咎于租赁。
            await self._interrupt(tenant_id, user_id, conversation_id, claim.turn.id, traces, model_ids)
            raise
        except TimeoutError:
            error_code, answer = "turn_timeout", "本轮处理超时，历史消息已保留，请明确重试。"
        except ContextBudgetExceeded:
            error_code, answer = "context_budget_exceeded", "本轮问题或工具结果超过上下文长度预算，历史与已保存的工具记录已保留，请缩小查询范围。"
        except AgentOutputValidationError as exc:
            if exc.request_id:
                model_ids.append(exc.request_id)
            error_code, answer = "model_output_invalid", "模型输出未通过结构校验，本轮停止，没有执行未批准的工具。"
        except ModelTransportError as exc:
            if exc.request_id:
                model_ids.append(exc.request_id)
            error_code, answer = "model_unavailable", "模型服务暂不可用，本轮已停止，历史消息已保留。"
        except Exception:
            # 切勿暴露带有凭据的传输异常或原始模型输出。
            error_code, answer = "conversation_unavailable", "本轮处理失败，历史消息已保留，没有自动发布或改变配置。"
        try:
            await self._notify(on_event, "phase", {"phase": "saving", "message": "正在保存本轮处理结果。"})
            return await self.repository.finish_turn(
                tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id, turn_id=claim.turn.id,
                assistant_content=answer, tools=traces, model_request_ids=model_ids, error_code=error_code,
            )
        except asyncio.CancelledError:
            await self._interrupt(tenant_id, user_id, conversation_id, claim.turn.id, traces, model_ids)
            raise

    async def _interrupt(self, tenant_id, user_id, conversation_id, turn_id, traces, model_ids):
        try:
            async with asyncio.timeout(5):
                await self.repository.finish_turn(
                    tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id, turn_id=turn_id,
                    assistant_content=self._render_answer("本轮处理已中断，请明确重试。", traces, tenant_id), tools=traces,
                    model_request_ids=model_ids, error_code="interrupted",
                )
        except (Exception, asyncio.CancelledError):
            # 如果原始保存已提交，则存储库 CAS 拒绝
            # 替换它。不可用的数据库将通过现有租约恢复。
            pass

    async def interrupt_unstarted_turn(self, *, tenant_id: str, user_id: str,
                                       conversation_id: UUID, claim):
        if claim.acquired:
            await self._interrupt(tenant_id, user_id, conversation_id, claim.turn.id, [], [])

    """构造"""
    def _render_answer(self, answer, traces, tenant_id):
        rendered = render_tool_results(traces)
        if rendered:
            answer = f"{answer}\n\n{rendered}"
        if self._local:
            description = getattr(self._tools, "query_description", lambda _: None)(tenant_id) or {}
            mixed = (any(trace.name == "query_hot_news" for trace in traces)
                     and description.get("understanding_model", {}).get("provider") == "openai_compatible")
            notice = ("查询理解使用已配置大模型，其余本地演示环节采用规则模拟"
                      if mixed else "本地规则模拟，不代表企业模型质量")
            answer = f"【{notice}】\n{answer}"
        return answer[:30000]

    """模型调用尝试，如果失败就尝试重试等待或者前端返回内容。"""
    @traced("conversation.inference")
    async def _infer(self, payload, *, on_event: ProgressCallback | None = None, call: int = 1):
        for attempt in range(2):
            try:
                with model_attempt_context(attempt=attempt + 1):
                    return await self._model.run_structured(app_id="python:conversation", mode="conversation",
                                                            payload=payload, output_type=ConversationPlan)
            except ModelTransportError as exc:
                if not exc.retryable or attempt == 1:
                    raise
                await self._notify(on_event, "phase", {"phase": "model_retry", "call": call, "attempt": 2,
                                                      "message": "模型暂时不可用，进行一次有界重试。"})
                await asyncio.sleep(0.1)


    @traced("conversation.tool", result_status=True)
    async def _execute(self, plan, tenant_id: str, trace_id: str, *, on_event: ProgressCallback | None = None,
                       deadline: float | None = None, user_id: str | None = None, permissions=frozenset()):
        set_safe_attributes(trace.get_current_span(), {"newsagent.tool.name": plan.tool_name if plan.tool_name in TOOL_NAMES else "unknown"})
        hook = getattr(self._tools, "hook_spec", lambda _: None)(plan.tool_name)
        for attempt in range(1, 3):
            try:
                tool_deadline = asyncio.get_running_loop().time() + 10
                with related_article_deadline(min(deadline, tool_deadline) if deadline else tool_deadline):
                    identity = {"permissions": permissions, "user_id": user_id} if (
                        hook is not None or (plan.tool_name == "capabilities" and
                                             hasattr(self._tools, "available_descriptions"))) else {}
                    result = await asyncio.wait_for(self._tools.execute(name=plan.tool_name, arguments=plan.arguments,
                        tenant_id=tenant_id, trace_id=trace_id, **identity), timeout=10)
                return ToolTrace(name=plan.tool_name, status="completed", attempts=attempt, arguments=plan.arguments, result=result)
            except ConversationToolDenied as exc:
                code = str(exc) if hook is not None else "tool_not_allowed_or_invalid"
                return ToolTrace(name=plan.tool_name, status="denied", attempts=attempt, arguments={}, result=exc.result, error_code=code)
            except AnalysisExecutionError as exc:
                # A worker timeout or invalid result is terminal; do not rerun analysis.
                return ToolTrace(name=plan.tool_name, status="failed", attempts=attempt,
                                 arguments=plan.arguments, result={}, error_code=exc.error_code)
            except (TimeoutError, ConnectionError, HotNewsPersistenceError, ModelTransportError) as exc:
                if attempt == 1 and getattr(exc, "retryable", True):
                    await self._notify(on_event, "phase", {"phase": "tool_retry", "tool_name": plan.tool_name,
                                                          "attempt": 2, "message": "只读工具暂时不可用，进行一次有界重试。"})
                    await asyncio.sleep(0.1)
                    continue
                break
            except Exception:
                break
        return ToolTrace(name=plan.tool_name, status="failed", attempts=attempt, arguments=plan.arguments, result={}, error_code="tool_unavailable")

    """热点新闻查询 Agent"""
    @traced("conversation.query", result_status=True)
    async def _execute_query(self, plan, tenant_id, user_id, trace_id, allowed, partial, on_bound,
                             *, original_question: str, deadline: float | None = None):
        # 此命令还保留/排队工作。不应用读取工具重试，或其十秒超时；外轮预算仍然限制等待。
        set_safe_attributes(trace.get_current_span(), {"newsagent.tool.name": plan.tool_name if plan.tool_name in TOOL_NAMES else "unknown"})
        try:
            if not allowed:
                raise ConversationToolDenied("hot_news_query_not_approved")
            # 第一个规划模型不得删除日期、来源或过滤器，在查询理解端口获取原始请求之前。
            question = plan.arguments.get("question")
            if not isinstance(question, str) or question.strip() != original_question.strip():
                return ToolTrace(name=plan.tool_name, status="denied", attempts=0,
                                 arguments={}, result={"query_input_error": "question_mismatch"},
                                 error_code="query_question_mismatch")
            # 这边执行热点新闻查询
            with related_article_deadline(deadline):
                result = await self._tools.execute_hot_news_query(arguments=plan.arguments, tenant_id=tenant_id,
                    user_id=user_id, trace_id=trace_id, allowed=allowed, on_bound=on_bound)
            return ToolTrace(name=plan.tool_name, status="completed", attempts=1,
                             arguments=plan.arguments, result=result)
        except QueryResolutionError as exc:
            result = {"query_resolution": exc.resolution.model_dump(mode="json")}
            if exc.request_id:
                result["understanding_model_request_id"] = exc.request_id
            return ToolTrace(name=plan.tool_name, status="failed", attempts=1,
                             arguments=plan.arguments,
                             result=result,
                             error_code=f"query_{exc.resolution.reason_code}")
        except SqlAssistantQuestionError:
            return ToolTrace(name=plan.tool_name, status="denied", attempts=1,
                             arguments={}, result={"query_input_error": "input_boundary"},
                             error_code="query_input_boundary")
        except ConversationToolDenied:
            return ToolTrace(name=plan.tool_name, status="denied", attempts=1,
                             arguments={}, result={}, error_code="hot_news_query_not_approved_or_invalid")
        except HTTPException as exc:
            code = {502: "query_workflow_failed", 503: "query_backend_unavailable",
                    504: "query_wait_timeout"}.get(exc.status_code, "query_request_rejected")
            return ToolTrace(name=plan.tool_name, status="failed", attempts=1,
                             arguments=plan.arguments, result=dict(partial), error_code=code)
        except Exception:
            return ToolTrace(name=plan.tool_name, status="failed", attempts=1,
                             arguments=plan.arguments, result=dict(partial),
                             error_code="query_result_unavailable" if partial else "hot_news_query_unavailable")

    async def _await_confirmation(self, *, tenant_id, user_id, conversation_id, claim, confirmation, deadline):
        # Leave time for the terminal checkpoint. The whole turn remains bounded.
        while asyncio.get_running_loop().time() < deadline - 0.05 and not confirmation_expired(confirmation):
            values = await self.repository.claim_inputs(tenant_id=tenant_id, user_id=user_id,
                conversation_id=conversation_id, turn_id=claim.turn.id, request_id=claim.turn.request_id)
            if values:
                if len(values) != 1:
                    return "changed", values
                try:
                    control = parse_confirmation(values[0].content)
                except ValueError:
                    return "changed", values
                if (control is None or not matches_confirmation(control, confirmation)
                        or str(values[0].request_id) != confirmation["input_request_id"]):
                    return "changed", values
                if confirmation_expired(confirmation):
                    return "expired", values
                return ("confirm" if control.action == "confirm" else "cancelled"), values
            await asyncio.sleep(min(0.25, max(0, deadline - asyncio.get_running_loop().time() - 0.05)))
        return "expired", []

    @traced("conversation.confirmed_hook", result_status=True)
    async def _start_confirmed_hook(self, *, name, arguments, tenant_id, user_id, permissions,
                                   conversation_id, request_id, trace_id, original_content, partial, on_bound,
                                   confirmed=True, prepared_context=None):
        set_safe_attributes(trace.get_current_span(), {"newsagent.tool.name": name if name in TOOL_NAMES else "unknown"})
        try:
            result = await self._tools.execute(name=name, arguments=arguments, tenant_id=tenant_id,
                user_id=user_id, trace_id=trace_id, permissions=permissions, confirmed=confirmed,
                conversation_id=conversation_id, request_id=request_id, original_content=original_content,
                on_bound=on_bound, prepared_context=prepared_context)
            return ToolTrace(name=name, status="completed", attempts=1, arguments=arguments, result=result)
        except (ConversationToolDenied, ConversationWritingConflict) as exc:
            return ToolTrace(name=name, status="denied", attempts=1, arguments={},
                             result={**partial, **getattr(exc, "result", {})}, error_code=str(exc))
        except ProductionBundleRuleViolation as exc:
            known_admission = {"admission_queue_full", "admission_policy_conflict", "admission_request_conflict",
                "admission_tenant_mismatch", "admission_enqueue_rejected", "admission_enqueue_unavailable", "admission_rejected"}
            return ToolTrace(name=name, status="denied" if str(exc) in known_admission else "failed", attempts=1,
                arguments={}, result=dict(partial), error_code=str(exc) if str(exc) in known_admission else "capability_start_unavailable")
        except Exception:
            return ToolTrace(name=name, status="failed", attempts=1, arguments=arguments,
                             result=dict(partial), error_code=("writing_start_unavailable" if name == "start_writing_intent"
                                                              else "capability_start_unavailable"))

    @staticmethod
    async def _notify(callback: ProgressCallback | None, event: str, data: dict):
        if callback is not None:
            await callback(event, data)

    @staticmethod
    def _model_trace(trace):
        """Automatic excerpt cards belong to the saved UI projection, not the planner budget."""
        projected = trace.model_dump(mode="json")
        projected["result"].pop("confirmation", None)
        projected["result"].pop("prepared_context", None)
        if trace.name in {"read_hot_news", "query_hot_news"}:
            projected["result"].pop("related_articles", None)
        return projected

    @staticmethod
    def _context_trace(trace):
        result = {key: value for key, value in trace.result.items() if key not in {"confirmation", "prepared_context"}}
        if trace.name in {"read_hot_news", "query_hot_news"} and trace.status == "completed":
            result = {"run_id": result["run_id"], "not_found": bool(result.get("not_found")), "items": [
                {"news_id": item["news_id"], "rank": item["rank"], "title": item["title"]}
                for item in result.get("items", [])],
                **{key: result[key] for key in ("window_start", "window_end") if key in result}}
        elif trace.name == "analyze_hot_news_data" and trace.status == "completed":
            result = {"source": result["source"], "operation": result["operation"], "metric": result["metric"]}
        return {"name": trace.name, "status": trace.status, "arguments": trace.arguments, "result": result}

    @staticmethod
    def _analysis_run_ids(history, traces) -> set[str]:
        """A model may analyze only a run already read in this owned conversation."""
        allowed = set()
        previous = [item for turn in history for item in turn.get("tools", [])]
        for item in [*previous, *(trace.model_dump() for trace in traces)]:
            if item.get("status") != "completed":
                continue
            result = item.get("result", {})
            if item.get("name") in {"read_hot_news", "query_hot_news"} and not result.get("not_found"):
                run_id = result.get("run_id")
                if run_id:
                    allowed.add(run_id)
            elif item.get("name") == "list_hot_news":
                allowed.update(row["run_id"] for row in result.get("items", []) if row.get("run_id"))
            elif item.get("name") == "analyze_hot_news_data":
                source = result.get("source", {})
                run_id = source.get("run_id")
                if run_id:
                    allowed.add(run_id)
                reference_id = source.get("reference", {}).get("run_id")
                if reference_id:
                    allowed.add(reference_id)
        return allowed

    @staticmethod
    def _feedback_targets(history, traces) -> set[tuple[str, str]]:
        targets = set()
        previous = [item for turn in history for item in turn.get("tools", [])]
        for item in [*previous, *(trace.model_dump() for trace in traces)]:
            if item.get("status") != "completed":
                continue
            result = item.get("result", {})
            if item.get("name") in {"read_hot_news", "query_hot_news"} and not result.get("not_found"):
                targets.update((result["run_id"], row["news_id"]) for row in result.get("items", []))
        return targets

    @staticmethod
    def _candidate_targets(history, traces, user_content) -> set[str]:
        # Candidate references originate in an explicit user ID or a persisted
        # tool receipt, never in model text or a retrieved article.
        targets = {str(UUID(value)) for value in re.findall(
            r"(?<![0-9a-f-])[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?![0-9a-f-])",
            user_content, flags=re.I)}
        previous = [item for turn in history for item in turn.get("tools", [])]
        for item in [*previous, *(trace.model_dump() for trace in traces)]:
            if item.get("status") == "completed" and item.get("name") == "propose_bundle_candidate":
                candidate = item.get("result", {}).get("candidate", {})
                if candidate.get("candidate_id"):
                    targets.add(candidate["candidate_id"])
        return targets
