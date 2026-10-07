"""Conversation adapter to the existing isolated hot-news SQL Workflow."""

import asyncio
import time
from fastapi import HTTPException
from app.conversation.tools import ConversationToolDenied
from datetime import timedelta
from uuid import UUID, uuid4

from app.api.dependencies import DataLoopPrincipal, HotNewsPermission
from app.conversation.query_understanding import (
    QueryPolicy, QueryResolutionError, QueryUnderstanding, resolve_query_understanding,
)
from app.domain.errors import AgentOutputValidationError
from app.model_runtime.agent_client import model_request_context
from app.schemas.sql_assistant import SqlAssistantPreviewRequest
from app.services.hot_news_query import HotNewsQueryService
from app.sql_assistant.input_boundary import SqlAssistantQuestionError, screen_question
from app.sql_assistant.scenarios import load_sql_scenarios
from app.sql_assistant.warehouse import WINDOW_START
from examples.hot_news_e2e_support import E2E_TENANT_ID

"""连续失败熔断；冷却后半开放一次探测，成功即复位。"""
class _HotNewsQueryBreaker:
    def __init__(self, *, failure_threshold: int = 3, open_seconds: float = 60.0) -> None:
        self._threshold = failure_threshold
        self._open_seconds = open_seconds
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False

    def check(self) -> None:
        if self._opened_at is None:
            return       # CLOSED 正常放行
        if not self._probing and time.monotonic() - self._opened_at >= self._open_seconds:
            self._probing = True
            return       # HALF_OPEN 放一次探测
        raise ConversationToolDenied("hot_news_query_circuit_open")

    def record_success(self) -> None:
        self._failures, self._opened_at, self._probing = 0, None, False

    def record_failure(self) -> None:
        self._failures += 1
        if self._probing or self._failures >= self._threshold:
            self._opened_at = time.monotonic()
            self._probing = False

    def release_probe(self) -> None:
        if self._probing:
            self._opened_at = time.monotonic()
            self._probing = False

class LocalConversationHotNewsQuery:
    def __init__(self, state):
        settings = state.settings
        if settings.environment != "e2e" or not settings.conversation_hot_news_query_enabled:
            raise ValueError("conversation SQL query requires explicit isolated configuration")
        config = load_sql_scenarios(settings.sql_assistant_scenarios_path)
        scenario = config.resolve(settings.conversation_hot_news_query_scenario)
        if scenario.result_mode != "ranking":
            raise ValueError("conversation hot-news query requires a ranking scenario")
        self._state = state
        self._scenario = scenario.id
        self._start = WINDOW_START + timedelta(hours=settings.conversation_hot_news_query_hour)
        self._end = self._start + timedelta(hours=1)
        self._policy = QueryPolicy(
            scenario=scenario, default_limit=min(5, scenario.max_limit),
            max_limit=scenario.max_limit,
            supported_window_start=self._start, supported_window_end=self._end,
            data_watermark=self._end,
        )
        self._understanding_timeout = config.model_timeout_seconds
        self._breaker = _HotNewsQueryBreaker(failure_threshold=3, open_seconds=60.0)

    def description(self):
        return {"name": "query_hot_news",
                "description": "先理解当前问题并校验日期、榜单来源和条件，再查询模拟数仓并执行原有热点分析Workflow；不发布、不审批。今日不等于样本窗口，范围不满足时不会执行取数。",
                "arguments": {"question": "完整保留当前用户原问题，2..1000字；不得删除日期、来源或条件，不得携带SQL或身份"},
                "scenario_id": self._scenario, "window_start": self._start.isoformat(),
                "window_end": self._end.isoformat(),
                "query_policy": self._policy.model_dump(mode="json"),
                "understanding_schema": QueryUnderstanding.model_json_schema()}

    def available_for(self, tenant_id):
        return tenant_id == E2E_TENANT_ID

    async def _understand(self, *, question, tenant_id):
        # Inspect the raw input before any model is allowed to normalize it.
        try:
            screened = screen_question(question)
        except SqlAssistantQuestionError:
            resolve_query_understanding(question, QueryUnderstanding(
                status="unsupported", unsupported_conditions=["input_boundary"],
            ), self._policy).require_ready()
            raise  # The resolver always blocks this input; keep a fail-closed fallback.
        with model_request_context(tenant_id=tenant_id, trace_id=f"query-understanding-{uuid4()}"):
            try:
                async with asyncio.timeout(self._understanding_timeout):
                    result = await self._state.sql_assistant.agent.run_structured(
                        app_id="python:query-understanding-v1", mode="query_understanding",
                        payload={"question": screened, "policy": self._policy.model_dump(mode="json")},
                        output_type=QueryUnderstanding,
                    )
            except AgentOutputValidationError as exc:
                raise QueryResolutionError(
                    resolve_query_understanding(screened, {}, self._policy),
                    request_id=exc.request_id,
                ) from exc
        resolution = resolve_query_understanding(screened, result.value, self._policy)
        if resolution.status != "ready":
            raise QueryResolutionError(resolution, request_id=result.request_id)
        return resolution, result.request_id

    """执行查询"""
    async def execute(self, *, question, tenant_id, user_id, on_bound):
        from examples.local_simulation import execute_local_hot_news_query

        if not self.available_for(tenant_id):
            raise ValueError("synthetic warehouse tenant required")
        # Unsupported questions are user-facing semantic outcomes, not service
        # availability failures. No SQL preview or Workflow exists at this point.
        resolution, understanding_request_id = await self._understand(
            question=question, tenant_id=tenant_id,
        )
        self._breaker.check()
        try:
            # Only the permission-gated ConversationTools entry may reach here.
            principal = DataLoopPrincipal(tenant_id=UUID(tenant_id), user_id=UUID(user_id),
                                        permissions=frozenset({HotNewsPermission.ADMIN.value}))
            async def bound_with_resolution(metadata):
                await on_bound({**metadata, "question": question,
                                "query_resolution": resolution.model_dump(mode="json"),
                                "understanding_model_request_id": understanding_request_id})

            result = await execute_local_hot_news_query(
                state=self._state, principal=principal,
                on_bound=bound_with_resolution if on_bound is not None else None,
                expected_intent=resolution.intent,
                body=SqlAssistantPreviewRequest(question=resolution.canonical_question, scenario_id=self._scenario,
                                                window_start=self._start, window_end=self._end),
            )
            detail = await HotNewsQueryService(database=self._state.database).get_run_detail(
                tenant_id=tenant_id, run_id=result.run_id,
            )
            if detail is None or detail.run.status != "completed" or detail.sql_tool_trace is None:
                raise ValueError("completed query report unavailable")
            if detail.sql_tool_trace.query_id != result.sql_query_id:
                raise ValueError("query report binding drift")
            response = {"run_id": str(result.run_id), "workflow_id": result.workflow_id,
                    "sql_query_id": result.sql_query_id, "status": result.status,
                    "production_bundle_version": result.production_bundle_version,
                    "window_start": self._start.isoformat(), "window_end": self._end.isoformat(),
                    "question": question, "scenario_id": self._scenario,
                    "query_resolution": resolution.model_dump(mode="json"),
                    "understanding_model_request_id": understanding_request_id,
                    "sql": detail.sql_tool_trace.preview.sql,
                    "parameters": detail.sql_tool_trace.preview.parameters,
                    "query_model_provider": detail.sql_tool_trace.preview.model_provider,
                    "query_model_request_id": detail.sql_tool_trace.preview.model_request_id,
                    "analysis_model_request_ids": [item.analysis.model_request_id for item in detail.ranked_news
                                                    if item.analysis and item.analysis.model_request_id],
                    "analyzed_news_count": result.analyzed_news_count}
        except QueryResolutionError as exc:
            self._breaker.release_probe()
            if exc.resolution.reason_code == "query_intent_mismatch":
                raise QueryResolutionError(resolution.model_copy(update={
                    "status": "unsupported", "reason_code": "query_intent_mismatch",
                    "message": "查询计划与已确认条件不一致，未执行取数。",
                    "intent": None, "canonical_question": "",
                }), request_id=understanding_request_id) from exc
            raise
        except HTTPException as exc:
            if 500 <= exc.status_code <= 599:
                self._breaker.record_failure()
            else:
                self._breaker.release_probe()
            raise
        except asyncio.CancelledError:
            self._breaker.release_probe()
            raise
        except Exception:
            self._breaker.record_failure()
            raise
        self._breaker.record_success()
        return response
