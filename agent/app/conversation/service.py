"""
聊天总协调入口。ConversationAgentService 负责读取历史、调用模型、执行工具、限制超时与调用次数、保存工具检查点和最终答复。
按长度加载已完成历史，达到预算后压缩并保存摘要。
重点看 prepare_turn() 和 run_turn()。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from uuid import UUID

from app.conversation.plan import ConversationPlan
from app.conversation.local import has_dated_news_request
from app.conversation.query_understanding import QueryResolutionError
from app.conversation.context import LengthBasedContext, ContextBudgetExceeded, POLICY_VERSION
from app.conversation.tools import ConversationToolDenied, TOOL_DESCRIPTIONS, render_tool_results
from app.data_analysis.runner import AnalysisExecutionError
from app.domain.errors import AgentOutputValidationError, HotNewsPersistenceError
from app.model_runtime.agent_client import model_request_context
from app.model_runtime.http import ModelTransportError
from app.repositories.conversation import ConversationConflict
from app.schemas.conversation import ToolTrace
from app.sql_assistant.input_boundary import SqlAssistantQuestionError


ProgressCallback = Callable[[str, dict], Awaitable[None]]


class ConversationAgentService:
    def __init__(self, *, repository, model, tools, context_max_chars: int = 32000,
                 context_recent_chars: int = 12000, context_summary_chars: int = 4000,
                 context_threshold_ratio: float = 0.8, context_max_compaction_attempts: int = 2,
                 max_tool_calls: int = 3, turn_timeout_seconds: float = 60, local_simulation: bool = False,
                 runtime_metadata: dict | None = None):
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
        self._timeout = turn_timeout_seconds
        self._local = local_simulation
        self._runtime_metadata = dict(runtime_metadata or {})
        self._runtime_metadata.update(context_policy=POLICY_VERSION, **self._context_options)

    @property
    def turn_timeout_seconds(self) -> float:
        return self._timeout

    def enable_hot_news_query(self, runner):
        self._tools.enable_hot_news_query(runner)

    def runtime_info(self, *, tenant_id, hot_news_query_allowed=False):
        description = getattr(self._tools, "query_description", lambda _: None)(tenant_id)
        analysis = getattr(self._tools, "analysis_description", lambda: None)()
        return {"model_provider": "local" if self._local else self._runtime_metadata.get("model_provider", "unknown"),
                "model_route": self._runtime_metadata.get("model_route", "unknown"),
                "prompt_version": self._runtime_metadata.get("prompt_version", "unknown"),
                "query_enabled": bool(description and hot_news_query_allowed),
                "query_scope": description if description and hot_news_query_allowed else None,
                "data_analysis": analysis}

    async def send(self, *, tenant_id: str, user_id: str, conversation_id: UUID, request_id: UUID, content: str,
                   hot_news_query_allowed: bool = False):
        claim = await self.prepare_turn(tenant_id=tenant_id, user_id=user_id,
                                        conversation_id=conversation_id, request_id=request_id, content=content)
        return await self.run_turn(tenant_id=tenant_id, user_id=user_id,
                                   conversation_id=conversation_id, claim=claim, hot_news_query_allowed=hot_news_query_allowed)

    async def prepare_turn(self, *, tenant_id: str, user_id: str, conversation_id: UUID,
                           request_id: UUID, content: str):
        """Claim before streaming headers, preserving normal ownership/conflict errors."""
        claim = await self.repository.begin_turn(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id,
            request_id=request_id, content=content, stale_after_seconds=int(self._timeout) + 30,
            runtime_metadata=self._runtime_metadata,
        )
        if not claim.acquired and claim.turn.status == "processing":
            raise ConversationConflict("message_processing")
        return claim

    async def run_turn(self, *, tenant_id: str, user_id: str, conversation_id: UUID,
                       claim, on_event: ProgressCallback | None = None, hot_news_query_allowed: bool = False):
        """claim=TurnClaim(acquired=bool是否可以执行, turn包含这一轮的对话)  on_event异步回调函数，这边调用的对象是 publish() 函数，用这个函数异步处理。"""
        if not claim.acquired:
            return claim.turn
        content = claim.turn.user_content
        traces = []
        model_ids = []
        error_code = None
        answer = ""
        try:
            async with asyncio.timeout(self._timeout):
                await self._notify(on_event, "phase", {"phase": "history", "message": "正在恢复本会话上下文。"})
                # 处理上下文长度。
                context = LengthBasedContext(repository=self.repository, model=self._model,
                                             trace_projection=self._context_trace, **self._context_options)
                # 获取工具对象。
                available = list(getattr(self._tools, "descriptions", TOOL_DESCRIPTIONS))
                query = getattr(self._tools, "query_description", lambda _: None)(tenant_id)
                if hot_news_query_allowed and query:
                    available.append(query)
                # 定义一个函数内的异步函数，传入之前的异步函数。
                async def notify(event, data):
                    await self._notify(on_event, event, data)
                def input_payload():
                    return {"message": content, "history": [],
                            "tool_results": [item.model_dump(mode="json") for item in traces],
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
                    for call_index in range(self._max_tool_calls + 1):
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
                        if plan.action == "respond":
                            # Authoritative numeric data is rendered below from snapshots by Python.
                            if re.search(r"\d|news_id|run_id|query_id|chunk_id", plan.answer, flags=re.I):
                                answer = "解释文字未通过数字或引用约束；权威数据只显示本轮工具的原始快照。"
                            else:
                                answer = plan.answer
                            break
                        if len(traces) >= self._max_tool_calls:
                            answer = "本轮工具调用已达到上限，可以在下一轮继续明确追问。"
                            break
                        key = json.dumps([plan.tool_name, plan.arguments], ensure_ascii=False, sort_keys=True)
                        tool_index = len(traces)
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
                        elif plan.tool_name in {"list_hot_news", "read_hot_news"} and has_dated_news_request(content):
                            seen_calls.add(key)
                            traces.append(ToolTrace(name=plan.tool_name, status="denied", attempts=0,
                                                    arguments={}, result={"query_input_error": "dated_saved_report"},
                                                    error_code="dated_request_requires_query"))
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
                                partial, remember_binding, original_question=content,
                            )
                        else:
                            seen_calls.add(key)
                            traces.append(await self._execute(plan, tenant_id, str(claim.turn.id), on_event=on_event))
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
                        await self._notify(on_event, "tool_finished", {
                            "index": tool_index, "name": trace.name, "status": trace.status,
                            "attempts": trace.attempts, "error_code": trace.error_code,
                        })
                rendered = render_tool_results(traces)
                if rendered:
                    answer = f"{answer}\n\n{rendered}"
                if self._local:
                    answer = f"【本地规则模拟，不代表企业模型质量】\n{answer}"
                answer = answer[:30000]
        except asyncio.CancelledError:
            # A caught cancellation permits bounded cleanup. Do not detach an untracked
            # shielded database task; hard crashes/storage outages fall back to the lease.
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
            # Never expose credential-bearing transport exceptions or raw model output.
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
                    assistant_content="本轮处理已中断，请明确重试。", tools=traces,
                    model_request_ids=model_ids, error_code="interrupted",
                )
        except (Exception, asyncio.CancelledError):
            # If the original save already committed, repository CAS refuses to
            # replace it. An unavailable database is recovered by the existing lease.
            pass

    async def interrupt_unstarted_turn(self, *, tenant_id: str, user_id: str,
                                       conversation_id: UUID, claim):
        if claim.acquired:
            await self._interrupt(tenant_id, user_id, conversation_id, claim.turn.id, [], [])

    """模型调用和重试函数。"""
    async def _infer(self, payload, *, on_event: ProgressCallback | None = None, call: int = 1):
        for attempt in range(2):
            try:
                return await self._model.run_structured(app_id="python:conversation", mode="conversation",
                                                        payload=payload, output_type=ConversationPlan)
            except ModelTransportError as exc:
                if not exc.retryable or attempt == 1:
                    raise
                await self._notify(on_event, "phase", {"phase": "model_retry", "call": call, "attempt": 2,
                                                      "message": "模型暂时不可用，进行一次有界重试。"})
                await asyncio.sleep(0.1)

    async def _execute(self, plan, tenant_id: str, trace_id: str, *, on_event: ProgressCallback | None = None):
        for attempt in range(1, 3):
            try:
                result = await asyncio.wait_for(self._tools.execute(name=plan.tool_name, arguments=plan.arguments,
                                                                    tenant_id=tenant_id, trace_id=trace_id), timeout=10)
                return ToolTrace(name=plan.tool_name, status="completed", attempts=attempt, arguments=plan.arguments, result=result)
            except ConversationToolDenied:
                return ToolTrace(name=plan.tool_name, status="denied", attempts=attempt, arguments={}, result={}, error_code="tool_not_allowed_or_invalid")
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

    """执行查询 query """
    async def _execute_query(self, plan, tenant_id, user_id, trace_id, allowed, partial, on_bound,
                             *, original_question: str):
        # This command also persists/enqueues work. Do not apply read-tool retry
        # or its ten-second timeout; the outer turn budget still bounds waiting.
        try:
            if not allowed:
                raise ConversationToolDenied("hot_news_query_not_approved")
            # The first planning model must not erase dates, sources or filters
            # before the query-understanding Port gets the original request.
            question = plan.arguments.get("question")
            if not isinstance(question, str) or question.strip() != original_question.strip():
                return ToolTrace(name=plan.tool_name, status="denied", attempts=0,
                                 arguments={}, result={"query_input_error": "question_mismatch"},
                                 error_code="query_question_mismatch")
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
        except Exception:
            return ToolTrace(name=plan.tool_name, status="failed", attempts=1,
                             arguments=plan.arguments, result=dict(partial),
                             error_code="query_in_progress" if partial else "hot_news_query_unavailable")

    @staticmethod
    async def _notify(callback: ProgressCallback | None, event: str, data: dict):
        if callback is not None:
            await callback(event, data)

    @staticmethod
    def _context_trace(trace):
        result = trace.result
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
